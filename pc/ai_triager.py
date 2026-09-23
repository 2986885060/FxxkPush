#!/usr/bin/env python3
"""FuckPush AI triager — the brain.

Consumes four ntfy topics (fp-pc: Windows notifications, fp-gray: VPS
gray-zone events, fp-vps: VPS hard alerts, fp-feedback: the 👍/👎 buttons
on pushes this service sent), asks MiMo to classify importance, and:
  - important  -> publish to fp-phone (phone buzzes)
  - unimportant-> silent archive

Repeats of the same source+content inside a sliding window are suppressed
entirely (no API call, no push).

注：triage_config.json 的 ``daily_report_time`` 和 state 里的 ``last_report``
是**预留键，日报尚未实现**（见 README Roadmap），这里不声称有这功能。

Runs alongside pc_subscriber.py / notification_listener.py.
"""
import asyncio
import os
import json
import re
import sys
import time
import psutil        # _clean_tmp 的 pid 已死判定（r7-9b）
from pathlib import Path

import httpx

import pclog
import fp_feedback

HERE = Path(__file__).parent
try:
    CONFIG = json.loads((HERE / "triage_config.json").read_text(encoding="utf-8"))
except Exception:
    # 损坏的 JSON、或 read_text 被 AV 瞬间独占 —— 这行跑在下面
    # sys.excepthook 挂上**之前**，pythonw 下会无声退出（第 5 轮：这类
    # 导入期读取点全仓库有 4 处，这是其中之一）。给一份能跑起来的配置：
    # api_key 空 → classify 收 401，那是 LLM 侧、有 .response，走服务端
    # 退避并留日志 —— 比进程直接消失好得多。
    CONFIG = {}
if not isinstance(CONFIG, dict):
    # r7-9a：文件是合法 JSON 但不是对象（数组/字符串/数字）时，json.loads
    # 不抛异常，CONFIG 就成了非 dict —— 下面 setdefault 直接 AttributeError，
    # 而这行跑在 sys.excepthook 挂上**之前**，pythonw 下无声退出。watchdog
    # 每 300s revive 一次，每次都在同一行死 -> 无人可自动修复的崩溃循环，
    # 每 5 分钟只留一行 FATAL。降级成空 dict，由 setdefault 补齐必需键。
    CONFIG = {}
# 补齐必需键：即使配置文件被手改少了字段，也不该在运行中途 KeyError。
CONFIG.setdefault("model", "mimo-v2.6-flash")
CONFIG.setdefault("dedup_window_sec", 1800)
CONFIG.setdefault("api_key", "")
CONFIG.setdefault("base_url", "https://api.xiaomimimo.com/v1")
CONFIG.setdefault("max_tokens", 200)

NTFY_BASE = os.environ.get("FP_NTFY_URL", "http://127.0.0.1:2586")  # via SSH tunnel (see pc/ntfy_tunnel.py)
_SECRET = HERE / "ntfy.secret"
# token 走惰性读取，见下面的 ntfy_token() —— 导入期读一次会把一次 AV 抖动
# 永久定成空串，而空 token 发出去的 `Bearer ` 是非法头值（第 5 轮 P2·必修）。
ARCHIVE = HERE / "triage_log.jsonl"
STATE = HERE / "triage_state.json"

TOPICS_IN = {"fp-pc": "通知", "fp-gray": "VPS灰区", "fp-vps": "VPS硬规则"}
# 反馈 topic 不进 TOPICS_IN：那个 dict 的 value 是给 AI 的 kind 标签，
# 反馈要是混进去就会被当成 [fp-feedback] 待分诊消息问一遍模型。
ALL_TOPICS = [*TOPICS_IN, fp_feedback.TOPIC]
TOPIC_OUT = "fp-phone"

SYSTEM_PROMPT = """你是消息分诊助手。用户会在手机上收到你判定为"重要"的消息，\
所以只把真正需要立即知道的事情标为重要。

判定标准：
- 重要：涉及用户本人被点名/被找、日程与截止提醒、账户安全、服务异常需要处理、\
重复超过3次的同源事件（说明有人在坚持联系）、家人/紧急联系人消息
- 忽略：群聊闲聊、营销推广、验证码之外的系统通知、新闻推送、状态汇报

只输出 JSON，格式：{"label": "重要"|"忽略", "reason": "10字以内理由"}"""


LOG = pclog.get_logger("ai_triager")

# 推送快照的存活期。超过一周的👍/👎已经没有纠正意义，占着 state 只会让
# 每次 save_state 都多写一堆死数据 —— 反馈是低频事件，7 天足够。
PUSH_TTL = 7 * 24 * 3600

# P2-3：TTL 之外的第二道闸。TTL 只挡「老」挡不住「多」。
PUSH_CAP = 500

