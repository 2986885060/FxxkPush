#!/usr/bin/env python3
"""FxxkPush vision listener — 配置驱动的窗口视觉分诊。

配置读取自仓库根 fp.config.json（全项目唯一真实来源，经 pc/core/fpconfig.py）：
  - ai 块：OCR 端点（base_url / api_key / model，与 ai_triager 分诊共用）
  - vision 块：settings 运行参数（轮询 / @我昵称 / 活跃时段 / 去重 / 屏外
    偏移）+ 窗口条目 —— process（PID→exe，必填）→ class（子串，可选）→
    title（正则，可选）三层匹配，同条件取面积最大的主窗口；条目可带
    enabled（开关）/ poll_min（独立轮询）/ prompt（整段提示词覆盖）
  - apps 块：按来源声明分诊规则（ignore / blacklist / must_push /
    prompt_hint），与闸门 ai_triager 消费同一份声明

被监控的窗口会被挪到屏幕外（保持可见、不最小化），每 POLL 分钟
PrintWindow 截图发给云端/本地OCR模型识别。

Important rules (user-defined):
  - @所有人 or @我 in any chat  -> important
  - 家人联系/日程/账户安全/服务异常 -> important
  - everything else -> silent archive

Schedule: active 08:00-23:59, quiet 00:00-07:59.

用法：
    python vision_listener.py          # 常驻监听
    python vision_listener.py park     # 把配置的窗口挪到屏幕外（一次性）
    python vision_listener.py list     # 列出可见窗口，帮你填 fp.config.json vision 块
"""
import base64
import os
import ctypes
import json
import re
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import httpx
import psutil

HERE = Path(__file__).resolve().parents[1]  # pc/（本文件在 pc/services/）
NTFY_BASE = os.environ.get("FP_NTFY_URL", "http://127.0.0.1:2586")  # via SSH tunnel (see pc/services/ntfy_tunnel.py)

sys.path[:0] = [str(HERE),                    # pc/
                str(HERE / "core")]           # 公共件 pclog/fpconfig/fp_feedback/alert_fallback
import fpconfig
# 导入期读取的安全性由 fpconfig 保证：缺文件/坏块/AV 抖动一律降级成
# ""/{}，绝不把 pythonw 打死在 excepthook 挂上之前（原先这里的 try 就是
# 为这个场景准备的，现在收敛到 fpconfig 一处）。
NTFY_TOKEN = fpconfig.ntfy_token()
AI = fpconfig.ai()


def _auth() -> dict:
    """鉴权头。**空 token 时不带头** + 每次取不到都重试（fpconfig 非空才缓存）。

    空 ``Bearer `` 是非法头值 → httpx LocalProtocolError（TransportError，
    没有 .response）；不带头则服务端回 401，push() 的 except 接得住、
    留日志（第 5 轮在 ai_triager 确认的同型问题）。
    """
    global NTFY_TOKEN
    if not NTFY_TOKEN:
        NTFY_TOKEN = fpconfig.ntfy_token()
    return {"Authorization": f"Bearer {NTFY_TOKEN}"} if NTFY_TOKEN else {}
ARCHIVE = HERE / "vision_log.jsonl"
SEEN_STATE = HERE / "vision_state.json"
# SEEN_TTL / POLL_MIN / ACTIVE_* / MY_NAME / OFFSCREEN_X_OFFSET
# 全部从 fp.config.json vision 块的 settings 派生（见下方 TARGETS），
# 不再写死在这里；忽略/黑名单统一走 apps 块（fpconfig.rules_for）。

USER32 = ctypes.windll.user32
GDI32 = ctypes.windll.gdi32

# Coordinate math must use PHYSICAL pixels: a DPI-unaware process sees a
# virtualized screen width (e.g. 2048 instead of 2560 at 125% scaling), which
# makes the "off-screen" park position land inside the visible area.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        USER32.SetProcessDPIAware()
    except Exception:
        pass


sys.path[:0] = [str(Path(__file__).resolve().parents[1]),            # pc/
                str(Path(__file__).resolve().parents[1] / "core")]   # 公共件 pclog/fp_feedback/alert_fallback
