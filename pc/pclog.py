"""FxxkPush 统一日志 —— 单一格式、单一出口、trace_id 贯穿。

所有 PC 侧服务（含隧道、看门狗）都必须用这个模块写日志，不要各自
手写 print + open()：格式不一致、时间戳缺日期、pythonw 下 stdout 丢失，
这三个坑在 2026-09 的排查里各坑过我们一次。

统一格式：
    2026-09-22T13:05:12.123+08:00 [INFO ] ai_triager | trace=ab12cd34 | 消息

统一出口：
    pc/logs/<service>.log   RotatingFileHandler 2MB x3（含日期的时间戳，
                            跨天排序才成立）
    stderr                  仅当可用（pythonw 下 sys.stderr 可能为 None，
                            必须防御，否则进程启动即崩）

trace_id：
    消息经 ntfy 中转时靠 header ``X-Fp-Trace`` 贯穿（消息体可能是纯文本，
    塞 JSON 不通用）。发布方 set_trace_id()，订阅方从 header 取出来再 set，
    中间每一跳的日志都会带上同一串 id，一条消息的完整链路用 grep 就能拉出来。

用法::

    import pclog
    log = pclog.get_logger("ai_triager")

    log.info("hello")                    # trace=- （未绑定）
    with pclog.trace("deadbeef"):
        log.info("处理中")               # trace=deadbeef
"""
from __future__ import annotations

import contextvars
import logging
import os
import re
import sys
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).parent
LOG_DIR = Path(os.environ.get("FP_LOG_DIR") or (HERE / "logs"))

# ntfy 只透传已知字段，自定义 header / 未知 JSON 字段一律被服务端丢弃
# （实测：X-Fp-Trace、trace_id、sequence_id 均 HTTP 200 但订阅端收不到），
# 唯一能带着元数据跨进程走的是 tags —— trace 就寄生在这里。
TAG_PREFIX = "t_"          # tags 里的 trace 前缀，如 t_ab12cd34

_trace: contextvars.ContextVar = contextvars.ContextVar("fp_trace", default="-")

_configured: dict[str, logging.Logger] = {}
_utc_offset = time.strftime("%z")  # +0800


def new_trace_id() -> str:
    """短 id：8 位 hex 够单机消息量用（42 亿种），日志里 grep 不费劲，
    变成 ntfy tag 也不会把通知撑得太长。"""
    return uuid.uuid4().hex[:8]


def set_trace_id(tid: str | None) -> str:
    """绑定当前上下文的 trace_id。

    None/空串 = 新开一个；显式传 "-" 表示主动清除（重连、周期任务这些
    不属于任何消息的事件应该回到无 trace 状态）。
    """
    if not tid:
        tid = new_trace_id()
    _trace.set(tid)
    return tid


def get_trace_id() -> str:
    return _trace.get()


class trace:  # noqa: N801 - 用作上下文管理器，小写才顺手
    """``with pclog.trace(tid): ...`` —— 块内所有日志带同一个 trace_id。"""

    def __init__(self, tid: str | None = None):
        self._tid = tid
        self._token = None

    def __enter__(self):
        self._token = _trace.set(self._tid or new_trace_id())
        return self._tid

    def __exit__(self, *exc):
        if self._token is not None:
            _trace.reset(self._token)
        return False


class _Formatter(logging.Formatter):
    """唯一的日志格式。级别压到 5 字符，列才对得齐。"""

    _SHORT = {50: "CRIT", 40: "ERROR", 30: "WARN", 20: "INFO", 10: "DEBUG", 0: "DEBUG"}

    def formatTime(self, record, datefmt=None):  # noqa: N802 - logging API
        ct = time.localtime(record.created)
        ms = int(record.msecs)
        return f"{time.strftime('%Y-%m-%dT%H:%M:%S', ct)}.{ms:03d}{_utc_offset}"

    def format(self, record: logging.LogRecord) -> str:
        # record.msg 可能是 % 格式化的 args，先算好再嵌进自定义串
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        level = self._SHORT.get(record.levelno, str(record.levelno))
        ts = self.formatTime(record)
        tid = _trace.get()
        head = f"{ts} [{level:<5}] {record.name:<22} | trace={tid} | "
        # 多行消息（traceback、或者 triager 的 f"{title}\n{body}"）必须每行
        # 重复完整前缀 —— 否则续行没有时间戳也没有 trace，grep trace=xxx
        # 会漏掉半条消息。2026-09-22 实测踩到过这个坑。
        return "\n".join(head + part for part in msg.split("\n"))