# P1-2：推失败的重发队列。push_phone 失败最常发生在隧道抖动 / 断网时，而那
# 恰恰是「重要」消息最该送达的时候 —— 原实现失败即丢（连按钮快照一起 pop），
# 只留一行 PUSH_FAIL 日志，等于要人工从 state/log 里捞。flush_pending 每 15s
# 重试一条，超过 PENDING_TTL 才真放弃。
_push_pending: list[dict] = []
PENDING_CAP = 100
PENDING_TTL = 900
RETRY_INTERVAL = 15        # flush_pending 的重试节拍（抽成常量：测试要能调快）


def log(msg):
    # file log: pythonw runs have no console, a crash must leave evidence.
    # pclog owns rotation/format/level — do not hand-roll it here.
    pclog.log_auto(LOG, msg)


_token_cache: str | None = None


def ntfy_token() -> str:
    """惰性读取 ntfy token —— **失败与空内容都不缓存**，下次调用自动重试。

    第 4 轮改成导入期读一次 + 包 try，看着更稳，实际更糟（第 5 轮 P2·必修）：
    读失败把 token 永久定成 ``""``，于是**每个**请求都发
    ``Authorization: Bearer ***（空）—— httpx 实测抛
    ``LocalProtocolError('Illegal header value b"Bearer "')``。那是
    TransportError、**没有 .response**，所以走的是传输级分支，而不是第 4 轮
    注释里声称的「401 服务端退避」：4 条流每 5s 各打一条 ERROR（≈7 万行/天），
    第 12 次 raise 换 client，4 条流全断重连不回放，循环到人工重启。
    文件恢复后本函数下一次调用就读到真 token，自愈 ——这才是和
    fp_feedback._token() 一致的契约（那边每次推送都重读）。
    """
    global _token_cache
    if _token_cache is None or _token_cache == "":
        try:
            tok = _SECRET.read_text().strip() if _SECRET.exists() else ""
        except Exception as e:
            log(f"ntfy.secret unreadable ({e!r}) —— 本轮请求不带鉴权头")
            return ""
        _token_cache = tok
    return _token_cache


def _auth() -> dict:
    """鉴权头。**空 token 时不带头**，这是能自愈的关键。

    带空 ``Bearer `` 是非法头值 → LocalProtocolError（TransportError，
    无 .response → 传输级分支 → 热循环，见 ntfy_token docstring）。
    不带头则服务端正常回 401 → HTTPStatusError 有 .response →
    落进 supervise 的服务端退避分支：日志看得见、退避可自愈。
    """
    tok = ntfy_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _log_crash(exc_type, exc, tb):
    import traceback
    log("FATAL " + "".join(traceback.format_exception(exc_type, exc, tb)).strip())
    os._exit(1)


sys.excepthook = _log_crash


def archive(rec):
    try:
        pclog.append_rotating(ARCHIVE, json.dumps(rec, ensure_ascii=False),
                              mode="rotate")
    except Exception as e:
        # 磁盘满/权限问题不该让一条消息掀翻消费循环
        log(f"archive write failed: {e!r}")


def _prune_pushes(st: dict) -> None:
    """丢掉过期的推送快照。启动时和每次入队后各跑一次 —— 环形缓冲的最小
    实现，不用后台定时器（多一个任务就多一个可能挂在 finally 外的异常）。"""
    pushes = st.get("pushes")
    if not isinstance(pushes, dict) or not pushes:
        st["pushes"] = {}
        return
    now = time.time()
    fresh = {}
    for k, v in pushes.items():
        # 内层 entry 也要校：ts 是字符串/None 时 `now - v.get("ts", 0)` 直接
        # TypeError，而 _prune_pushes 跑在 load_state 里、任何 save_state
        # **之前** —— 崩在这里状态文件永远不会被重写，有守护进程就是无限
        # 崩溃循环（第 1 轮只校了外层是 dict，深度不够）。
        if not isinstance(v, dict):
            continue
        ts = v.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        if now - ts < PUSH_TTL:
            fresh[k] = v
    # P2-3：TTL 之外再加硬上限，按 ts 保留最新 PUSH_CAP 条。TTL 只挡「老」，
    # 挡不住「多」—— content 每条截 500、TTL 7 天，日均几百条重要推送时
    # state 能长到 MB 级，而 save_state 在**每条消息的 finally** 里全量重写
    # 它，写放大会跟着翻几倍。
    if len(fresh) > PUSH_CAP:
        fresh = dict(sorted(fresh.items(),
                            key=lambda kv: kv[1].get("ts", 0))[-PUSH_CAP:])
    st["pushes"] = fresh