import pclog
# 日志服务名沿用 "wechat_vision"：watchdog 的 quiet 阈值与「脚本名→日志名」
# 映射（watchdog.py:75,85）、以及告警文案里的日志路径都认这个名字，
# 改名要同步那三处，这里先不动。
LOG = pclog.get_logger("wechat_vision")


def log(msg):
    pclog.log_auto(LOG, msg)


def _log_crash(exc_type, exc, tb):
    import traceback
    log("FATAL " + "".join(traceback.format_exception(exc_type, exc, tb)).strip())
    os._exit(1)


sys.excepthook = _log_crash


# 窗口清单 + 运行参数统一经 fpconfig 读 fp.config.json（注释剥离 / 校验 /
# 启用过滤都在那边）；problems 交给这里的 logger —— 与原先 _load_targets
# 的日志行为一致：没配置是显式一行日志，不是静默空转。
# 注意 fpconfig.vision() 的返回顺序是 (settings, targets, problems)
SETTINGS, TARGETS, _cfg_problems = fpconfig.vision()
for _p in _cfg_problems:
    log(_p)

# ---- settings 块派生的运行参数（默认值 = 原先写死的常量，行为不变）----
POLL_MIN = int(SETTINGS.get("poll_min", 30))              # 全局轮询间隔（分钟）
MY_NAME = SETTINGS.get("my_name", "你的昵称")          # 「@我」判定昵称
ACTIVE_FROM = int(SETTINGS.get("active_from", 8))         # 活跃时段起（含）
ACTIVE_TO = int(SETTINGS.get("active_to", 24))            # 活跃时段止（不含）
SEEN_TTL = int(SETTINGS.get("dedup_ttl_sec", 6 * 3600))   # 同一未读去重窗口
OFFSCREEN_X_OFFSET = int(SETTINGS.get("offscreen_offset", 120))
# 忽略/黑名单不在此派生：handle() 运行时经 fpconfig.rules_for(label) 读
# apps 块 —— 与闸门 ai_triager 消费同一份声明（单一配置，无第二份名单）。

# OCR/视觉模型接口已在文件顶部取好（AI = fpconfig.ai()，fp.config.json 单一
# 来源，与 ai_triager 共用），这里不再合并第二份。

# 睡眠间隔取「全局值与各目标 poll_min 的最小」——否则某目标配了更短间隔
# 也不会真的更早醒来（醒来后由目标级到期判断决定扫谁）。
POLL_SLEEP_MIN = min([POLL_MIN] + [int(t.get("poll_min") or POLL_MIN)
                                    for t in TARGETS.values()])


