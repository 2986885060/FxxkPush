#!/usr/bin/env python3
"""FuckPush Windows notification listener.

Polls UserNotificationListener for new toast notifications from IM apps
(QQ/微信/钉钉/TIM/学习通/企业微信...), dedupes by notification id, and:
  1. publishes each to ntfy topic fp-pc  (for AI triage later)
  2. archives locally to pc/notifications.jsonl (silent archive)

Runs as a long-lived process. New/unknown apps are captured too and
marked app_unknown=true so the AI layer can learn them.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
from winrt.windows.ui.notifications import NotificationKinds
from winrt.windows.ui.notifications.management import (
    UserNotificationListener,
    UserNotificationListenerAccessStatus,
)

# ---------- config ----------
NTFY_BASE = os.environ.get("FP_NTFY_URL", "http://127.0.0.1:2586")  # via SSH tunnel (see pc/ntfy_tunnel.py)
_SECRET = Path(__file__).parent / "ntfy.secret"
try:
    NTFY_TOKEN = os.environ.get("FP_NTFY_TOKEN") or (
        _SECRET.read_text().strip() if _SECRET.exists() else "")
except Exception:
    # 导入期读取包 try：AV 在 exists() 和 read_text() 之间独占一下就足以
    # 让进程在 excepthook 挂上之前无声退出（pythonw 下连日志都没有）。
    # 空 token 先跑起来，_auth() 不带头、服务端回 401，上层的
    # `ntfy publish failed` 日志会留痕（第 5 轮同型问题）。
    NTFY_TOKEN = ""
TOPIC = "fp-pc"
POLL_SEC = 3
ARCHIVE = Path(__file__).parent / "notifications.jsonl"
STATE = Path(__file__).parent / "listener_state.json"

# apps worth watching; everything else is captured but flagged unknown
WATCHLIST = {"QQ", "微信", "WeChat", "钉钉", "DingTalk", "TIM", "学习通",
             "企业微信", "WeCom"}
# system noise to skip entirely
IGNORE = {"Windows.Defender.SecurityCenter", "无线", "AMD Software",
          "Microsoft Store", "Settings", "Windows 安全中心"}

# Our OWN toasts (popped by pc_subscriber) come back through the notification
# API and would be re-triaged and re-pushed to the phone -> feedback loop.
# Skip anything we produced ourselves.
SELF_MARKERS = {"fuckpush", "fxxkpush", "winotify", "python", "pythonw"}
# pc_subscriber writes every toast it pops to this file; Windows hands the same
# toast back to us as a notification a moment later (app name resolves to "?"),
# so match on content instead of app identity.
ECHO_FILE = Path(__file__).parent / "toast_echo.jsonl"
ECHO_WINDOW = 180  # seconds


def _auth() -> dict:
    """鉴权头。**空 token 时不带头**，且每次取不到都会重试读文件。

    空 ``Bearer `` 是非法头值 → httpx 抛 LocalProtocolError（TransportError，
    没有 .response），except 接得住但日志里只有一行不知所云的异常；不带头
    则服务端回 401/403，`ntfy publish failed: ... 401` 一眼能看懂（第 5 轮
    在 ai_triager 上确认的同型问题，其余三个服务同批修）。
    """
    global NTFY_TOKEN
    if not NTFY_TOKEN:
        try:
            NTFY_TOKEN = (os.environ.get("FP_NTFY_TOKEN")
                          or (_SECRET.read_text().strip()
                              if _SECRET.exists() else ""))
        except Exception:
            return {}
    return {"Authorization": f"Bearer {NTFY_TOKEN}"} if NTFY_TOKEN else {}


def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def is_our_toast(texts) -> bool:
    """True when these texts match a toast we popped ourselves recently."""
    if not ECHO_FILE.exists():
        return False
    incoming = _norm(" ".join(texts))[:200]
    if not incoming:
        return False
    cutoff = time.time() - ECHO_WINDOW
    try:
        # 尾部读取：echo 文件是 append-only 无限增长，每次通知都全量读一遍
        # 和 watchdog.check_link 同一个毛病
        for line in pclog.read_tail(ECHO_FILE, 60):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                # 单行损坏（写一半被杀 / 编码坏）不该让整个回环检测失效：
                # 原来这里会冒泡到外层 except -> return False，之后窗口期内
                # 自己弹出的 toast 全被当成新通知，直接重推一遍。
                continue
            if rec.get("ts", 0) < cutoff:
                continue
            fp = _norm(f'{rec.get("title", "")} {rec.get("msg", "")}')[:200]
            if fp and (fp == incoming or fp in incoming or incoming in fp):
                return True
    except Exception:
        return False
    return False


import pclog
LOG = pclog.get_logger("notification_listener")


def log(msg):
    pclog.log_auto(LOG, msg)


def _log_crash(exc_type, exc, tb):
    import traceback
    log("FATAL " + "".join(traceback.format_exception(exc_type, exc, tb)).strip())
    os._exit(1)


sys.excepthook = _log_crash


def load_seen():
    if STATE.exists():
        try:
            return set(json.loads(STATE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen(seen, cap=2000):
    """落盘并按 cap 截断（就地修改传入的 set）。

    原实现 ``seen = set(sorted(seen)[-cap:])`` 只给局部变量重新绑定，外面的
    set 纹丝不动 —— 截断只在写盘那一瞬生效，运行期间内存里的 seen 一直涨。
    而且 set 无序、通知 id 是数字字符串，按字典序取"最后 2000 个"拿到的是
    ['9','89','8'] 这种而不是最新的那批：截错方向会把仍在通知中心的 id 丢掉，
    下一轮它们又被当成新通知重推一遍。
    """
    if len(seen) > cap:
        def _key(x):
            try:
                return (1, int(x))     # 通知 id 单调递增，数值最大 = 最新
            except ValueError:
                return (0, x)
        keep = sorted(seen, key=_key)[-cap:]
        seen.clear()
        seen.update(keep)
    STATE.write_text(json.dumps(sorted(seen)), encoding="utf-8")


def extract(n) -> dict | None:
    """Parse one UserNotification into our event dict."""
    try:
        app = n.app_info.display_info.display_name
    except Exception:
        app = "?"
    if app in IGNORE:
        return None
    if app and any(m in app.lower() for m in SELF_MARKERS):
        return None  # our own toast -> never re-triage
    # texts 必须先给默认值：get_binding 抛异常、或 binding 为 None（非
    # ToastGeneric 的通知）时它压根没被赋值，下面 is_our_toast(texts) 会
    # NameError —— 而这发生在 poll_once 的 for 循环里，一炸整轮剩余通知
    # 全部丢失，只在 main 的 except 里留一行 "poll error"（日志至今 0 次，
    # 属于还没被触发的定时炸弹）。
    texts = []
    try:
        binding = n.notification.visual.get_binding("ToastGeneric")
        if binding:
            texts = [t.text for t in binding.get_text_elements() if t.text]
    except Exception:
        pass
    if is_our_toast(texts):
        return None  # echo of a toast pc_subscriber popped -> do not re-triage
    return {
        "id": str(n.id),
        "app": app,
        "texts": texts,
        "ts": time.time(),
        "watched": app in WATCHLIST,
    }


async def poll_once(listener, seen) -> list[dict]:
    events = []
    notifs = await listener.get_notifications_async(NotificationKinds.TOAST)
    for i in range(notifs.size):
        n = notifs.get_at(i)
        if str(n.id) in seen:
            continue
        ev = extract(n)
        if ev is None:
            seen.add(str(n.id))  # remember ignored ones too
            continue
        events.append(ev)
        seen.add(str(n.id))
    return events


def publish(ev: dict):
    """Send to ntfy (fp-pc) + local archive."""
    # 整条处理（组包 -> 发送 -> 归档 -> 日志）都放在 with 里：链路起点的
    # 这几行日志必须带上本条消息的 trace，否则从 listener 这端就断了。
    with pclog.trace(None):
        title = f"{ev['app']}: {ev['texts'][0][:60]}" if ev["texts"] else f"{ev['app']} 通知"
        body = "\n".join(ev["texts"]) or "<no text>"
        payload = {
            "topic": TOPIC,
            "title": title[:120],
            "message": body[:500],
            "priority": 3 if ev["watched"] else 1,
            "tags": pclog.tags_with_trace(["bell"] if ev["watched"] else ["question"]),
        }
        try:
            r = httpx.post(NTFY_BASE + "/", json=payload,
                           headers=_auth(),
                           timeout=10, trust_env=False)
            ok = r.status_code == 200
        except Exception as e:
            log(f"ntfy publish failed: {e!r}")
            ok = False
        ev["pushed"] = ok
        try:
            pclog.append_rotating(ARCHIVE, json.dumps(ev, ensure_ascii=False),
                                  mode="rotate")
        except Exception as e:
            log(f"archive write failed: {e!r}")
        log(f"{'PUSH' if ok else 'ARCH'} [{ev['app']}] {' | '.join(ev['texts'])[:80]}")


async def main():
    listener = UserNotificationListener.current
    access = await listener.request_access_async()
    if access != UserNotificationListenerAccessStatus.ALLOWED:
        log("notification access DENIED — run probe_notifications.py first")
        return 1
    log(f"notification access OK, polling every {POLL_SEC}s -> topic {TOPIC}")

    seen = load_seen()
    log(f"loaded {len(seen)} known notification ids")

    # first pass marks existing notifications as seen (no flood on startup)
    for ev in await poll_once(listener, seen):
        pass  # discarded: pre-existing
    save_seen(seen)
    log("startup snapshot marked as seen; listening for NEW notifications")

    while True:
        try:
            before = len(seen)
            events = await poll_once(listener, seen)
            for ev in events:
                publish(ev)
            # 按 seen 是否变化来存，不能只看 events：extract 返回 None 的
            # 通知（IGNORE 名单 / 自己弹的 toast）照样 add 了 id 却不产出
            # events。只在 events 非空时存盘，这批 id 下一轮又是"新的"，
            # 每 3 秒重新 extract 一遍（含读 echo 文件）。
            if events or len(seen) != before:
                save_seen(seen)
        except Exception as e:
            log(f"poll error: {e!r}")
        await asyncio.sleep(POLL_SEC)


if __name__ == "__main__":
    asyncio.run(main())