def load_state():
    st = None
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception as e:
            # 状态损坏（断电/非原子写时代留下的半截 JSON）。load_state 在
            # main() 的 while True 之外，这里抛出去就是服务死、没人接得住。
            # 丢去重状态的最坏后果只是同一条消息重新分诊一次，比死强。
            log(f"triage state corrupt ({e!r}), resetting")
    if not isinstance(st, dict):
        st = {"recent": {}, "last_report": ""}
    if not isinstance(st.get("recent"), dict):
        # 「可解析但形状不对」：recent 要是字符串/列表，setdefault 只补键不
        # 校验类型，dedup_check 的 .items() 会让除硬规则外的每条消息都抛，
        # 被 consume 吞掉 —— 服务看着活着，AI 路径其实全废。
        st["recent"] = {}
    if not isinstance(st.get("last_report"), str):
        st["last_report"] = ""
    # pushes 的外层/内层类型都在 _prune_pushes 里洗（它开头就处理了
    # 非 dict 的情况），这里不必再判一次 —— 同一个判断写两遍是第 5 轮
    # 报的冗余。
    _prune_pushes(st)
    return st


def _clean_tmp() -> None:
    """P2-3：清掉硬崩溃留下的孤儿临时文件（卡在 write 与 replace 之间那次）。

    文件名都带 pid，进程死了就再也没人认领它们，不清理就是永久残留。只清
    本模块自己那几种名字，不误伤别的文件。（正常失败路径自己会 unlink，
    这里兜的是「unlink 之前进程就被 kill」的窗口。）
    """
    # r7-9b：泛化到 pc/ 下全部 *.<pid>.tmp —— 原来只清 triage_state 自己
    # 那种名字，listener_state / vision_state / triage_rules 每次硬崩溃都
    # 残留一个，与 P2-3 的初衷不一致。
    # 判据用 **pid 是否已死**（不是 mtime）：mtime > 600s 那版有两个坑 ——
    # (1) 崩溃后 600s 内被 watchdog revive 重启时清不掉（revive 冷却才 300s），
    #     残留跨多轮；(2) 「活着的进程正在写的 tmp」要等 10 分钟才被排除。
    # pid 活着 = 有人几分钟内就会 replace 它，跳过；pid 死了 = 没人认领，
    # 立即清。pid 被系统复用给无关进程的极端情况只是晚清一次，无害。
    for p in HERE.glob("*.tmp"):
        m = re.search(r"\.(\d+)\.tmp$", p.name)
        if not m:
            continue          # 只认带 pid 的原子写临时名，不误伤其它 .tmp
        pid = int(m.group(1))
        if pid == os.getpid() or psutil.pid_exists(pid):
            continue
        try:
            p.unlink()
            log(f"removed orphan tmp: {p.name} (pid {pid} 已死)")
        except OSError:
            pass


def save_state(st):
    # 原子写：先落 .tmp 再 rename。直接 write_text 写一半断电会留下半截
    # JSON —— 这个函数在每条消息的 finally 里跑，非原子写等于埋雷。
    # 临时名带 pid：重启重叠期两个进程共用固定 .tmp 会把半截 JSON replace
    # 上去（load_state 会自愈重置，代价是去重状态清零）。
    tmp = STATE.with_name(f"{STATE.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE)
    except OSError as e:
        # 这里在 consume 的 finally 里 —— 异常冒出去会被 supervise 当成
        # stream error：断流重连，而**重连不回放**，断开窗口内的消息永久
        # 丢失。Windows 上编辑器/AV/索引器在 rename 瞬间持句柄就能触发。
        # 落不下盘就打日志继续，下一条消息的 save_state 会带上同样状态；
        # 权衡下来「丢一次落盘」远好过「为了它断一条流」（第 4 轮）。
        log(f"save_state failed ({e!r}) —— 本条状态未落盘，下条会重试")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


async def classify(client: httpx.AsyncClient, kind: str, content: str) -> dict:
    payload = {
        "model": CONFIG["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"[{kind}] {content}"},
        ],
        "max_completion_tokens": CONFIG["max_tokens"],
        "thinking": {"type": "disabled"},
    }
    r = await client.post(f"{CONFIG['base_url'].rstrip('/')}/chat/completions",
                          json=payload,
                          headers={"Authorization": f"Bearer {CONFIG['api_key']}"},
                          timeout=30)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"].strip()
    # strip markdown fence if model added one
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)


def dedup_check(st, kind, content) -> str | None:
    """同一源在窗口期内的重复条目整条压掉（不问 AI、不推送）。

    返回 ``"pass"`` 表示首次出现、放行；``None`` 表示命中窗口、压掉。
    调用方只判 ``is None``。
    """
    src = re.sub(r"\s+", "", content)[:40]
    now = time.time()
    win = CONFIG["dedup_window_sec"]
    for k in list(st["recent"]):
        v = st["recent"][k]
        # 内层 entry 也要校：ts 缺失/非数值在这里就是 KeyError/TypeError，
        # 而 dedup_check 在 handle_event 的 try 之内 —— 后果是**每一条**消息
        # handle failed，服务看着活着，AI 路径其实全废（和第 2 轮给 recent
        # 写的那句注释是同一类缺口，当时只校了外层是 dict）。
        if not isinstance(v, dict) or not isinstance(v.get("ts"), (int, float)) \
                or isinstance(v.get("ts"), bool):
            del st["recent"][k]
            continue
        if now - v["ts"] > win:
            del st["recent"][k]
    key = f"{kind}:{src}"
    if key in st["recent"]:
        return None  # suppressed
    # 不再写 count：第 2 轮删掉了它唯一的消费者「（第N次）」，第 4 轮确认
    # 全仓库再无读取端 —— 连同为它加的类型防御一起是死代码。
    st["recent"][key] = {"ts": now}
    return "pass"