def _seen_load() -> dict:
    try:
        return json.loads(SEEN_STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        # state 坏掉 = 已推送指纹全丢 -> 下一轮把同一批消息再推一遍
        log(f"vision_state unreadable ({e!r}); treating all as new")
        return {}


def _seen_save(state: dict):
    now = time.time()
    state = {k: v for k, v in state.items() if now - v < SEEN_TTL}
    # P2-4：tmp+replace，和 triage_state / seen(通知) 对齐。裸 write_text 写
    # 一半断电会留半截 JSON，_seen_load 读坏返回 {} —— 下一轮把同一批未读
    # **重复推送**（6 小时内）。
    tmp = SEEN_STATE.with_name(f"{SEEN_STATE.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(SEEN_STATE)
    except Exception as e:
        # 保存失败 = 这一轮的去重白存 -> 重启/下轮会重复识别并重复推送
        log(f"vision_state save failed: {e!r}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _sig(app: str, chat: str, preview: str) -> str:
    return f"{app}|{chat}|{' '.join((preview or '').split())[:120]}"


def archive(rec):
    try:
        pclog.append_rotating(ARCHIVE, json.dumps(rec, ensure_ascii=False),
                              mode="rotate")
    except Exception as e:
        log(f"archive write failed: {e!r}")


def _exe_of(pid: int, cache: dict) -> str:
    """PID → exe 名（带缓存）。读不到（进程刚退/权限）得空串，等同不匹配。"""
    if pid not in cache:
        try:
            cache[pid] = psutil.Process(pid).name()
        except Exception:
            cache[pid] = ""
    return cache[pid]


def find_target(spec: dict, min_w: int | None = None):
    """按 fp.config.json vision 块的条目挑主窗口，返回 (hwnd, x, y, w, h) 或 None。

    三层匹配：process（PID→exe，必填）→ class（子串，可选）→ title
    （正则，可选）。同条件窗口按主窗口启发式挑：排除 WS_EX_TOOLWINDOW、
    子窗/owned 窗、无标题窗，取面积最大者。min_w 用于「最小化」二次探测
    （显式传 0 = 只要有窗就算）。
    """
    want_proc = (spec.get("process") or "").lower()
    want_cls = spec.get("class")
    want_title = spec.get("title")
    limit = spec.get("min_w", 400) if min_w is None else min_w
    rx = re.compile(want_title) if want_title else None
    best = None
    best_area = -1
    pid_exe: dict[int, str] = {}
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, lp):
        nonlocal best, best_area
        if not USER32.IsWindowVisible(hwnd):
            return True
        if USER32.GetWindowLongW(hwnd, -20) & 0x80:   # WS_EX_TOOLWINDOW
            return True
        if USER32.GetParent(hwnd):                    # 子窗 / owned 窗
            return True
        t = ctypes.create_unicode_buffer(256)
        USER32.GetWindowTextW(hwnd, t, 256)
        if not t.value:
            return True
        if want_proc:
            pid = wintypes.DWORD()
            USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if _exe_of(pid.value, pid_exe).lower() != want_proc:
                return True
        if want_cls:
            c = ctypes.create_unicode_buffer(64)
            USER32.GetClassNameW(hwnd, c, 64)
            if want_cls not in c.value:
                return True
        if rx and not rx.search(t.value):
            return True
        r = wintypes.RECT()
        USER32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w <= limit:
            return True
        area = w * h
        if area > best_area:
            best_area = area
            best = (hwnd, r.left, r.top, w, h)
        return True

    USER32.EnumWindows(CB(cb), 0)
    return best


def park_needed(hwnd) -> bool:
    """True when any part of the window overlaps a monitor's work area."""
    rect = wintypes.RECT()
    if not USER32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    overlapped = [False]
    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]
    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong, ctypes.c_ulong,
                            ctypes.POINTER(RECT), ctypes.c_double)

    def cb(hmon, hdc, lprc, data):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        USER32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        w = mi.rcWork
        if (rect.left < w.r and rect.right > w.l
                and rect.top < w.b and rect.bottom > w.t):
            overlapped[0] = True
        return True

    USER32.EnumDisplayMonitors(0, None, CB(cb), 0)
    return overlapped[0]


def move_offscreen(hwnd, w, h):
    """Park the window just beyond the right edge of the rightmost monitor."""
    right_edge = USER32.GetSystemMetrics(0)  # primary width; extend below if multi-monitor
    overlapped = [right_edge]
    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                    ("r", ctypes.c_long), ("b", ctypes.c_long)]
    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong, ctypes.c_ulong,
                            ctypes.POINTER(RECT), ctypes.c_double)

    def cb(hmon, hdc, lprc, data):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        USER32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        if mi.rcMonitor.r > overlapped[0]:
            overlapped[0] = mi.rcMonitor.r
        return True

    USER32.EnumDisplayMonitors(0, None, CB(cb), 0)
    USER32.SetWindowPos(hwnd, 0, overlapped[0] + OFFSCREEN_X_OFFSET, 100,
                        w, h, 0x0004)


