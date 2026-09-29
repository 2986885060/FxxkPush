"""FxxkPush 单一配置读取器 —— 全项目唯一真实来源是仓库根的 fp.config.json。

四类信息都在那一个文件里（支持 // 行注释）：
  ai      ① OCR/视觉模型端点与分诊参数（ai_triager 与 vision 共用）
  ntfy    ② ntfy token（tk_ 一行）
  vps     ③ VPS SSH 四段凭据（host/port/user/password）
  vision  ④ 要盯的窗口清单 + 视觉运行参数（原 vision_targets.json 整体）

设计取舍：选「各消费方直接读单文件」而不是「单文件同步写出旧文件」——
本项目六个服务是登录时各自独立启动的（HKCU Run，不经 start_services），
写盘同步会引入启动竞态与两份真相；一个共享读取模块零同步、零漂移。

契约（与原先各处的读取行为一一对齐）：
- ntfy_token()：取到非空才缓存，失败/为空下次自动重试（AV 抖动可自愈）；
  环境变量 FP_NTFY_TOKEN 优先（pc_subscriber / notification_listener 原有语义）
- ai()：永远返回带默认值的 dict，缺文件/坏块不炸导入期
- vps()：没配置抛 ValueError —— 消费方本来就包在 try 里
- vision()：问题收集在 problems 列表里，由 vision_listener 记日志
  （本模块不 import pclog：vps_exec 这类根目录工具也用它，不希望拖出日志句柄）
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # pc/core/fpconfig.py → 仓库根
PATH = ROOT / "fp.config.json"

_cache: tuple | None = None   # (mtime_ns, data)；None = 未缓存（缺失/坏文件）
_tok = ""


def _strip_comments(text: str) -> str:
    """去掉字符串外的 // 行注释 —— fp.config.json 允许写注释，
    json.loads 不接受，读之前在这剥掉（字符串里的 // 不受影响）。"""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def raw(force: bool = False) -> dict:
    """整个配置（mtime 缓存）：文件改动自动重读；坏文件/缺失**不缓存**。

    以前把读失败结果缓存成 {} 且永不失效 —— 开机时被 AV/半写撞一下，整个
    配置就钉死在空值直到重启（rules 全失效、TARGETS 空转、vps 改对也不自
    愈）。现在：缺失→文件出现即生效（向导后补建配置）；解析失败→下次调用
    重读；mtime 变化→改配置即时生效（apps 黑名单/必推词无需重启）。
    导入期读取点仍绝不因此把 pythonw 打死（失败一律返回 {}）。
    """
    global _cache
    try:
        mtime = PATH.stat().st_mtime_ns
    except OSError:
        _cache = None            # 文件不存在：不缓存，出现后自动生效
        return {}
    if not force and _cache is not None and _cache[0] == mtime:
        return _cache[1]
    try:
        data = json.loads(_strip_comments(PATH.read_text(encoding="utf-8")))
    except Exception:
        _cache = None            # 半写/AV锁/手改坏：不缓存，下次重读
        return {}
    _cache = (mtime, data if isinstance(data, dict) else {})
    return _cache[1]


def reset() -> None:
    """丢弃缓存（改文件后强制重读；测试用）。"""
    global _cache, _tok
    _cache = None
    _tok = ""


AI_DEFAULTS = {
    "provider": "",
    "model": "云端/本地OCR模型",
    "base_url": "云端/本地OCR模型端点",
    "api_key": "",
    "max_tokens": 200,             # ai_triager 文字分诊输出预算
    "max_completion_tokens": 600,  # vision 视觉分诊输出预算
    "dedup_window_sec": 1800,
    "daily_report_time": "22:00",  # 预留键（日报尚未实现）
}


def ai() -> dict:
    """① OCR/视觉模型端点与分诊参数。永远返回带默认值的 dict。"""
    cfg = dict(AI_DEFAULTS)
    sec = raw().get("ai")
    if isinstance(sec, dict):
        cfg.update({k: v for k, v in sec.items() if v is not None})
    return cfg


def ntfy_token() -> str:
    """② ntfy token。非空才缓存；为空/失败下次重读（AV 抖动自愈）。
    环境变量 FP_NTFY_TOKEN 优先。"""
    global _tok
    if _tok:
        return _tok
    env = os.environ.get("FP_NTFY_TOKEN", "").strip()
    if env:
        _tok = env
        return _tok
    sec = raw(force=True).get("ntfy")     # 没缓存到 token → 重读文件
    _tok = str(sec.get("token") or "").strip() if isinstance(sec, dict) else ""
    return _tok


def vps() -> tuple[str, str, str, str]:
    """③ VPS SSH 四段 (host, port, user, password)，port 保持字符串
    （paramiko 侧各自 int()）。没配置/字段缺失抛 ValueError。"""
    sec = raw().get("vps")
    if not isinstance(sec, dict):
        raise ValueError("fp.config.json 缺少 vps 块（host/port/user/password）")
    host = str(sec.get("host") or "").strip()
    if not host:
        raise ValueError("fp.config.json 的 vps.host 为空")
    user = str(sec.get("user") or "").strip()
    if not user:
        raise ValueError("fp.config.json 的 vps.user 为空")
    port = str(sec.get("port") or "22").strip()
    password = str(sec.get("password") or "")
    return host, port, user, password