async def push_phone(client, title, message, priority=4, actions=None):
    try:
        payload = {
            "topic": TOPIC_OUT, "title": title, "message": message,
            "priority": priority,
            # tags carries the trace_id: ntfy drops every other custom field,
            # so this is the only channel that can carry the chain to the phone.
            "tags": pclog.tags_with_trace(["bell"]),
        }
        if actions:
            payload["actions"] = actions
        r = await client.post(NTFY_BASE + "/", timeout=10, json=payload,
                              headers=_auth())
        return r.status_code == 200
    except Exception as e:
        # 传输失败要在这里变成 False：冒出去会把整条 SSE 断掉（见 consume），
        # 而推送失败恰恰发生在网络抖动的时候 —— 断流等于雪上加霜。
        log(f"push_phone transport error: {e!r}")
        return False


async def flush_pending(client, st):
    """P1-2 �发：把推送失败的那条重新发一遍，超时才放弃。

    与 consume 同生共死（由 main 挂进同一个 tasks 列表，client 重建时一起
    cancel）。每 15s 出队一条：失败回**队尾**（不阻塞后面别的消息），成功就
    丢弃；超过 PENDING_TTL 才清按钮快照并落盘 —— 那之后手机上不会再出现
    这个 pid 的👍/👎，留着快照只会白占 7 天 state。
    """
    beats = 0
    while True:
        await asyncio.sleep(RETRY_INTERVAL)
        beats += 1
        if beats % 20 == 0:
            # r7-10：SSE 订阅建立后上游一条消息都没有 / 订阅其实已断但没报错，
            # 两者在日志上同形。每 5 分钟一条固定心跳，watchdog 的 quiet 检查
            # 才有判据（read=120s 超时只在真断时触发，不产生任何周期日志）。
            log(f"idle heartbeat: pending={len(_push_pending)} topics={len(ALL_TOPICS)}")
        if not _push_pending:
            continue
        # r7-3（P1）：peek 而不是先 pop —— client 重建路径会在下面 await 点
        # 注入 CancelledError，原实现此时条目已出队、尚未回插，于是这一条
        # 既没发出也没留在队列里，静默丢失（断网/抖动正是高发场景）。
        # peek + 成功后才 remove：cancel 落在任何位置，条目都还在队首。
        item = _push_pending[0]
        age = time.time() - item.get("ts", 0)
        if age > PENDING_TTL or age < 0:
            _push_pending.pop(0)
            if item.get("pid"):
                st["pushes"].pop(item["pid"], None)
                save_state(st)
            log(f"PUSH_GIVEUP (>{PENDING_TTL}s) [{item['title']}] "
                f"{item['message'][:50]}")
            continue
        ok = await push_phone(client, item["title"], item["message"],
                              actions=item.get("actions"))
        if ok:
            # 单消费者，pop 前确认还是同一条（await 期间理论上没人动它，
            # 但防御性判断零成本）
            if _push_pending and _push_pending[0] is item:
                _push_pending.pop(0)
            log(f"PUSH_RETRY ok [{item['title']}] {item['message'][:50]}")
        else:
            # 失败转队尾，不阻塞后面别的消息
            if _push_pending and _push_pending[0] is item:
                _push_pending.append(_push_pending.pop(0))