def capture(hwnd) -> tuple[bytes, int, int] | None:
    rect = wintypes.RECT()
    USER32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    hdc = USER32.GetWindowDC(hwnd)
    mem = GDI32.CreateCompatibleDC(hdc)
    bmp = GDI32.CreateCompatibleBitmap(hdc, w, h)
    GDI32.SelectObject(mem, bmp)
    ok = USER32.PrintWindow(hwnd, mem, 2)

    class BMIH(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]
    bmi = BMIH(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    GDI32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    GDI32.DeleteObject(bmp)
    GDI32.DeleteDC(mem)
    USER32.ReleaseDC(hwnd, hdc)
    return (buf.raw, w, h) if ok else None


def bgra_to_png(bgra: bytes, w: int, h: int) -> bytes:
    """BGRA 原始数据 -> PNG bytes（给云端/本地OCR模型视觉 API 上传）。

    Pillow（C 实现）替代了原来的纯 Python 逐像素循环：800x600 实测
    Pillow **5.0ms** vs 纯 Python **172.3ms = 34.4x**，两条路径输出逐像素
    一致（第 4 轮实测；docstring 原来写的「356.8ms → 146.0ms（2.4x）」是
    切片版的旧数据，那版代码从未执行过，留着会误导）。输出大小不变
    （1408 vs 1407 KB —— 截图要走公网上传给 API，压缩率不能降）。
    纯红/纯绿/纯蓝三色解码校验过两条路径输出逐像素一致，通道顺序
    （BGRA -> RGB）没有错位。Pillow 不可用时回退原实现，vision 不会因
    缺依赖停摆。
    """
    try:
        from PIL import Image
        import io as _io
        # raw decoder：**必须是 "BGRX"**，不是 "BGRA"。
        # Image.frombytes("RGB", ..., "raw", "BGRA", 0, 1) 在 Pillow 12.3.0
        # 上直接抛 ValueError: unknown raw mode for given image mode —— 这行
        # 之所以一直没炸，只是因为它被下面那个同名 def 覆盖、从没执行过
        # （第 4 轮把两层衔接起来时第一次真正跑它才发现）。X = 忽略 alpha，
        # 正是我们要的：4 字节/像素、一步解码出 RGB、全程 C。
        # BGRX 实测解码 (30,20,10) = R,G,B，通道顺序没反。
        img = Image.frombytes("RGB", (w, h), bgra, "raw", "BGRX")
        out = _io.BytesIO()
        img.save(out, "PNG", compress_level=6)
        return out.getvalue()
    except ImportError:
        pass  # Pillow 缺失：走下面的纯 Python 实现
    # 注意：原来这里**没有 return**，而且下面那个同名 def 把整个函数覆盖了 ——
    # 结果是 Pillow 那条 C 快路径从来没执行过（docstring 里的 2.4x 优化一次
    # 都没生效），每张截图都在走纯 Python 逐像素循环；而「except ImportError:
    # pass」落到函数尾部会 return None，让 analyze() 里 b64encode(None) 炸掉。
    # 第 4 轮把纯实现改名成 fallback，两层真正衔接起来。
    return _bgra_to_png_pure(bgra, w, h)


def _bgra_to_png_pure(bgra: bytes, w: int, h: int) -> bytes:
    """纯 Python fallback（Pillow 缺失时）。输出与 Pillow 路径逐像素一致。"""
    import struct
    import zlib
    rows = []
    for y in range(h):
        row = bgra[y * w * 4:(y + 1) * w * 4]
        rgb = bytearray(b"\x00")
        for i in range(0, len(row), 4):
            rgb += bytes((row[i + 2], row[i + 1], row[i]))
        rows.append(bytes(rgb))

    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
    png += chunk(b"IEND", b"")
    return png


def _prompt_for(label: str, hint: str) -> str:
    """每个目标一条 prompt：label / prompt_hint 来自 vision 块，
    界面长什么样由配置里那句 hint 告诉模型，不再写死微信的会话列表布局。"""
    hint = hint or "识别界面上的未读提示（红点/数字徽标/加粗条目）"
    return f"""你是消息分诊助手，分析 PC 端应用主窗口截图。
应用：{label}。界面说明：{hint}

任务：
1. 找出所有有未读提示的会话/条目（红色数字徽标或加粗），记录：名字、未读数、预览文字
2. 特别注意消息预览中是否包含"@所有人"或"@{MY_NAME}"——这类消息必须标记为重要
3. 其他判重要标准：家人/紧急联系人来信、日程提醒、账户安全、服务异常需要处理
4. 群聊普通闲聊、营销、系统通知、转账回执 → 忽略

只输出 JSON（不要多余文字）：
{{"app": "{label}", "unread": [{{"chat": "名字", "count": 数字, "preview": "预览", "at_all": true/false, "at_me": true/false}}], "important": [{{"chat": "名字", "reason": "10字以内"}}]}}
无未读时：{{"app": "{label}", "unread": [], "important": []}}"""


def analyze(bgra, w, h, key: str, label: str, prompt: str) -> dict | None:
    png = bgra_to_png(bgra, w, h)
    b64 = base64.b64encode(png).decode()
    try:
        r = httpx.post(
            AI["base_url"].rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {AI['api_key']}"},
            timeout=90,
            trust_env=False,  # never route our traffic through the system proxy
            json={
                "model": AI["model"],
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {"type": "text", "text": f"分诊这张{label}截图"},
                    ]},
                ],
                "max_completion_tokens": AI.get("max_completion_tokens", 600),
                "thinking": {"type": "disabled"},
            })
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        return json.loads(text)
    except Exception as e:
        log(f"analyze error [{key}]: {e!r}")
        return None


