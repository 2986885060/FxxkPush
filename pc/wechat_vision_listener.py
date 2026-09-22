#!/usr/bin/env python3
"""FxxkPush vision listener v0.3 — WeChat + WeCom (企业微信).

Every POLL minutes, screenshots both apps' main windows (parked
off-screen) and sends them to MiMo vision for triage.

Important rules (user-defined):
  - @所有人 or @我 in any chat  -> important
  - 家人联系/日程/账户安全/服务异常 -> important
  - everything else -> silent archive

Schedule: active 08:00-23:59, quiet 00:00-07:59.
"""
import base64
import os
import ctypes
import json
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import httpx

HERE = Path(__file__).parent
NTFY_BASE = os.environ.get("FP_NTFY_URL", "http://127.0.0.1:2586")  # via SSH tunnel (see pc/ntfy_tunnel.py)
try:
    NTFY_TOKEN = ((HERE / "ntfy.secret").read_text().strip()
                  if (HERE / "ntfy.secret").exists() else "")
    CFG = json.loads((HERE / "triage_config.json").read_text(encoding="utf-8"))
except Exception:
    # 两个导入期读取都包进来：AV 独占 secret、或 triage_config.json 被手改
    # 坏 —— 都发生在 excepthook 挂上之前，pythonw 下会无声退出（第 5 轮
    # 同型问题，这是 4 处里的第 4 处）。CFG 给空 dict，下面全部用 .get 带
    # 默认值，不会因此 KeyError。
    NTFY_TOKEN = ""
    CFG = {}


def _auth() -> dict:
    """鉴权头。**空 token 时不带头** + 每次取不到都重试读文件。

    空 ``Bearer `` 是非法头值 → httpx LocalProtocolError（TransportError，
    没有 .response）；不带头则服务端回 401，push() 的 except 接得住、
    留日志（第 5 轮在 ai_triager 确认的同型问题）。
    """
    global NTFY_TOKEN
    if not NTFY_TOKEN:
        try:
            p = HERE / "ntfy.secret"
            NTFY_TOKEN = p.read_text().strip() if p.exists() else ""
        except Exception:
            return {}
    return {"Authorization": f"Bearer {NTFY_TOKEN}"} if NTFY_TOKEN else {}
ARCHIVE = HERE / "vision_log.jsonl"
SEEN_STATE = HERE / "vision_state.json"
SEEN_TTL = 6 * 3600  # don't re-report the same unread content for 6h
POLL_MIN = CFG.get("wechat_poll_min", 30)
ACTIVE_FROM, ACTIVE_TO = 8, 24
OFFSCREEN_X_OFFSET = 120
MY_NAME = CFG.get("wechat_my_name", "哦莫")  # used to detect @我

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

TARGETS = {
    "wechat": {"class": "Qt51514", "title": None, "min_w": 400},
    "wecom": {"class": "WeWorkWindow", "title": "企业微信", "min_w": 500},
}

# chats never pushed regardless of AI verdict (system/low-value noise)
IGNORE_CHATS = {"微信支付", "公众号", "服务通知", "QQ邮箱提醒", "折叠的聊天",
                "应用提醒", "失物招领&寻物启事"}


import pclog
LOG = pclog.get_logger("wechat_vision")


def log(msg):
    pclog.log_auto(LOG, msg)


def _log_crash(exc_type, exc, tb):
    import traceback
    log("FATAL " + "".join(traceback.format_exception(exc_type, exc, tb)).strip())
    os._exit(1)


sys.excepthook = _log_crash


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