async def handle_event(client, st, topic, ev):
    kind = TOPICS_IN.get(topic, topic)
    # ntfy carries everything as title+message text already
    title = (ev.get("title") or "").strip()
    body = (ev.get("message") or "").strip()
    # content 来源一次选好：原来先拼 f"{title}\n{body}".strip()，紧接着的
    # fp-pc 分支（主流量）又用 content = body or title 整个覆盖 —— 白拼一次
    # （第 3 轮冗余）。
    if topic == "fp-pc":
        # title shape: "QQ: sender" -> app = QQ, rest = sender
        src = title.split(":")[0].strip() if ":" in title else "Windows通知"
        content = body or title
    else:
        src = topic
        content = (title + "\n" + body).strip() if body else title

    # HARD RULE: @所有人 / @我 always push (bypasses AI + dedup). The vision
    # listener flags these too, but the triager is the gate to the phone — if
    # it does not enforce them here, its "群聊闲聊" verdict would silence a
    # message the user explicitly wants.
    if any(mark in content for mark in ("@所有人", "@我", "@全体成员")):
        archive({"ts": time.time(), "topic": topic, "src": src,
                 "content": content, "label": "重要", "reason": "@提及硬规则"})
        ok = await push_phone(client, f"[提及] {src}", content[:200])
        log(f"MENTION_PUSH {'ok' if ok else 'FAIL'} [{src}] {content[:50]}")
        return

    # TEST RULE: anything containing "text" always goes straight to the
    # phone (bypasses AI + dedup) so omo can test the pipe anytime.
    if "text" in content.lower():
        archive({"ts": time.time(), "topic": topic, "src": src,
                 "content": content, "label": "测试直推"})
        ok = await push_phone(client, f"[测试] {src}", content[:200])
        log(f"TEST_PUSH {'ok' if ok else 'FAIL'} [{src}] {content[:50]}")
        return

    # P2-6: fp-vps 是硬规则通道 —— vps_monitor 的 docstring 承诺「直接推
    # 手机」，实际却进这里被 AI 二道闸（实测启动 push 被判「忽略」静默归档，
    # PC 端 toast 还在、手机全聋）。整通道绕过 AI+dedup，fail-open：多推
    # 无害，漏推致命。不按 priority 分流有两重原因：_unwrap 只透传
    # title/message（SSE 原始事件的 priority 已被丢弃，实测 L536/547），
    # 以及 priority=3 的 SSH 登录同样是硬规则、分流会漏它。VPS 侧 cooldown
    # （disk/unit 30min、ssh 24h）才是这里唯一的去重权威。
    if topic == "fp-vps":
        archive({"ts": time.time(), "topic": topic, "src": src,
                 "content": content, "label": "重要",
                 "reason": "VPS硬规则直推(绕过AI二道闸)"})
        msg = f"{content[:200]}\n— VPS 硬规则，绕过 AI 直推"
        ok = await push_phone(client, f"[VPS] {title or '硬规则'}", msg)
        if not ok:
            if len(_push_pending) >= PENDING_CAP:
                dropped = _push_pending.pop(0)
                if dropped.get("pid"):
                    st["pushes"].pop(dropped["pid"], None)
            _push_pending.append({"title": f"[VPS] {title or 'fp-vps'}",
                                  "message": msg, "actions": None,
                                  "pid": None, "ts": time.time()})
        log(f"HARD_PUSH {'ok' if ok else 'FAIL->pending'} [{src}] {content[:50]}")
        return

    # dedup first (no API call for repeats)
    if dedup_check(st, kind, content) is None:
        log(f"dedup suppressed [{src}] {content[:40]}")
        archive({"ts": time.time(), "topic": topic, "src": src,
                 "content": content, "label": "重复抑制"})
        return

    # 源级规则快速路：这个源被人工点过 3 次👎 → 直接静默归档，连 AI 都不问
    # （省一次 API 调用）。排在两条硬规则之后：@提及和测试通道是用户明确要
    # 的东西，不该被一条自动学来的规则压掉。
    # 只对 fp-pc 生效：其它 topic 的 src 恒等于 topic 名（见上面的 else 分支），
    # 对 fp-vps 点 3 次👎 会静音掉整条服务器告警通道 —— 磁盘爆了手机不响，
    # 这个口子绝不能开。按钮侧也做了同样的限定（push 段），这里是第二道闸。
    if topic == "fp-pc" and fp_feedback.should_ignore(src):
        archive({"ts": time.time(), "topic": topic, "src": src,
                 "content": content, "label": "规则静默",
                 "reason": f"源[{src}]误判累积"})
        log(f"rule-silenced [{src}] {content[:50]}")
        return

    try:
        verdict = await classify(client, kind, content)
        if not isinstance(verdict, dict):
            # 模型完全可能返回合法 JSON 但不是对象（["重要"] / "重要" /
            # null —— classify 自己就在处理模型不守 fence，说明这类偏移真实
            # 存在）。类型检查必须放在 try **里面**：放外面的话这里抛
            # AttributeError，本该兜底的「AI 异常默认推送」反而失效，
            # 消息被 consume 吞掉（第 2 轮 P2）。
            raise TypeError(f"model returned {type(verdict).__name__}: "
                            f"{str(verdict)[:60]}")
    except Exception as e:
        log(f"classify failed ({e!r}) -> fallback: push (safer to buzz)")
        verdict = {"label": "重要", "reason": "AI异常默认推送"}

    # label 归一化 + fail-open：模型返回「重要。」「重要 」这类变体是真实存在
    # 的（classify 自己就在处理模型不守 fence）。精确匹配会让这些既不进 push
    # 分支、也不触发 fallback —— 于是**静默归档成「忽略」**，方向正好和上面
    # 「AI 异常默认推送 (safer to buzz)」相反，而漏推是本模块自认最坏的失败。
    # 连 verdict 缺 label 键也一样：原来默认取 "忽略"，那是 fail-closed。
    raw_label = verdict.get("label")
    label = str(raw_label).strip().rstrip("。.！! ，,").strip() \
        if raw_label is not None else ""
    reason = str(verdict.get("reason", "") or "")
    if label not in ("重要", "忽略"):
        reason = (f"未知判定 {raw_label!r}，按重要推送（fail-open）"
                  + (f"；模型给的理由：{reason}" if reason else ""))
        label = "重要"
    rec = {"ts": time.time(), "topic": topic, "src": src, "content": content,
           "label": label, "reason": reason}
    archive(rec)

    if label == "重要":
        # 推送快照：反馈回来时靠它还原 src/content。triage_log 是轮转的，
        # 几天后点一次按钮去那里查只会读到空 —— 所以自包含地存一份。
        # 只有 fp-pc 才带按钮：其它 topic 的 src 就是 topic 名本身，规则粒度
        # 是「整条通道」，3 次👎 会把 VPS 告警整条静音（不可接受）。
        #
        # 这里原本还有「（第N次）」后缀：dedup_check 对窗口内的重复直接
        # return None（静默抑制，不推也不调 AI），所以能走到推送的必然是首次，
        # count 恒为 1、后缀恒为空 —— 死代码。而且它用子串匹配找 key，
        # content 去空白后为空串时 `"" in k` 恒真，会把别人的计数算到自己头上。
        # 一并删掉（第 2 轮 P2 + 性能项）。
        pid = pclog.new_trace_id()
        actions = fp_feedback.build_actions(pid) if topic == "fp-pc" else None
        if actions:
            # 没有 actions 就不会有反馈，快照存着只会在 state 里躺满 7 天。
            # content 截到 500：够做微调语料，又不至于让 save_state 每条消息
            # 都重写几万字节（推送本身也只发前 200 字）。
            st["pushes"][pid] = {
                "ts": time.time(), "topic": topic, "src": src,
                "content": content[:500], "label": label, "reason": reason,
            }
            _prune_pushes(st)
        msg = f"{content[:200]}\n— {reason}"
        ok = await push_phone(client, src, msg, actions=actions)
        if not ok:
            # P1-2：失败不等于放弃 —— 挂进重发队列，flush_pending 每 15s 重试。
            # 原实现是「失败即 pop 按钮快照」，断网窗口里的重要推送和它的
            # 👍/👎 一起永久丢失。只有 PENDING_TTL 超时才清快照（在 flush 里）。
            if len(_push_pending) >= PENDING_CAP:
                dropped = _push_pending.pop(0)
                if dropped.get("pid"):
                    st["pushes"].pop(dropped["pid"], None)
                log(f"push queue full, dropped oldest "
                    f"push_id={dropped.get('pid')} [{dropped.get('title')}]")
            _push_pending.append({"title": src, "message": msg,
                                  "actions": actions,
                                  "pid": pid if actions else None,
                                  "ts": time.time()})
        log(f"{'PUSHED' if ok else 'PUSH_FAIL'} [{src}] {reason} "
            f"push_id={pid} {'+fb' if actions else ''} | {content[:50]}")
    else:
        log(f"silent    [{src}] {reason} | {content[:50]}")