def push(title, message, priority=4, topic="fp-pc"):
    # trace 由 main() 的每轮扫描创建，这里沿用它 —— 截图/识别/推送/归档
    # 同一轮共享一个 id，tags_with_trace 在无 trace 时会自己补一个。
    # P2-3：topic 默认 fp-pc（走 AI 分诊）；采集停摆告警直推 fp-phone，
    # 不给二道闸把「监控自己坏了」判成「忽略」的机会。
    try:
        r = httpx.post(NTFY_BASE + "/", timeout=10, trust_env=False, json={
            "topic": topic, "title": title[:120], "message": message[:500],
            "priority": priority,
            "tags": pclog.tags_with_trace(["bell"]),
        }, headers=_auth())
        if r.status_code == 200:
            return True
        # 非 200（403 token 失效/500）必须留 ERROR：watchdog.check_link 只数
        # [ERROR]，静默 False 会让 FAULT_ALERT 推送失败零痕迹、link+quiet 双绿
        log(f"ntfy push failed: HTTP {r.status_code} topic={topic} "
            f"title={title[:40]}")
        return False
    except Exception as e:
        log(f"ntfy error: {e!r}")
        return False


def is_active_hours() -> bool:
    return ACTIVE_FROM <= datetime.now().hour < ACTIVE_TO


def scan_app(key: str, spec: dict) -> tuple[str, dict | None]:
    """P2-3：返回 (状态, 结果)。原来任何失败都 return None，调用方却无条件
    scanned += 1 —— 窗口找不到七轮照样心跳「2/2 apps scanned」，watchdog 的
    quiet/link 双绿（2026-09-23 实测 08:35-11:35 连续 7 轮 window not found
    而三项全绿，直到人工翻日志才发现）。

    状态：
      ok        —— 截图+识别完成，result 为 dict（unread 可能为空）
      closed    —— 三层匹配都找不到窗 = 应用没开（没消息可漏，不算故障）
      minimized —— 窗口在但太窄（min_w=0 找得到、min_w 找不到）= 最小化，
                   采集不了（故障态：应用在跑但瞎了）
      capture   —— PrintWindow 失败（故障态）
      analyze   —— 识别失败（故障态，analyze 内部已 log error）
    """
    label = spec.get("label", key)
    hint = spec.get("prompt_hint", "")
    # 目标级 prompt 字段 = 整段覆盖默认模板；没写就按 label+prompt_hint 拼
    prompt = spec.get("prompt") or _prompt_for(label, hint)
    info = find_target(spec)
    if not info:
        # 三态区分就在这二次探测：min_w=0 能不能找到
        if find_target(spec, min_w=0):
            log(f"[{key}] window too narrow (minimized?), skip")
            return "minimized", None
        # 措辞避开 pclog 的 `not found`→ERROR 规则：应用没开是良性态，
        # 记 ERROR 会污染错误统计（每 30 分钟一条假错误）
        log(f"[{key}] 窗口未开启（应用未运行），本轮不扫")
        return "closed", None
    hwnd, x, y, w, h = info
    if park_needed(hwnd):  # any part visible → park it
        move_offscreen(hwnd, w, h)
    cap = capture(hwnd)
    if not cap:
        log(f"[{key}] capture failed")
        return "capture", None
    result = analyze(*cap, key, label, prompt)
    if result is None:
        return "analyze", None
    result["_app"] = key
    result["_label"] = label
    return "ok", result