def vision() -> tuple[dict, dict, list[str]]:
    """④ 窗口清单 + 视觉运行参数。返回 (settings, targets, problems)。

    校验规则（原 vision_listener._load_targets）：条目须是对象且有
    process；enabled=false 跳过；title 必须是合法正则；顶层 _ 键忽略。
    """
    sec = raw().get("vision")
    problems: list[str] = []
    if not isinstance(sec, dict):
        return {}, {}, ["fp.config.json 缺少 vision 块，无视觉目标"]
    data = dict(sec)
    settings = data.pop("settings", {})
    if not isinstance(settings, dict):
        problems.append("vision.settings 不是对象，已忽略")
        settings = {}
    else:
        settings = dict(settings)          # 拷贝：别改到缓存里的原始配置
        if "ai" in settings:
            settings.pop("ai")
            problems.append("vision.settings.ai 已废弃：端点统一用顶层 ai 块")

    def _num(key: str, default: int) -> int:
        v = settings.get(key, default)
        try:
            return int(v)
        except (TypeError, ValueError):
            problems.append(f"vision.settings.{key} 非数值({v!r})，已用默认 {default}")
            return default

    # 数值收敛：手改配置写成 "30"/"abc" 时，原实现会在导入期或扫描期 int()
    # 直接 FATAL、或在 EnumWindows 回调里 TypeError 中止枚举（表现成「窗口
    # 未开启」误判且服务看着健康）
    for k, dflt in (("poll_min", 30), ("active_from", 8), ("active_to", 24),
                    ("dedup_ttl_sec", 21600), ("offscreen_offset", 120)):
        settings[k] = _num(k, dflt)
    if settings["active_from"] >= settings["active_to"]:
        problems.append("vision.settings active_from>=active_to：活跃时段为空，将永不扫描")

    targets: dict = {}
    for key, spec in data.items():
        if key.startswith("_"):            # 顶层 _注释 之类保留键
            continue
        if not isinstance(spec, dict) or not spec.get("process"):
            problems.append(f"[{key}] 配置无效（至少要有 process 字段），跳过")
            continue
        if not spec.get("enabled", True):
            problems.append(f"[{key}] enabled=false，跳过")
            continue
        if spec.get("title"):
            try:
                re.compile(spec["title"])
            except re.error as e:
                problems.append(f"[{key}] title 正则非法: {e}，跳过")
                continue
        spec = dict(spec)                   # 拷贝后再收敛，别改缓存
        try:
            spec["min_w"] = int(spec.get("min_w", 400))
        except (TypeError, ValueError):
            problems.append(f"[{key}] min_w 非数值，已用默认 400")
            spec["min_w"] = 400
        if "poll_min" in spec:
            try:
                spec["poll_min"] = int(spec["poll_min"])
            except (TypeError, ValueError):
                problems.append(f"[{key}] poll_min 非数值，已回退全局间隔")
                spec["poll_min"] = None
        targets[key] = spec
    if not targets:
        problems.append("vision 里没有启用的目标")
    return settings, targets, problems


APPS_DEFAULT = {
    "ignore": False,        # true = 该来源整段静默（不问 AI 不推送）
    "blacklist": [],        # 会话/标题黑名单：命中即静默归档
    "must_push": [],        # 必推词（不区分大小写子串）：命中走硬规则直推
    "prompt_hint": "",      # 追加到分诊系统提示的该来源补充说明
}


def _apps_norm(rules: dict) -> dict:
    """把合并结果收敛成固定形状，防手改配置的类型漂移。"""
    rules["ignore"] = bool(rules.get("ignore"))
    for k in ("blacklist", "must_push"):
        v = rules.get(k)
        rules[k] = [str(x) for x in v if x] if isinstance(v, list) else []
    rules["prompt_hint"] = str(rules.get("prompt_hint") or "")
    return rules


def rules_for(src: str) -> dict:
    """⑤ 按来源解析声明式规则（fp.config.json 的 apps 块）——自定义软件
    接入的核心：新来源只需声明 ignore / blacklist / must_push / prompt_hint。

    条目 key 或其 ``match`` 列表命中 src → 与 ``_default`` 浅合并返回；
    未命中返回 ``_default``。返回值供只读消费。
    """
    data = raw().get("apps")          # 注意别写成 raw = raw()：局部绑定会
    if not isinstance(data, dict):     # 让下面的 raw() 变成 UnboundLocalError
        return dict(APPS_DEFAULT)
    default = dict(APPS_DEFAULT)
    base = data.get("_default")
    if isinstance(base, dict):
        default.update(base)
    default = _apps_norm(default)
    for key, spec in data.items():
        if key == "_default" or not isinstance(spec, dict):
            continue
        aliases = spec.get("match")
        hit = (src == key) or (isinstance(aliases, list) and src in aliases)
        if hit:
            merged = dict(default)
            merged.update(spec)
            return _apps_norm(merged)
    return default