def _unwrap(ev: dict, body: str) -> dict:
    """把 message 字段还原成 ``{"title", "message", ...}``。绝不抛。

    实测两个发布方（notification_listener / wechat_vision_listener）发的
    message 都是**纯文本**，所以绝大多数消息走的是最后那个兜底分支；JSON
    分支目前是给「发布方哪天改发 JSON」留的活口，不是线上主路径。
    解析不出来（不是 JSON、或压根不是字符串）一律退回原文兜底。
    """
    try:
        # isinstance 挡在前面：json.loads(None) 抛的是 **TypeError** 而不是
        # JSONDecodeError（第 3 轮实测），只接后者会违背「绝不抛」的契约，
        # 消息被 consume 吞掉、连归档都不做。
        maybe = json.loads(body) if isinstance(body, str) else None
    except (json.JSONDecodeError, TypeError):
        maybe = None
    base = {"title": ev.get("title", ""), "message": body}
    if isinstance(maybe, dict):
        data = maybe.get("data")
        if isinstance(data, dict):
            # 顶层键和 data 里的键都留着 —— 保守起见，不凭空丢数据。线上两个
            # 发布方发的都是纯文本（根本走不到这个分支），而 handle_event 的
            # kind 也是从 topic 派生、并不读 ev 里的 kind/ts（第 3 轮核过：
            # 全仓库没有读取点，所以「丢顶层 kind/ts」当初是无效修复）。
            # 真正有用的是那句 message 兜底：data 里没有 message 键时
            # handle_event 会拿到空 body，AI 分诊只剩一个标签、内容全丢。
            # 顶层若带 message/title 则照旧覆盖，与 fp-pc 原行为一致。
            return {**base, **maybe, **data}
        # data 不是对象（str/None/int/list）→ 顶层原样返回
        return {**base, **maybe}
    # 走到这里：不是 JSON，或 JSON 不是对象（["重要"] / null / 数字）。
    # 原实现在 isinstance(parsed, str) 为假时直接 **parsed —— **None 会
    # TypeError，违背自己「绝不抛」的 docstring，消息被 consume 吞掉。
    return base


