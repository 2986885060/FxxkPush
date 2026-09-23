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

# P1-2：推失败的通知挂在这里下一轮重试（3s 一轮、每轮只出队一条）。
# 失败最常发生在断网/隧道抖动时，而那恰恰是最需要送达的时刻 —— 原实现只写
# 一行日志，等于「失败即永久丢失，要人工从 jsonl 里捞」。
_pending: list[dict] = []
PENDING_CAP = 200
PENDING_TTL = 900         # 15 分钟还没推出去就放弃：更旧的通知没有打扰价值

# P0-2：watchdog 每轮写 logs/watchdog.hb，这里每 3 分钟看一眼 mtime。
# watchdog 不在它自己的 WATCHED 里（没法自我监控），日志里的 heartbeat 也
# 没有消费者 —— 它一死，四项巡检连同三层告警一起静默失效。
HB_FILE = Path(__file__).parent / "logs" / "watchdog.hb"
_hb_alert_at = 0.0
_seen_dirty = False       # save_seen 失败时置 True，下一轮继续重试落盘


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
    # P2-4：tmp+replace，和 triage_state / triage_rules 对齐。裸 write_text
    # 写一半断电会留半截 JSON，而 load_seen 读坏直接返回空集 —— 等于全部 id
    # 失忆，下一轮把通知中心里整批内容重推一遍。
    # 返回是否成功：调用方靠它维持「脏」标记继续重试，否则这批 id 再也没机会
    # 落盘（下轮 seen 长度不再变化，存盘条件就不成立了）。
    tmp = STATE.with_name(f"{STATE.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(sorted(seen)), encoding="utf-8")
        tmp.replace(STATE)
        return True
    except OSError as e:
        log(f"save_seen failed ({e!r}) —— 本批 id 未落盘，下轮重试")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


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
        return ok


def _enqueue(ev: dict) -> None:
    """进重发队列（满了就丢并留痕，不能悄悄吞）。"""
    ev.setdefault("_queued_at", time.time())
    if len(_pending) < PENDING_CAP:
        _pending.append(ev)
    else:
        log(f"pending 队列已满({PENDING_CAP})，丢弃 [{ev.get('app')}] "
            f"{' | '.join(ev.get('texts') or [])[:60]}")


def flush_pending() -> None:
    """P1-2：每轮出队一条重试，失败回队尾（不阻塞后面的），超时放弃。"""
    if not _pending:
        return
    ev = _pending.pop(0)
    age = time.time() - (ev.get("_queued_at") or 0)
    if age > PENDING_TTL or age < 0:
        log(f"drop pending [{ev.get('app')}] (>{PENDING_TTL}s): "
            f"{' | '.join(ev.get('texts') or [])[:60]}")
        return
    if not publish(ev):
        _enqueue(ev)


def replay_failed(cap: int = 20) -> None:
    """P1-2 的磁盘版：上次运行里 pushed:false 且还不太旧的，启动时补发一轮。

    内存队列只覆盖本次进程活着的时候；进程崩溃 / 机器重启时，断网窗口里推
    失败的那些只躺在 notifications.jsonl 里，从来没人回头看它（报告点名的
    「恢复后不补发」）。

    只读尾 60 行：补发本身会继续往同一个文件追加，读全量会变成自触发的
    无限重放。ttl 过滤同时挡住「跨多次重启重复补发同一条」——旧的失败行
    15 分钟后就自然过期了。
    """
    now = time.time()
    done = 0
    # r7-4：publish 每次调用都归档一条新记录 —— 失败那条 pushed:false 永久
    # 留在文件里，重试成功只**追加**一条 pushed:true，不回填也不作废旧记录。
    # 原实现只过滤 pushed is False，于是 15 分钟窗口内任何重启都会把已经成功
    # 推送过的那条再推一次（手机收到同一条两次）+ 归档重复行。这里先按 id
    # 建索引、同 id 只认**时间最新**那条的 pushed，旧的 false 行自动被 true
    # 行压掉。read_tail 是文件序，后面的更新，直接覆盖即可。
    latest: dict[str, dict] = {}
    for ln in pclog.read_tail(ARCHIVE, 60):
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        rid = str(rec.get("id", ""))
        if rid:
            latest[rid] = rec
    for rec in latest.values():
        if rec.get("pushed") is not False:
            continue
        age = now - (rec.get("ts") or 0)
        if age > PENDING_TTL or age < 0:
            continue      # 太旧不补；负数 = 时间回拨过的脏数据，跳过 (r7-8)
        if done >= cap:
            break
        ev = {"id": str(rec.get("id", "")), "app": rec.get("app", "?"),
              "texts": rec.get("texts") or [], "ts": rec.get("ts") or now,
              "watched": rec.get("watched", True)}
        if publish(ev):
            done += 1
        else:
            _enqueue(ev)
    if done:
        log(f"replay: 补发 {done} 条上次运行推送失败的通知")


# P2-5：三层告警的 2/3 层与 watchdog 共用（alert_fallback 不建任何文件句
# 柄，见其 docstring —— 这是它能被两个进程同时 import 而不制造轮转竞争的
# 全部原因）
from alert_fallback import push_vps, push_toast

_access_alert_at = 0.0


def _push_alert(title: str, message: str) -> bool:
    """P2-5：listener 的告警原来只有本地隧道一条通道 —— 「看门人已死」的
    时刻隧道（同为 ntfy 链路）很可能也死了，单通道 = 静默。复用 watchdog 的
    三层降级，每层各自 try（P1-2 同款边界，一层失败只降级不击穿）。"""
    payload = {
        "topic": "fp-phone",
        "title": title[:120],
        "message": message[:900],
        "priority": 4,
        "tags": pclog.tags_with_trace(["rotating_light"]),
    }
    try:
        r = httpx.post(NTFY_BASE + "/", json=payload, headers=_auth(),
                       timeout=10, trust_env=False)
        if r.status_code == 200:
            log(f"告警已送达(本地隧道): {title}")
            return True
    except Exception as e:
        log(f"告警本地隧道失败: {e!r}")
    try:
        if push_vps(payload, LOG):
            log(f"告警已送达(VPS-SSH兜底): {title}")
            return True
    except Exception as e:
        log(f"告警 VPS 通道异常: {e!r}")
    try:
        if push_toast(title, message, LOG):
            log(f"告警仅本机 toast(网络通道都失败): {title}")
            return True
    except Exception as e:
        log(f"告警 toast 通道异常: {e!r}")
    log(f"告警三条通道都失败: {title}")
    return False


def check_access() -> None:
    """P2-4：WinRT 权限只在启动时 request_access_async 查过一次；运行中被
    系统撤掉后 get_notifications 恒返回空 —— 零异常、心跳照打、quiet/link
    双绿，分诊链路静默死亡到下次登录（backlog P2-4）。周期重查：实测它是
    同步方法 get_access_status()（不是属性），撤了直推手机。"""
    global _access_alert_at
    try:
        st = UserNotificationListener.current.get_access_status()
        if st == UserNotificationListenerAccessStatus.ALLOWED:
            return
    except Exception as e:
        log(f"access status 查询失败: {e!r}")
        return
    gap = time.time() - _access_alert_at
    if 0 <= gap < 1800:
        return          # 同一故障 30 分钟一次（同 check_watchdog_hb 的节流）
    _access_alert_at = time.time()
    LOG.error(f"WinRT 通知访问权限被撤 (status={st}) —— 新通知不再进分诊，心跳照打也是空转")
    _push_alert(
        "[FxxkPush] 通知访问权限被撤",
        "系统撤掉了通知监听权限，Windows 通知不再进入分诊，"
        "QQ/微信等新通知会静默丢失。\n"
        "恢复: 设置 → 系统 → 通知 → 授权监听（或重跑 probe_notifications.py）。\n"
        "排查: pc/logs/notification_listener.log")


def check_watchdog_hb() -> None:
    """P0-2：看门人也得有人看（交叉检查，进程独立于 watchdog）。"""
    global _hb_alert_at
    try:
        age = time.time() - HB_FILE.stat().st_mtime
    except OSError:
        return          # 文件还没有 = watchdog 还没跑过第一轮，先不判（防开机误报）
    if age < 900:
        return
    gap = time.time() - _hb_alert_at
    if 0 <= gap < 1800:
        return          # 同一故障 30 分钟提醒一次，别刷屏；
                        # 负数 = 系统时间被回拨过 (r7-8)：视为已过期，继续报，
                        # 宁可多喊一次也好过告警被永久静音
    _hb_alert_at = time.time()
    LOG.error(f"watchdog 心跳 {int(age)}s 未更新 —— 看门狗可能已死，告警能力失效")
    # P2-5：单通道在「看门人已死」场景下不可靠 —— 走三层降级（local→VPS→toast）
    _push_alert(
        "[FxxkPush] watchdog 心跳超时",
        f"watchdog.hb 已 {int(age // 60)} 分钟未更新，看门狗可能已退出："
        f"health/procs/link/disk 四项巡检与三层告警同时失效。\n"
        f"排查: pc/logs/watchdog.log，或直接跑 restart_services.bat")


async def main():
    global _seen_dirty
    listener = UserNotificationListener.current
    access = await listener.request_access_async()
    if access != UserNotificationListenerAccessStatus.ALLOWED:
        log("notification access DENIED — run probe_notifications.py first")
        return 1
    log(f"notification access OK, polling every {POLL_SEC}s -> topic {TOPIC}")

    first_run = not STATE.exists()
    seen = load_seen()
    log(f"loaded {len(seen)} known notification ids")

    # P1-3：启动首遍原来对所有没见过的通知「既不发布也不归档、零日志、直接
    # 标 seen」—— 崩溃/断电/重启停机窗口内到达的通知就这么静默消失，唯一
    # 证据还留在 Windows 通知中心里。现在分两种情况：
    #   - 首次运行（state 文件不存在）：通知中心躺着几天的存量，发出去是
    #     洪水，照旧只做快照。
    #   - 之后每次重启：这些是「服务没在时才到达」的，补发。cap 30 是给
    #     state 被写坏导致 seen 清空那次兜底的 —— 那种情况下通知中心里全是
    #     「没见过」的，不设上限等于把历史一股脑怼给手机。
    pre = await poll_once(listener, seen)
    if first_run:
        log(f"first run: {len(pre)} 条存量通知标记为 seen（不发布）")
    else:
        for ev in pre[:30]:
            # r7-4：原实现忽略返回值 —— 断网时启动补发整批静默失败，
            # 既不入 _pending 也无本轮重试，等于要再重启一次才补得出来。
            if not publish(ev):
                _enqueue(ev)
        if len(pre) > 30:
            log(f"补发最近 30 条，丢弃其余 {len(pre) - 30} 条 pre-existing")
        elif pre:
            log(f"补发停机窗口内到达的 {len(pre)} 条通知")
    _seen_dirty = not save_seen(seen)

    # P1-2：上次运行推失败（pushed:false）的补一轮
    replay_failed()

    tick = 0
    while True:
        try:
            before = len(seen)
            events = await poll_once(listener, seen)
            for ev in events:
                if not publish(ev):
                    _enqueue(ev)     # 失败不丢：进队列，3s 后重试
            flush_pending()
            # 按 seen 是否变化来存，不能只看 events：extract 返回 None 的
            # 通知（IGNORE 名单 / 自己弹的 toast）照样 add 了 id 却不产出
            # events。只在 events 非空时存盘，这批 id 下一轮又是"新的"，
            # 每 3 秒重新 extract 一遍（含读 echo 文件）。_seen_dirty 是
            # 上次落盘失败后的补记（P2-4）。
            if events or len(seen) != before or _seen_dirty:
                _seen_dirty = not save_seen(seen)
        except Exception as e:
            log(f"poll error: {e!r}")
        tick += 1
        if tick % 60 == 0:              # 每 3 分钟看一眼看门狗还活着没
            check_watchdog_hb()
        if tick % 200 == 0:             # P2-4：每 10 分钟复查 WinRT 权限
            check_access()
        if tick % 100 == 0:
            # r7-10：事件驱动 + WinRT 权限被撤后 get_notifications 返回空也
            # 不留痕，「活着但没干活」和「健康」在日志上完全同形（实测 6 小时
            # 才 74 行）。每 5 分钟一条固定心跳，watchdog 的 quiet 检查才有
            # 判据可依。
            log(f"idle heartbeat: seen={len(seen)} pending={len(_pending)} "
                f"watchdog_hb_ok")
        await asyncio.sleep(POLL_SEC)


if __name__ == "__main__":
    asyncio.run(main())