def handle(result: dict):
    app = result.get("_app", "?")
    label = result.get("_label", app)
    # 声明式来源规则（apps 块）：ignore = 整源跳过；blacklist = 会话级忽略。
    # 与闸门 ai_triager 消费同一份声明（fpconfig.rules_for），单一配置。
    rules = fpconfig.rules_for(label)
    if rules.get("ignore"):
        log(f"[{app}] apps 声明 ignore，跳过本条")
        return
    ign = set(rules.get("blacklist") or [])
    unread = result.get("unread", [])
    important = result.get("important", [])
    archive({"ts": time.time(), "app": app, "unread": unread,
             "important": important})
    log(f"[{app}] {len(unread)} unread chats, {len(important)} important")

    # merge AI's important list with our hard rules (@所有人/@我/text直推)
    # 黑名单判定与闸门一致用子串（闸门对 probe 是子串；这里精确等值会漏
    # 「公众号精选」这类带前后缀的会话名）
    flagged = {i.get("chat"): i for i in important
               if not any(b and b in (i.get("chat") or "") for b in ign)}
    for u in unread:
        chat = u.get("chat", "")
        preview = u.get("preview", "")
        if chat in flagged or any(b and b in chat for b in ign):
            continue
        if re.search(r"(?<![a-zA-Z])text(?![a-zA-Z])", preview, re.I):
            flagged[chat] = {"chat": chat, "reason": "测试直推"}
            continue
        if u.get("at_all") or u.get("at_me"):
            flagged[chat] = {"chat": chat,
                             "reason": "@所有人" if u.get("at_all") else "@我"}

    # A chat stays "unread" until the user opens it, so without this the same
    # message would be re-reported every poll (30 min) forever. Report a given
    # (chat, preview) once, then stay quiet until the preview actually changes.
    seen = _seen_load()
    now = time.time()
    fresh = {}
    for chat, info in flagged.items():
        preview = next((u.get("preview", "") for u in unread
                        if u.get("chat") == chat), "")
        sig = _sig(app, chat, preview)
        if now - seen.get(sig, 0) < SEEN_TTL:
            log(f"[{app}] {chat} 与上次相同，跳过重复推送")
            continue
        seen[sig] = now
        fresh[chat] = (info, preview)
    _seen_save(seen)

    failed_sigs = []
    for chat, (info, preview) in fresh.items():
        reason = info.get("reason", "")
        ok = push(f"{label}: {chat}", f"{preview}\n— {reason}", priority=4)
        log(f"{'PUSHED' if ok else 'PUSH_FAIL'} [{label}:{chat}] {reason}")
        if not ok:
            # push 失败必须回滚 seen —— 上面已经把 sig 记成「已上报」并落盘
            # 了，不回滚的话这个 (chat, preview) 在 SEEN_TTL=6h 内不再重试，
            # 这条未读最多静默 6 小时。ai_triager 的语义正是「推送失败就
            # pop 掉快照」，两边对齐（第 5 轮）。
            failed_sigs.append(_sig(app, chat, preview))
    if failed_sigs:
        for sig in failed_sigs:
            seen.pop(sig, None)
        _seen_save(seen)