async def consume(client, st, topic):
    """JSON stream one topic, handle each message event."""
    url = f"{NTFY_BASE}/{topic}/json"
    async with client.stream("GET", url, headers=_auth()) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line:
                continue
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            # isinstance 挡在 per-message try **外面**的这一句上：json.loads
            # 合法但不是对象（[1] / null / 123）时 ev.get 直接 AttributeError
            # → 断流（重连不回放）。同类点 pclog.extract_trace 早就补了
            # isinstance，这里一直没补（第 5 轮）。
            if not isinstance(ev, dict) or ev.get("event") != "message":
                continue
            # trace_id starts here for this message: ntfy only forwards tags,
            # so the id rides in tags (or falls back to the ntfy message id,
            # which is identical for every subscriber of the same message).
            # with 块结束自动恢复上下文。直接 bind_from_event() 是不恢复的：
            # main() 里那句 "stream error ... reconnect in 5s" 会挂上最后一条
            # 消息的 trace，grep 那条消息时会捞到一堆无关的重连错误
            # （pc_subscriber 已因同一个 bug 修过，这里是同一处漏网）。
            with pclog.trace(pclog.extract_trace(ev)):
                body = ev.get("message", "")
                # 结构性兜底：单条消息失败绝不能断开 SSE —— 重连不回放，
                # 断开的这几秒里该 topic 的消息会永久丢失。classify/push_phone
                # 各自有 try，这里防的是 archive、未来的调用、以及任何没想到的
                # 异常。save_state 放 finally：失败那条也得把去重状态存下去，
                # 否则同一条消息重连后会再分诊一次。
                try:
                    if topic == fp_feedback.TOPIC:
                        # 反馈分支绕开 _unwrap 是为了**省一次白做的
                        # json.loads**（handle 里还要按 push_id 解析一次），
                        # 不是「拿不到东西」—— 新 _unwrap 已把 message 兜底
                        # 铺在最前，走它也不会丢 message 键了。
                        fp_feedback.handle(st, body)
                    else:
                        await handle_event(client, st, topic, _unwrap(ev, body))
                except Exception as e:
                    log(f"handle failed ({e!r}), stream stays up")
                finally:
                    save_state(st)