class _SafeStreamHandler(logging.StreamHandler):
    """pythonw 下 sys.stderr 是 None：直接挂上去会在首次写日志时炸掉进程。"""

    def __init__(self):
        stream = sys.stderr
        if stream is None or not hasattr(stream, "write"):
            super().__init__(stream=None)
            self._disabled = True
        else:
            super().__init__(stream=stream)
            self._disabled = False

    def emit(self, record):
        if self._disabled:
            return
        try:
            super().emit(record)
        except Exception:
            self.handleError(record)


def get_logger(service: str, level: int = logging.INFO) -> logging.Logger:
    """取服务专属 logger；同名重复调用返回同一个实例。"""
    if service in _configured:
        return _configured[service]

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(service)
    logger.setLevel(level)
    logger.propagate = False          # 不往 root 冒泡，避免重复输出
    fmt = _Formatter()

    fh = RotatingFileHandler(LOG_DIR / f"{service}.log",
                             maxBytes=2 * 1024 * 1024, backupCount=3,
                             encoding="utf-8", delay=True)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = _SafeStreamHandler()
    if not sh._disabled:
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    _configured[service] = logger
    return logger


def tags_with_trace(tags=None) -> list:
    """给 ntfy payload 的 tags 挂上当前 trace_id（替掉已有的，避免累积）。

    发布方调它：同一条消息在所有订阅方日志里都叫同一个 trace。
    """
    tid = _trace.get()
    if tid == "-":
        tid = set_trace_id(None)
    out = [t for t in (tags or []) if not t.startswith(TAG_PREFIX)]
    out.append(TAG_PREFIX + tid)
    return out


def extract_trace(ev) -> str | None:
    """只从 ntfy 事件里取出 trace_id，不绑定。

    配合 ``with pclog.trace(pclog.extract_trace(ev))`` 使用，块结束自动
    恢复 —— 否则一条消息的 trace 会泄漏到后面的重连错误上，grep 这条
    消息时会捞到一堆无关的 ConnectError。
    """
    if not isinstance(ev, dict):
        return None
    for t in (ev.get("tags") or []):
        if isinstance(t, str) and t.startswith(TAG_PREFIX) and len(t) > len(TAG_PREFIX):
            return t[len(TAG_PREFIX):]
    mid = ev.get("id")
    if mid:
        return str(mid)[:8]
    return None


def bind_from_event(ev) -> str:
    """从 ntfy 订阅事件取 trace_id 并绑定到当前上下文。

    优先 tags（发起方带过来的）；没有就用 ntfy 消息 id 派生 —— 同一条消息
    在多个订阅方（subscriber / triager 都订 fp-vps）拿到的 id 相同，
    链路照样对得上，VPS 侧纯文本告警也能追溯。
    """
    return set_trace_id(extract_trace(ev))


# 各服务原来的 log(msg) 有几百个调用点，不值得逐个改成 log(..., level=)。
# 按消息内容自动定级，level 的收益就拿到了，调用点一行不动。
_ERR_PAT = re.compile(
    r"FATAL|Traceback|\berror\b|failed|exception|失败|拒绝|超时|"
    r"denied|refused|not found|disconnected", re.I)
_WARN_PAT = re.compile(r"retry|reconnect|重试|ignored|skip|降级|fallback", re.I)


def log_auto(logger: logging.Logger, msg: str) -> None:
    """按内容定级后写日志 —— 各服务 log() 的统一实现。"""
    if _ERR_PAT.search(msg):
        lvl = logging.ERROR
    elif _WARN_PAT.search(msg):
        lvl = logging.WARNING
    else:
        lvl = logging.INFO
    logger.log(lvl, msg)