def main():
    log(f"vision listener: poll={POLL_MIN}min(sleep {POLL_SLEEP_MIN}min), "
        f"active {ACTIVE_FROM}:00-{ACTIVE_TO}:00, ai={AI.get('model', '未配置')}, "
        f"targets={list(TARGETS) or '无 —— 检查 fp.config.json 的 vision 块'}")
    for key, spec in TARGETS.items():
        info = find_target(spec)
        if info:
            hwnd, x, y, w, h = info
            if park_needed(hwnd):
                move_offscreen(hwnd, w, h)
            log(f"[{key}] hwnd={hwnd} parked")
        else:
            log(f"[{key}] 启动时窗口未开启（应用未运行），后续每轮继续探测")

    # P2-3：连续 2 轮「应用在跑但采不了」直推手机 —— 必须放 while 外面，
    # 否则每轮重置、永远到不了 2
    fault_streak: dict[str, int] = {}
    fault_alerted: set[str] = set()
    last_scan: dict[str, float] = {}   # 目标级到期判断：上次实际扫描的时刻
    while True:
        if not is_active_hours():
            log("night quiet hours, sleeping 10min")
            time.sleep(600)
            continue
        # P2-9：一口气睡满 30min 时，夜间最后一行（如 07:55）到 08:00 换挡
        # 后的首个心跳之间会隔 600+1800+两段 90s 识别 ≈ 2580s，离 quiet 阈值
        # 2700s 只剩 2 分钟，AI 稍慢每天早上误报。分段睡：每 300s 一条心跳，
        # 最大相邻间隔降到 300s + 一轮扫描。步长按最小间隔（含目标级）算。
        slept = 0
        while slept < POLL_SLEEP_MIN * 60:
            time.sleep(300)
            slept += 300
            log(f"sleep heartbeat: {slept}/{POLL_SLEEP_MIN * 60}s")
        scanned = 0
        cycle_at = time.time()
        for app, cfg in TARGETS.items():
            # apps 声明 ignore 的来源直接跳过 —— 不截图不调视觉模型（handle()
            # 里的同名检查保留为第二道闸，这里是省钱前置）
            if fpconfig.rules_for(cfg.get("label", app)).get("ignore"):
                log(f"[{app}] apps 声明 ignore，本轮不扫")
                continue
            # 目标级 poll_min：睡眠已按最小间隔醒，这里只放行到期的目标
            if cycle_at - last_scan.get(app, 0.0) < int(cfg.get("poll_min") or POLL_MIN) * 60:
                continue
            last_scan[app] = cycle_at
            # 每轮每个 app 一个 trace：截图、识别、推送、归档全用同一个 id，
            # 出问题时 grep 一次就能拉出这一轮的完整过程
            pclog.set_trace_id(None)
            try:
                state, result = scan_app(app, cfg)
                if state == "ok":
                    handle(result)
                    scanned += 1          # P2-3：只数真正扫完的（原来失败也 +1）
                    fault_streak[app] = 0
                    fault_alerted.discard(app)
                elif state == "closed":
                    fault_streak[app] = 0      # 没开应用：不是故障，别误报
                    fault_alerted.discard(app)
                else:                          # minimized / capture / analyze
                    fault_streak[app] = fault_streak.get(app, 0) + 1
                    n = fault_streak[app]
                    if n >= 2 and app not in fault_alerted:
                        # 故障态直推 fp-phone（不经 AI 二道闸）；推失败不记
                        # alerted，下一轮（30min）重试
                        if push(f"[FxxkPush] {app} 采集停摆",
                                f"连续 {n} 轮采集失败（{state}）：视觉分诊静默停摆，"
                                f"该应用的新消息不会推手机。\n"
                                f"排查: pc/logs/wechat_vision.log；窗口别最小化，"
                                f"拖到屏外即可（见「拖走聊天窗口.bat」）。",
                                priority=4, topic="fp-phone"):
                            fault_alerted.add(app)
                            log(f"FAULT_ALERT [{app}] state={state} streak={n} -> phone")
            except Exception as e:
                log(f"[{app}] cycle error: {e!r}")
                # 异常也要进故障计数：否则「一直抛错但心跳照打」永远无人发现
                # （与 P2-3 修的「失败照样绿」同型，只是入口从状态改成了异常）
                fault_streak[app] = fault_streak.get(app, 0) + 1
                n = fault_streak[app]
                if n >= 2 and app not in fault_alerted:
                    if push(f"[FxxkPush] {app} 采集异常",
                            f"连续 {n} 轮扫描异常（{type(e).__name__}）：视觉分诊停摆，"
                            f"该应用的新消息不会推手机。\n"
                            f"排查: pc/logs/wechat_vision.log",
                            priority=4, topic="fp-phone"):
                        fault_alerted.add(app)
                        log(f"FAULT_ALERT [{app}] cycle-error streak={n} -> phone")
            finally:
                pclog.set_trace_id("-")   # 不把 trace 带到下一轮/静默期日志
        # r7-10：一轮扫完（有无新消息都算）留一条心跳 —— 原来「正常但没新消息」
        # 零日志，和「截图失败但被吞 / 循环卡死」同形。watchdog 的 quiet 检查
        # （阈值 2700s）靠这条判断 30min 轮询还活着。
        log(f"idle heartbeat: cycle done, {scanned}/{len(TARGETS)} apps scanned")