async def supervise(client, st, topic, max_fails: int = 12):
    """单条流的监督者：这条流挂了只重连它自己。

    原来是 ``gather(*(consume(...)))``：任何一条流抛异常，gather 立刻向上传播，
    main 的 ``async with`` 随即关掉 client。而**兄弟任务并不会被自动 cancel**
    —— 第 3 轮用 Python 3.11 实测：抛出瞬间 ``sibling cancelled? False``，
    要等事件循环退出才收尾（本函数早先的注释写反了，那是在复述一个想当然的
    行为）。于是剩下几条会拿着已关闭的 client 各撞十几行假错误，最后那个
    raise 还被已完成的 gather 用 fut.exception() 吞掉。main 里那段显式
    cancel 就是补这个洞的。fp-feedback 是全新链路（发布端实测过不等于订阅端
    没问题），它一旦 401/404，另外三条主流会被一个自己用不到的 topic 拖断 ——
    而重连不回放，断开那几秒的消息永久丢失。

    连续失败攒到 max_fails（12 × 5s = 1 分钟）就往外抛，让 main 重建
    httpx client：client 级的故障（系统代理毒化那种）在流内小打小闹治不好，
    必须换掉整个客户端 —— 原 main 的重连逻辑正是为此存在，不能丢。
    """
    fails = 0            # 只统计**传输级**故障（攒够才换 client）
    config_retries = 0   # 服务端响应类故障的退避序号
    last_server_err = 0.0  # 上次服务端响应类故障的时刻，棘轮复位用
    while True:
        try:
            await consume(client, st, topic)
            fails = 0            # 服务端主动断开也算一次干净收尾
            config_retries = 0
        except Exception as e:
            resp = getattr(e, "response", None)
            status = getattr(resp, "status_code", None)
            if status is not None:
                # 只要拿到了 HTTP 响应（4xx / 429 / 5xx 一视同仁），换
                # client 就是白费：那是服务端或代理的事 → 单流退避。
                # 棘轮复位：距**上一次**服务端故障超过 10 分钟 = 新一轮
                # episode，退避从短的重新来。上一版的判据
                # ``time.time()-t0 > 300`` 是死条件（第 5 轮实测）：
                # status 只可能来自 consume 里的 raise_for_status()，必然
                # 发生在 t0 之后 ≤15s+15s 内，永远 <300s —— 承诺的复位
                # 一次都没发生过，下一次全新 4xx 会直接从残留值起步。
                now = time.time()
                if now - last_server_err > 600:
                    config_retries = 0
                last_server_err = now
                config_retries += 1
                # 从 30s 起（30*2**n 首次算出 60s 是 off-by-one），
                # 30/60/120/240/480/600 封顶
                delay = min(600, 30 * 2 ** max(0, config_retries - 1))
                # 必须带「stream error」：pclog._ERR_PAT 认 \\berror\\b →
                # 自动定级 ERROR；watchdog 的 _ERR_PAT 同样认它。上一版打的
                # 是无标记 INFO → 持续 4xx 期间 health 200、procs 齐、link
                # 也只见 INFO → errs≥3 && oks==0 永不成立 → **手机零告警**，
                # 而订阅此时可能已经断了、没有 PUSH_FAIL 兜底，全链路静默。
                log(f"[{topic}] stream error: server {status}, "
                    f"single-stream backoff {delay}s "
                    f"(第 {config_retries} 次，其余流不受影响)")
                await asyncio.sleep(delay)
                continue
            # 没 response = 连都没连上（隧道断/端口不通）→ 这类才该换 client。
            # fails 只在这里累加，与上面「只统计传输级故障」一致 —— 上一版
            # 在 except 顶部无条件 +1，4xx episode 之后 fails 已 ≥12 且只有
            # 干净 return 才清零，于是之后**每一次**传输抖动都立即 raise 换
            # client（4 条流全断 ~5s、不回放），12 次容错形同虚设；而今天
            # 日志里传输类抖动有 646 条，是常态事件（第 5 轮）。
            fails += 1
            log(f"[{topic}] stream error: {e!r} #{fails}, reconnect in 5s")
            if fails >= max_fails:
                fails = 0
                raise   # 换掉整个客户端
        await asyncio.sleep(5)   # 正常收尾也要等：否则服务端秒断会变成紧循环


async def main():
    log(f"triager up: model={CONFIG['model']}, out={TOPIC_OUT}")
    _clean_tmp()                     # P2-3：先扫掉上次硬崩溃留下的孤儿 .tmp
    st = load_state()
    # read 不能是 None：那样 SSE 流**没有任何读超时**，网络路径静默失效
    # （休眠唤醒 / NAT 超时 / 网络切换，没发 FIN 也没 RST）时 aiter_lines()
    # 会永久阻塞 —— 无日志、不重连、watchdog 也查不出（进程活着、/v1/health
    # 正常），消息全丢到重启为止（第 4 轮 P1）。ntfy 实测 keepalive 间隔是
    # **45s**（连续观测 45.8/90.8/135.8/180.8s，与 ntfy 默认
    # keepalive-interval: 45s 吻合 —— 第 4 轮注释写的「每 30s」是错的，
    # 照 30s 去调 read 反而会每 45s 断一次流），consume 对非 message 事件
    # 本来就 continue，所以 120s = 45s 的 2.7 倍：足够宽，又不至于让半开
    # 连接挂一辈子。
    timeout = httpx.Timeout(connect=15, read=120, write=15, pool=15)
    while True:
        # Rebuild the client on every reconnect attempt with trust_env=False.
        # With trust_env on, httpx snapshots the Windows system proxy at
        # construction time; the long-lived client then keeps retrying through
        # a proxy port that v2rayN may have since torn down, and every request
        # fails (WinError 10061) until the process restarts. We talk to a
        # loopback tunnel and to the API directly, so never use a proxy.
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                # asyncio.gather 在子任务抛异常时**不取消**其余任务（第 3 轮
                # 已用 3.11 实测：抛出瞬间 sibling cancelled? False，要等事件
                # 循环退出才收尾）。而这里的 async with 随即 aclose() 掉 client
                # —— 剩下 3 条会拿着已关闭的 client 各自撞约 12 行假错误，
                # 最后那个 raise 还会被已完成的 gather 用 fut.exception() 吞掉。
                # 所以升级前必须显式 cancel 兄弟任务并等它们真的结束。
                tasks = [asyncio.create_task(supervise(client, st, t))
                         for t in ALL_TOPICS]
                # P1-2：重发任务和流任务同一批，client 重建时一起被 cancel
                # （见下面 except 分支），不会留下拿着已关闭 client 的孤儿任务。
                tasks.append(asyncio.create_task(flush_pending(client, st)))
                try:
                    await asyncio.gather(*tasks)
                except Exception:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
        except Exception as e:
            log(f"client error: {e!r}, rebuilding client in 5s")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