def find_window(cls_substr: str, title: str | None, min_w: int):
    wins = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, lp):
        c = ctypes.create_unicode_buffer(64)
        USER32.GetClassNameW(hwnd, c, 64)
        if cls_substr not in c.value:
            return True
        t = ctypes.create_unicode_buffer(128)
        USER32.GetWindowTextW(hwnd, t, 128)
        if title and title not in t.value:
            return True
        r = wintypes.RECT()
        USER32.GetWindowRect(hwnd, ctypes.byref(r))
        if (r.right - r.left) > min_w:
            wins.append((hwnd, r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    USER32.EnumWindows(CB(cb), 0)
    return wins[0] if wins else None


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
    """BGRA 原始数据 -> PNG bytes（给 MiMo 视觉 API 上传）。

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


VISION_PROMPT = f"""你是消息分诊助手，分析 PC 端聊天软件主窗口截图（左侧为会话列表）。

任务：
1. 找出左侧会话列表中所有有未读消息的聊天（红色数字徽标或加粗提示），记录：聊天名、未读数、预览文字
2. 特别注意消息预览中是否包含"@所有人"或"@{MY_NAME}"——这类消息必须标记为重要
3. 其他判重要标准：家人/紧急联系人来信、日程提醒、账户安全、服务异常需要处理
4. 群聊普通闲聊、营销、系统通知、转账回执 → 忽略

只输出 JSON（不要多余文字）：
{{"app": "<app>", "unread": [{{"chat": "名字", "count": 数字, "preview": "预览", "at_all": true/false, "at_me": true/false}}], "important": [{{"chat": "名字", "reason": "10字以内"}}]}}
无未读时：{{"app": "<app>", "unread": [], "important": []}}"""




def analyze(bgra, w, h, app: str) -> dict | None:
    png = bgra_to_png(bgra, w, h)
    b64 = base64.b64encode(png).decode()
    try:
        r = httpx.post(
            CFG["base_url"].rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {CFG['api_key']}"},
            timeout=90,
            trust_env=False,  # never route our traffic through the system proxy
            json={
                "model": CFG["model"],
                "messages": [
                    {"role": "system", "content": VISION_PROMPT.replace("<app>", app)},
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {"type": "text", "text": f"分诊这张{'企业微信' if app == 'wecom' else '微信'}截图"},
                    ]},
                ],
                "max_completion_tokens": 600,
                "thinking": {"type": "disabled"},
            })
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        return json.loads(text)
    except Exception as e:
        log(f"analyze error [{app}]: {e!r}")
        return None


def push(title, message, priority=4):
    # trace 由 main() 的每轮扫描创建，这里沿用它 —— 截图/识别/推送/归档
    # 同一轮共享一个 id，tags_with_trace 在无 trace 时会自己补一个。
    try:
        r = httpx.post(NTFY_BASE + "/", timeout=10, trust_env=False, json={
            "topic": "fp-pc", "title": title[:120], "message": message[:500],
            "priority": priority,
            "tags": pclog.tags_with_trace(["bell"]),
        }, headers=_auth())
        return r.status_code == 200
    except Exception as e:
        log(f"ntfy error: {e!r}")
        return False


def is_active_hours() -> bool:
    return ACTIVE_FROM <= datetime.now().hour < ACTIVE_TO


def scan_app(app: str, cfg: dict) -> dict | None:
    info = find_window(cfg["class"], cfg["title"], cfg["min_w"])
    if not info:
        log(f"[{app}] window not found (closed/tray?), skip")
        return None
    hwnd, x, y, w, h = info
    if park_needed(hwnd):  # any part visible → park it
        move_offscreen(hwnd, w, h)
    cap = capture(hwnd)
    if not cap:
        log(f"[{app}] capture failed")
        return None
    result = analyze(*cap, app)
    if result is None:
        return None
    result["_app"] = app
    return result


def handle(result: dict):
    app = result.get("_app", "?")
    label = "企业微信" if app == "wecom" else "微信"
    unread = result.get("unread", [])
    important = result.get("important", [])
    archive({"ts": time.time(), "app": app, "unread": unread,
             "important": important})
    log(f"[{app}] {len(unread)} unread chats, {len(important)} important")

    # merge AI's important list with our hard rules (@所有人/@我/text直推)
    flagged = {i.get("chat"): i for i in important if i.get("chat") not in IGNORE_CHATS}
    for u in unread:
        chat = u.get("chat", "")
        preview = u.get("preview", "")
        if chat in flagged or chat in IGNORE_CHATS:
            continue
        if "text" in preview.lower():
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
    log(f"vision listener v0.3: poll={POLL_MIN}min, active {ACTIVE_FROM}:00-{ACTIVE_TO}:00, targets={list(TARGETS)}")
    for app, cfg in TARGETS.items():
        info = find_window(cfg["class"], cfg["title"], cfg["min_w"])
        if info:
            hwnd, x, y, w, h = info
            if park_needed(hwnd):
                move_offscreen(hwnd, w, h)
            log(f"[{app}] hwnd={hwnd} parked")
        else:
            log(f"[{app}] not found at startup (will retry each cycle)")

    while True:
        if not is_active_hours():
            log("night quiet hours, sleeping 10min")
            time.sleep(600)
            continue
        time.sleep(POLL_MIN * 60)
        scanned = 0
        for app, cfg in TARGETS.items():
            # 每轮每个 app 一个 trace：截图、识别、推送、归档全用同一个 id，
            # 出问题时 grep 一次就能拉出这一轮的完整过程
            pclog.set_trace_id(None)
            try:
                result = scan_app(app, cfg)
                if result:
                    handle(result)
                scanned += 1
            except Exception as e:
                log(f"[{app}] cycle error: {e!r}")
            finally:
                pclog.set_trace_id("-")   # 不把 trace 带到下一轮/静默期日志
        # r7-10：一轮扫完（有无新消息都算）留一条心跳 —— 原来「正常但没新消息」
        # 零日志，和「截图失败但被吞 / 循环卡死」同形。watchdog 的 quiet 检查
        # （阈值 2700s）靠这条判断 30min 轮询还活着。
        log(f"idle heartbeat: cycle done, {scanned}/{len(TARGETS)} apps scanned")


if __name__ == "__main__":
    main()