def park_main() -> None:
    """一次性归位工具（原独立的 park_windows.py，已并入本文件）。

    用法: python vision_listener.py park
    把 fp.config.json vision 块里配置的窗口挪到屏幕外 —— 重启/重开 App 后手动
    跑一次，窗口就不会坐在屏幕上。已在屏外的不动；对显示/分辨率/缩放变化免疫。
    """
    delay, retry = 3, 3
    for attempt in range(retry):
        found_any = False
        for key, spec in TARGETS.items():
            info = find_target(spec)
            if not info:
                print(f"[{key}] 窗口未找到（没开或托盘中），跳过")
                continue
            hwnd, x, y, w, h = info
            if park_needed(hwnd):
                move_offscreen(hwnd, w, h)
                print(f"[{key}] 已挪到屏幕外（显示器右边缘之外）")
            else:
                print(f"[{key}] 已在屏幕外，无需处理")
            found_any = True
        if found_any or attempt == retry - 1:
            break
        print(f"窗口还没就绪，{delay}s 后重试 ({attempt + 1}/{retry})...")
        time.sleep(delay)
    try:
        input("\n完成，按回车关闭...")
    except EOFError:
        pass


def list_main() -> None:
    """列出可见顶层窗口（pid/exe/类名/标题/尺寸）—— 填 fp.config.json vision 块用。

    用法: python vision_listener.py list
    """
    rows = []
    pid_exe: dict[int, str] = {}
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, lp):
        if not USER32.IsWindowVisible(hwnd):
            return True
        t = ctypes.create_unicode_buffer(256)
        USER32.GetWindowTextW(hwnd, t, 256)
        if not t.value:
            return True
        pid = wintypes.DWORD()
        USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        cls = ctypes.create_unicode_buffer(64)
        USER32.GetClassNameW(hwnd, cls, 64)
        r = wintypes.RECT()
        USER32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        rows.append((w * h, pid.value, _exe_of(pid.value, pid_exe),
                     cls.value, t.value, w, h))
        return True

    USER32.EnumWindows(CB(cb), 0)
    rows.sort(reverse=True)
    print(f"{'pid':>7}  {'exe':<20} {'class':<28} {'WxH':>11}  title")
    for area, pid, exe, cls, title, w, h in rows:
        print(f"{pid:>7}  {exe:<20} {cls:<28} {w:>4}x{h:<5}  {title[:60]}")
    print(f"\n共 {len(rows)} 个可见顶层窗口（按面积降序）。")
    print("填 fp.config.json 的 vision 块：process=exe 全名（必填），"
          "class=类名子串、title=标题正则（都可选），label/prompt_hint 给 AI 用。")
    try:
        input("\n按回车关闭...")
    except EOFError:
        pass


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("park", "--park"):
        park_main()
    elif len(sys.argv) > 1 and sys.argv[1] in ("list", "--list"):
        list_main()
    else:
        main()
