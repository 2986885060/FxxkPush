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
    ntfy 只透传已知字段 —— 自定义 header（X-Fp-Trace）和未知 JSON 字段
    （trace_id / sequence_id）都会被服务端静默丢弃（实测 HTTP 200，但订阅端
    收不到），唯一能带着元数据跨进程走的只有 tags。所以 trace 寄生在 tags 里，
    形如 ``t_ab12cd34``：发布方用 tags_with_trace() 挂上，订阅方用
    extract_trace() 配合 ``with trace(...)`` 取下（块结束自动恢复 —— 直接
    bind 不恢复的话，一条消息的 trace 会泄漏到后面的重连错误上）。
    中间每一跳的日志都带同一串 id，grep 一次拉出全链路。

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


def read_tail(path, n: int = 60, max_bytes: int = 65536) -> list[str]:
    """只读文件末尾 max_bytes，返回最后 n 行（str 列表）。

    check_link / is_our_toast 这类"只看最近几行"的检查，原来写的是
    ``read_text().splitlines()[-60:]`` —— 为了 60 行把整个文件读进来。
    2MB 的日志轮转阈值 x 2 个文件 x 每 60 秒一次 ≈ 5.7GB/天的纯磁盘读，
    全喂给页缓存还拖着 CPU。从尾部 seek 一次只碰 64KB。
    """
    try:
        size = Path(path).stat().st_size
        with Path(path).open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()      # 丢掉可能被截断的半行
            data = f.read().decode("utf-8", "replace")
        return data.splitlines()[-n:]
    except Exception:
        return []


_rot_warn_at: dict[str, float] = {}


def _rot_warn(path, err) -> None:
    """轮转失败限频留痕（r7-5）：原来 except: pass 完全无日志，运维只看到
    文件无限涨却查不到原因。写到独立小文件（不能用 logging —— 轮转失败的
    很可能就是 logging 的 handler 出问题），每路径每小时最多一条。"""
    key = str(path)
    now = time.time()
    if now - _rot_warn_at.get(key, 0) < 3600:
        return
    _rot_warn_at[key] = now
    try:
        p = Path(path).parent / "pclog_internal.log"
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{now:.0f} rotate failed for {path}: {err!r}\n")
        if p.stat().st_size > 256 * 1024:      # 自身也封顶
            p.write_bytes(p.read_bytes()[-64 * 1024:])
    except Exception:
        pass


def _trim_inplace(path, keep: int) -> None:
    """不 rename 的截断：从倒数 keep 字节的第一个完整行起保留。
    r+b 截断不要求文件独占，跨进程句柄挡住 rename 时这是唯一还能生效的
    收缩手段（r7-5 硬上界）。"""
    with Path(path).open("r+b") as f:
        size = path.stat().st_size if False else f.seek(0, 2)
        if size <= keep:
            return
        f.seek(size - keep)
        f.readline()                    # 丢掉可能被截断的半行
        pos = f.tell()
        data = f.read()
        f.seek(0)
        f.write(data)
        f.truncate()


def append_rotating(path, text: str, *, max_bytes: int = 512 * 1024,
                    mode: str = "rotate") -> None:
    """追加一行；文件超过 max_bytes 时按 mode 收缩。

    mode="rotate"（归档类）：主文件改名 .1（旧 .1 丢弃），保留一段完整历史。
    mode="tail"（回环指纹类）：只保留末尾一半，且从第一个完整行起切 —— 这类
    文件只有最近的记录有用（is_our_toast 只读尾部 60 行），留历史没意义。

    调用点自己包 try：归档失败不该中断主流程，但也不能静默吞掉（4 个 jsonl
    原来全部零轮转，append-only 无限增长）。
    """
    path = Path(path)
    try:
        # r7-5：exists+stat 一起进 try —— AV/权限让 stat 抛 OSError 时，
        # 原实现异常冒到调用方，调用方 catch 后**这一行归档照样丢**
        # （P2-6 声称要杜绝的形态只堵了一半）。
        need = path.exists() and path.stat().st_size > max_bytes
    except OSError as e:
        need = False
        _rot_warn(path, e)
    if need:
        # 轮转是**尽力而为**，绝不因为轮转失败丢掉这一行归档（第 6 轮 P2-6）：
        # exists() 与 replace() 之间另一进程可能刚好也完成了轮转（TOCTOU），
        # 这时 path.replace 抛 FileNotFoundError。更常见的是 Windows 上另一
        # 个进程持着同一日志的句柄（跨进程共享 RotatingFileHandler，r7-7），
        # rename 必然 PermissionError。原实现第一次没接住、第二次 except pass
        # 又**无痕**且无上限 —— .1 一直换不掉，文件无限涨到磁盘满。
        rotated = False
        try:
            if mode == "tail":
                data = path.read_bytes()[-(max_bytes // 2):]
                nl = data.find(b"\n")
                if nl != -1:
                    data = data[nl + 1:]
                tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
                tmp.write_bytes(data)
                tmp.replace(path)      # r7-5：原子；失败不落半截/空文件
            else:
                bak = path.with_name(path.name + ".1")
                bak.unlink(missing_ok=True)
                path.replace(bak)
            rotated = True
        except OSError as e:
            _rot_warn(path, e)
        if not rotated:
            # 就地截断兜底：rename 被跨进程句柄挡住时，r+b 截断不要求独占，
            # 照样能把文件压回上限内 —— 增长有界（r7-5 的硬上界诉求），
            # 代价是这次轮转丢了 .1 历史，可接受。
            try:
                _trim_inplace(path, max_bytes // 2)
            except OSError as e2:
                _rot_warn(path, e2)
    with path.open("a", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")


def tags_with_trace(tags=None) -> list:
    """给 ntfy payload 的 tags 挂上当前 trace_id（替掉已有的，避免累积）。

    发布方调它：同一条消息在所有订阅方日志里都叫同一个 trace。
    当前没有 trace 时会**生成并绑定**（这样发布方自己的日志也带上同一串 id，
    链路才完整）—— 但绑定后不恢复，所以在长驻进程里必须放在
    ``with pclog.trace(...)`` 内调用，否则之后的心跳/重连日志都会挂上这条
    消息的 id（bind_from_event 有同样的坑，那边已标注）。
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

    警告：本函数**绑定后不恢复**。长驻进程里直接调它，之后的周期日志和重连
    错误都会挂上最后一条消息的 trace（2026-09-22 实测 grep 一条消息时捞到
    无关的 ReadError，就是这么来的）。要恢复就用
    ``with pclog.trace(pclog.extract_trace(ev)):``；一次性进程或明确不需要
    恢复的场景才用本函数。
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
