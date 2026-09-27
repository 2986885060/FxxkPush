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

_cache: dict | None = None
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
    """整个配置（带缓存）。文件缺失/坏掉/不是对象一律返回 {} ——
    导入期读取点绝不因此把 pythonw 打死，降级策略由调用方决定。"""
    global _cache
    if _cache is not None and not force:
        return _cache
    try:
        data = json.loads(_strip_comments(PATH.read_text(encoding="utf-8")))
        _cache = data if isinstance(data, dict) else {}
    except Exception:
        _cache = {}
    return _cache


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
        targets[key] = spec
    if not targets:
        problems.append("vision 里没有启用的目标")
    return settings, targets, problems
