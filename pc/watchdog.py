#!/usr/bin/env python3
"""FxxkPush 管道自检看门狗 —— 故障要推到手机，不能等有人开电脑看日志。

监控三件事，任一连续 FAIL_MIN 分钟不健康就直接推 fp-phone，绕过 AI：
  1. health  隧道 HTTP 200（隧道是全链路的地基）
  2. procs   5 个服务进程还在不在（shim+真身 = 2 个/服务）
  3. link    进程活着但连不上 —— 日志最近几分钟全是连接错误，
             正是 2026-09-22 那种"客户端中毒"的形态

告警通道必须独立于被监控对象：本地 127.0.0.1:2586 是隧道，隧道挂了它也挂。
所以先试本地（快），失败就用 SSH 到 VPS 上发 —— SSH 端口是这个网络唯一
放行的口，隧道死了它还活着。

2026-09-22 教训一：隧道 11:33 断到 12:52，80 分钟零告警，因为告警通道和
被监控对象是同一条路。今天这个故障本该在 11:42 就推到手机上。

教训二（同日 17:21-17:28 实测）：**上面那两条通道共享「外部网络」这一个
故障域** —— 隧道 WinError 10061 和 SSH TimeoutError 是同时发生的，
watchdog 连打两条「告警两条通道都失败！」。所以还有第三条：
``push_toast`` 走 Windows 原生通知中心，零网络依赖，断网也弹得出来。
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import psutil
import time
from datetime import datetime
from pathlib import Path

import httpx

HERE = Path(__file__).parent
NTFY_BASE = "http://127.0.0.1:2586"
HEALTH_URL = f"{NTFY_BASE}/v1/health"
LOG_DIR = HERE / "logs"

CHECK_INTERVAL = 60        # 秒
FAIL_MIN = 5               # 连续不健康多少分钟才告警
LINK_ERR_MIN = 300         # link 检查回看多少秒
LINK_ERR_THRESHOLD = 3     # 这段时间内至少几条错误
RENOTIFY = 1800            # 同一故障持续时最多重复提醒的秒数（防刷屏）
HEARTBEAT_CYCLES = 10      # 每 10 轮（10 分钟）记一条心跳

WATCHED = [                # 被监控的 5 个服务（watchdog 不监控自己，见 _log_crash）
    "ntfy_tunnel.py",
    "pc_subscriber.py",
    "notification_listener.py",
    "ai_triager.py",
    "wechat_vision_listener.py",
]

PYW = HERE.parent / ".venv" / "Scripts" / "pythonw.exe"   # 和 start_services 同一个
REVIVE_INTERVAL = 300     # 同一服务 5 分钟内只自动拉起一次（防拉起风暴）
DISK_PCT = 90             # 磁盘使用率告警阈值（VPS 侧同值）
HB = LOG_DIR / "watchdog.hb"   # 每轮心跳时间戳，给 notification_listener 交叉检查

# 脚本名 -> pclog 服务名（日志文件名）。wechat 的日志叫 wechat_vision.log，
# 和脚本名不一致，映射一次省得两处各写一遍。
LOG_STEM = {
    "ntfy_tunnel.py": "ntfy_tunnel",
    "pc_subscriber.py": "pc_subscriber",
    "notification_listener.py": "notification_listener",
    "ai_triager.py": "ai_triager",
    "wechat_vision_listener.py": "wechat_vision",
}
LINK_LOGS = [LOG_STEM[s] for s in WATCHED]   # check_link 要扫的全部日志

import pclog
LOG = pclog.get_logger("watchdog")


# ---------------------------------------------------------------- 检查项
def _token() -> str:
    p = HERE / "ntfy.secret"
    return p.read_text().strip() if p.exists() else ""


_health_client: httpx.Client | None = None


def _health_cli() -> httpx.Client:
    """复用同一个 Client：顶层 httpx.get() 每次都新建连接池并初始化，
    实测 654ms + 458KB 读 + 泄漏 4 个句柄/次（60 次/小时 = 240 句柄/小时，
    Windows 单进程 10k 句柄限制下约 40 小时触顶）。trust_env 在构造时固定
    为 False，之后系统代理怎么翻转都不影响本连接 —— 这正是复用安全的前提。
    """
    global _health_client
    if _health_client is None or _health_client.is_closed:
        _health_client = httpx.Client(trust_env=False,
                                      timeout=httpx.Timeout(6.0))
    return _health_client


def check_health() -> tuple[bool, str]:
    try:
        r = _health_cli().get(HEALTH_URL,
                              headers={"Authorization": f"Bearer {_token()}"})
        if r.status_code == 200:
            return True, "health=200"
        return False, f"health=HTTP {r.status_code}"
    except Exception as e:
        # 连接池里的死连接（隧道重连过）由 httpx 自己丢弃重试一次；
        # 真失败就把 Client 扔掉，下轮重建，避免坏状态常驻。
        global _health_client
        if _health_client is not None:
            try: _health_client.close()
            except Exception: pass
            _health_client = None
        return False, f"health {type(e).__name__}: {e}"


def _our_pythons() -> list[str]:
    """本项目的 pythonw 命令行（只认 fuckpush\\pc\\ 下的，不碰 Hermes 的解释器）。

    为什么不用 PowerShell：watchdog 由 pythonw 启动、自己没有控制台，子进程
    powershell.exe 会新建一个前台窗口，每 60 秒闪一次约两秒。更糟的是
    PowerShell 一旦被安全软件拦截返回空，check_procs 会把 5 个服务全判成死，
    然后对着手机发一条纯误报的告警。psutil 是进程内遍历：无子进程、无窗口。

    process_iter 只取 name：cmdline 要逐个开进程句柄，对全系统几百个进程都取
    一次要 2 秒（还含大量 SYSTEM 进程 AccessDenied）；先按 name 过滤、再只对
    十几个 pythonw 取 cmdline，约 15ms。实测 15ms vs PowerShell 冷启动 3.5 秒。
    """
    out = []
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower() != "pythonw.exe":
                continue
            cmd = " ".join(p.cmdline() or [])
            if "fuckpush\\pc\\" in cmd.lower():
                out.append(cmd)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return out


def _counts() -> tuple[dict[str, int], int]:
    """{脚本名: 进程数}, 枚举到的本项目 pythonw 总数。"""
    counts: dict[str, int] = {}
    total = 0
    for cl in _our_pythons():
        m = re.search(r"([a-z_]+\.py)", cl)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
            total += 1
    return counts, total


def check_procs() -> tuple[bool, str]:
    # P2-1：判据从「< 2」改成「!= 2」。原来只查缺失 —— 并发双击两份编排
    # 把每个服务起成 4 个进程时（双弹 toast、双次 AI 分诊、手机双推）这里
    # 依然是绿的。上限同样算故障。
    counts, _ = _counts()
    missing = [s for s in WATCHED if counts.get(s, 0) < 2]
    extra = [s for s in WATCHED if counts.get(s, 0) > 2]
    parts = []
    if missing:
        parts.append(f"进程缺失 {missing}")
    if extra:
        parts.append(f"进程重复 {extra}")
    if parts:
        return False, "; ".join(parts) + f" (计数={counts})"
    return True, f"{len(WATCHED)} 服务进程齐"


def _log_fresh(stem: str, secs: float) -> bool:
    """该服务日志最近 secs 秒里还有没有新行 —— 进程枚举漏了它时的兜底判据。"""
    lines = pclog.read_tail(LOG_DIR / f"{stem}.log", 5)
    now = time.time()
    for ln in reversed(lines):
        ts = _log_ts(ln)
        if ts is not None:
            return now - ts <= secs
    return False


def revive_dead(now: float, last_revive: dict) -> None:
    """P1-1：进程死了要拉起来，不能只发一条告警等人回办公室。

    三条保守闸门，每条都是为了不制造比原故障更糟的双份进程：
    1. 只在计数 **恰为 0** 时动手 —— 计数 1 可能是 shim 正在派生真身
       （秒级），这时再起一份必然变成 3-4 个；计数 1 交给 check_procs 报警。
    2. 枚举总数为 0 时不动手 —— 那多半是权限/安全软件把 cmdline 全挡了，
       服务其实活着，按「全死」重启会造出双份。
    3. 该服务日志 2 分钟内还有动静也不动手 —— 同样是「枚举漏了它」的信号。
    同一服务 5 分钟内只重试一次，防止拉起失败变成每轮一次的进程风暴。
    """
    counts, total = _counts()
    if total == 0:
        return
    for svc in WATCHED:
        if counts.get(svc, 0) != 0:
            continue
        if _log_fresh(LOG_STEM[svc], 120):
            continue
        if now - last_revive.get(svc, 0) < REVIVE_INTERVAL:
            continue
        last_revive[svc] = now
        try:
            subprocess.Popen(
                [str(PYW), str(HERE / svc)], cwd=str(HERE),
                creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0)
                               | getattr(subprocess, "CREATE_NO_WINDOW", 0)))
            LOG.warning(f"revive: {svc} 计数 0 且日志静默，已拉起"
                        f"（{REVIVE_INTERVAL}s 内不重复拉）")
        except Exception as e:
            LOG.error(f"revive {svc} failed: {e!r}")


def _log_ts(line: str) -> float | None:
    """解析 pclog 时间戳 `2026-09-22T13:09:05.258+0800` -> epoch 秒。

    只取前 19 位按本地时间算 —— 我们只关心时间差，不跨时区。
    """
    try:
        return time.mktime(time.strptime(line[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


_ERR_PAT = re.compile(
    r"\[ERROR\]|ConnectError|10061|stream error|All connection attempts|"
    r"连接被拒绝|ReadTimeout|ConnectTimeout|peer closed", re.I)


def check_link() -> tuple[bool, str]:
    """进程活着但在刷错：最近 LINK_ERR_MIN 秒里错误够多、正常日志压不住。

    抓的是 2026-09-22 那种形态 —— subscriber 进程健在、每 7 秒一条
    ConnectError，健康检查（隧道）却是绿的，只有日志知道它废了。

    P0-1（第 6 轮）：原来只看 pc_subscriber / ai_triager 两个日志，
    notification_listener（WinRT 权限被撤后每 3s 一条 poll error）和
    wechat_vision（每轮 cycle error）一旦「进程活着但一直出错」，
    procs 绿、health 绿、link 也不看它 —— 手机零告警、采集链路静默停摆
    数天直到下次登录。现在 WATCHED 的 5 个日志全看。

    判据从「errs>=3 且 oks==0」放宽到「errs>=3 且 (oks==0 或 errs>=oks)」：
    原条件在「错误与正常日志并存」的半残状态下永远不成立（P2-8）——
    典型是 fp-feedback 单流 401 退避到 600s、另三条流还在正常打 INFO，
    反馈通道断半小时这里依然是绿的。
    """
    now = time.time()
    suspects = []
    for stem in LINK_LOGS:
        p = LOG_DIR / f"{stem}.log"
        if not p.exists():
            continue
        # 只读尾部：原来是 read_text().splitlines()[-60:] —— 为了 60 行
        # 把整个日志读进来。2MB 轮转阈值 x 5 个文件 x 每 60 秒一次 ≈ 14GB/天
        # 的纯磁盘读，全喂给页缓存。
        lines = pclog.read_tail(p, 60)
        if not lines:
            continue
        errs = oks = 0
        for ln in lines:
            ts = _log_ts(ln)
            if ts is None or now - ts > LINK_ERR_MIN:
                continue
            if _ERR_PAT.search(ln):
                errs += 1
            elif "[INFO" in ln or "[WARN" in ln:
                oks += 1
        if errs >= LINK_ERR_THRESHOLD and (oks == 0 or errs >= oks):
            suspects.append(f"{stem}: {errs} 条错误/{oks} 条正常"
                            f"（近 {LINK_ERR_MIN//60}min）")
    if suspects:
        return False, "link " + "; ".join(suspects)
    return True, "link 正常"


def check_disk() -> tuple[bool, str]:
    """P2-10：磁盘满了会先哑掉可观测性，而这里还是绿的。

    写满时 logging 把异常吞在 handleError 里（日志静默丢失）、jsonl 归档和
    state 写入全部失败 —— 去重失效变重复推送，watchdog 却 3/3 通过。
    VPS 侧早有 df>=90% 检查，PC 侧一直缺这条。
    """
    try:
        pct = psutil.disk_usage(str(HERE)).percent
    except Exception as e:
        return False, f"disk {type(e).__name__}: {e}"
    if pct >= DISK_PCT:
        return False, f"disk {pct}% >= {DISK_PCT}%（日志/state 写入会被静默吞掉）"
    return True, f"disk {pct}%"


CHECKS = [("health", check_health), ("procs", check_procs),
          ("link", check_link), ("disk", check_disk)]


# ------------------------------------------------- 告警通道（独立于隧道）
def _ntfy_payload(title: str, message: str) -> dict:
    # 告警自带一个新 trace，但必须用 with 恢复 —— 直接 set_trace_id(None)
    # 会把 trace 留在上下文里，之后的心跳日志全都挂上最后一次告警的 id，
    # grep 心跳时会串到无关的告警上。
    with pclog.trace(None):
        return {
            "topic": "fp-phone",
            "title": title[:120],
            "message": message[:900],
            "priority": 4,                 # hard rule: high, 不经 AI
            "tags": pclog.tags_with_trace(["bell", "rotating_light"]),
        }


def push_local(payload: dict) -> bool:
    """本地隧道（快）。隧道挂了这里必然失败 —— 由 push_vps 兜底。"""
    try:
        r = httpx.post(f"{NTFY_BASE}/", json=payload,
                       headers={"Authorization": f"Bearer {_token()}"},
                       timeout=8, trust_env=False)
        return r.status_code == 200
    except Exception as e:
        LOG.warning(f"local alert channel failed: {e!r}")
        return False


def push_vps(payload: dict) -> bool:
    """SSH 到 VPS，在它本机发 ntfy —— 隧道死了这条路还活着。

    payload 走 base64：JSON 里有中文和引号，塞进 shell 命令会被转义啃掉，
    base64 全 ASCII 就没这问题；VPS 端用 curl -d @file 读，零转义。
    """
    secret = HERE.parent / "vps.secret"
    if not secret.exists():
        LOG.error("vps.secret 不存在，无法走 SSH 兜底")
        return False
    host, port, user, pwd = secret.read_text().split()
    import paramiko

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(host, port=int(port), username=user, password=pwd,
                    timeout=15, banner_timeout=20, auth_timeout=20)
        b64 = base64.b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode()
        # curl 走 shell 命令行，两个约束：整条命令的单引号必须是偶数
        # （否则 shell 报未闭合、$() 拿到空串、push_vps 恒返回 False），
        # 以及 token 只在取得到时才带 header。token 出现在命令行只在自己的
        # VPS 上短暂可见（ps aux），可接受。
        tok = _token()
        auth = f"-H 'Authorization: Bearer {tok}' " if tok else ""
        cmd = (
            "set -e; "
            f"echo {b64} | base64 -d > /tmp/fp_watchdog_alert.json; "
            f"code=$(curl -s -o /dev/null -w '%{{http_code}}' "
            f"{auth}"
            "-H 'Content-Type: application/json' "
            "-d @/tmp/fp_watchdog_alert.json "
            "http://127.0.0.1:2586/); "
            "rm -f /tmp/fp_watchdog_alert.json; "
            "echo $code"
        )
        _, stdout, stderr = cli.exec_command(cmd, timeout=25)
        out = stdout.read().decode("utf-8", "replace").strip()
        err = stderr.read().decode("utf-8", "replace").strip()
        if out == "200":
            return True
        LOG.error(f"vps alert rejected: code={out!r} stderr={err[:200]!r}")
        return False
    except Exception as e:
        LOG.error(f"vps alert channel failed: {e!r}")
        return False
    finally:
        try:
            cli.close()
        except Exception:
            pass


def _record_echo(title: str, message: str) -> None:
    """把弹出去的 toast 记进 toast_echo.jsonl，让 notification_listener 认出
    这是自己人（P2-9）。

    不记的话，watchdog 自己弹的告警 toast 会被 listener 当成新通知重新入队
    分诊 —— 实测 18:17/18:18 有两条 ``PUSH [?] FxxkPush watchdog 通道测试``；
    断网期间还会变成每 30 分钟一条必失败的 PUSH_FAIL，恢复后再补推一条过期
    的「已恢复」。复用 pc_subscriber.record_echo（同一个文件、同一格式），
    导入失败只降级成日志：回环指纹缺失的后果由 listener 侧兜底。
    """
    try:
        from pc_subscriber import record_echo
        record_echo(title, message)
    except Exception as e:
        LOG.warning(f"record echo failed (listener may re-triage this toast): {e!r}")


def push_toast(title: str, message: str) -> bool:
    """Windows 原生 toast —— 零网络依赖，两条网络通道同时挂时的最后防线。

    2026-09-22 17:21-17:28 实测过这个缺口：ntfy 隧道 WinError 10061 和
    VPS SSH TimeoutError **同时**发生（前两条通道共享「外部网络」这一个
    故障域），watchdog 连打两条「告警两条通道都失败！」—— 最该报警的
    时刻一条都没送出去，等恢复了才把「已恢复」发出来。这条通道只碰本机
    通知中心：断网、隧道死、SSH 死都照样弹。

    AUMID 用 ``"Windows PowerShell"``（powershell.exe 在通知中心的注册名，
    Win10/11 内置，零第三方依赖）—— 实测 CreateToastNotifier 对它不抛，
    stdout 收到 SHOWN、returncode 0。中文经环境变量传，绕开命令行转义。
    """
    ps = (
        "$ErrorActionPreference='Stop'; "
        "[void][Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime]; "
        "$x=[Windows.UI.Notifications.ToastNotificationManager]::"
        "GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]"
        "::ToastText02); "
        "$t=$x.GetElementsByTagName('text'); "
        "$t.Item(0).AppendChild($x.CreateTextNode($env:FP_T))|Out-Null; "
        "$t.Item(1).AppendChild($x.CreateTextNode($env:FP_M))|Out-Null; "
        "$n=[Windows.UI.Notifications.ToastNotificationManager]::"
        "CreateToastNotifier('Windows PowerShell'); "
        "$n.Show([Windows.UI.Notifications.ToastNotification]::new($x)); "
        "Write-Output 'SHOWN'"
    )
    env = {**os.environ, "FP_T": title[:120], "FP_M": message[:900]}
    try:
        # 同步等退出码：Popen 不等待的话，「弹失败了」和「弹成功」在日志里
        # 长得一模一样，等于这条通道没有可观测性。
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True, text=True, timeout=20, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (p.stdout or "").strip()
        if p.returncode == 0 and "SHOWN" in out:
            _record_echo(title, message)   # 别让 listener 把这条告警再推一遍
            return True
        LOG.error(f"toast channel failed: rc={p.returncode} "
                  f"stderr={(p.stderr or '').strip()[:300]!r}")
        return False
    except Exception as e:
        LOG.error(f"toast channel failed: {e!r}")
        return False


def send_alert(title: str, message: str) -> bool:
    """降级链：本地隧道（秒回）→ VPS SSH → 本机 toast（零网络）。

    前两条共享「外部网络」故障域（17:21 实测同时挂），toast 只碰本机 ——
    三条里至少有一条在任何网络状态下都可用。
    """
    payload = _ntfy_payload(title, message)
    if push_local(payload):
        LOG.info(f"告警已送达(本地隧道) {title}")
        return True
    if push_vps(payload):
        LOG.info(f"告警已送达(VPS-SSH兜底) {title}")
        return True
    if push_toast(title, message):
        LOG.info(f"告警已送达(本机toast，两条网络通道都失败) {title}")
        return True
    LOG.error(f"告警三条通道都失败！{title}")
    return False


# ---------------------------------------------------------------- 主循环
def fmt_dur(sec: float) -> str:
    m = int(sec // 60)
    return f"{m}分钟" if m >= 1 else f"{int(sec)}秒"


def main() -> int:
    LOG.info(f"watchdog up: interval={CHECK_INTERVAL}s fail_threshold={FAIL_MIN}min")
    fault_since: dict[str, float] = {}
    last_alert: dict[str, float] = {}   # name -> 上次告警时间
    cycle = 0

    last_revive: dict[str, float] = {}   # svc -> 上次自动拉起时间

    while True:
        cycle += 1
        now = time.time()
        healthy_now = 0
        health_ok = False
        for name, fn in CHECKS:
            try:
                ok, detail = fn()
            except Exception as e:
                ok, detail = False, f"检查器异常 {type(e).__name__}: {e}"

            if ok:
                healthy_now += 1
                if name == "health":
                    health_ok = True
                if name in fault_since:
                    dur = now - fault_since.pop(name)
                    LOG.info(f"{name} 恢复（故障持续 {fmt_dur(dur)}）")
                    if name in last_alert:
                        # 告警过就告知恢复，否则静默（没打扰过就别吵）
                        send_alert(f"[FxxkPush] {name} 已恢复",
                                   f"故障持续 {fmt_dur(dur)} 后恢复正常。\n{detail}")
                        last_alert.pop(name, None)
                continue

            # 不健康
            start = fault_since.setdefault(name, now)
            dur = now - start
            if dur < FAIL_MIN * 60:
                LOG.warning(f"{name} 不健康 {fmt_dur(dur)}（阈值 {FAIL_MIN}min）: {detail}")
                continue

            # 超过阈值：告警（同一故障最多每 RENOTIFY 秒重复一次）
            prev = last_alert.get(name, 0)
            if now - prev < RENOTIFY:
                continue
            started = datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M:%S")
            msg = (f"检查项: {name}\n"
                   f"详情: {detail}\n"
                   f"已持续: {fmt_dur(dur)}（阈值 {FAIL_MIN} 分钟）\n"
                   f"首次发现: {started}\n"
                   f"影响: {'全链路推送中断' if name == 'health' else '对应服务不可用'}\n"
                   f"排查: pc/logs/*.log")
            LOG.error(f"告警 {name}: {detail}")
            # P2-7：只有**真送出去**才记 last_alert。原实现在 send_alert
            # 之前就打时间戳 —— 三条通道同时失败时（17:21 事故形态）同一
            # 故障 1800s 内不再尝试，最坏把整轮告警全丢掉。失败就下一轮
            # （60s 后）立刻重试。
            if send_alert(f"[FxxkPush] {name} 故障 {fmt_dur(dur)}", msg):
                last_alert[name] = now
            else:
                LOG.error(f"告警发送失败，下一轮重试: {name}")

        # P1-1：隧道是通的才自动拉起死掉的服务 —— 和 start_services 的健康门
        # 同一条哲学：隧道没就绪时把它们全拉起来，只会对着 2586 空转刷错
        # （v0.3.0 之前那次启动顺序事故的形态），健康门失败时尤其不能补刀。
        if health_ok:
            revive_dead(now, last_revive)

        # 心跳：全绿时也留痕迹，否则 watchdog 静默运行时分不清它是健康还是已经死了
        if healthy_now == len(CHECKS) and cycle % HEARTBEAT_CYCLES == 0:
            names = "/".join(n for n, _ in CHECKS)
            LOG.info(f"heartbeat: {len(CHECKS)}/{len(CHECKS)} 检查通过 "
                     f"({names}) 第{cycle}轮")

        # P0-2：每轮落一个心跳时间戳。watchdog 不在 WATCHED 里（它没法自己
        # 监控自己），日志里那句 heartbeat 也没有任何消费者 —— 它一死，三项
        # 巡检连同三层告警一起静默失效，而没有任何东西会发现。notification_listener
        # 每 3 分钟看一眼这个文件的 mtime，超过 15 分钟没更新就替它喊人。
        try:
            HB.write_text(str(int(now)), encoding="utf-8")
        except OSError as e:
            LOG.warning(f"heartbeat file write failed: {e!r}")

        time.sleep(CHECK_INTERVAL)


def _log_crash(exc_type, exc, tb) -> None:
    """P0-2：watchdog 自己崩了必须留痕 + 尽力报警。

    它是全系统唯一的看门人。没有 excepthook 的话，导入期/运行期异常在
    pythonw 下既没控制台也进不了 pclog（异常直接打到无处可去的 stderr），
    三层告警连同四项巡检一起静默失效，而这件事本身无人知晓 —— 这正是
    第 6 轮报的 P0。先落日志，再走一遍告警降级链，最后退出。
    """
    import traceback
    msg = "FATAL " + "".join(traceback.format_exception(exc_type, exc, tb)).strip()
    try:
        LOG.error(msg[-2000:])
    except Exception:
        pass
    try:
        send_alert("[FxxkPush] watchdog 自身崩溃",
                   msg[-600:] + "\n\n三层告警与巡检已失效，请尽快重启 watchdog")
    except Exception:
        pass
    os._exit(1)


if __name__ == "__main__":
    sys.excepthook = _log_crash
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
