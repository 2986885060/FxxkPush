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
_START_TS = time.time()        # 本进程启动时刻（r7-1：开机宽限期用）
REVIVE_GRACE = 300             # 起来后 5 分钟内禁止 revive —— 开机时 6 个
                               # HKCU Run 独立启动、首轮枚举/日志都没稳定，
                               # 这时候补刀最容易造出双份进程

# 各服务日志「安静多久算可疑」（秒）。它们的节奏完全不同：listener 事件驱动
# （+5min 心跳行）、triager/subscriber/tunnel 5min 心跳行、wechat 30min 一轮
# （夜间 10min 一行）。统一按 120s 判会把长轮询服务永久判成「有动静」或
# 把正常静默判成故障（r7-1/r7-2 的判据缺口，各服务已配对应心跳行）。
LOG_QUIET = {
    "ntfy_tunnel": 1800,
    "pc_subscriber": 900,
    "notification_listener": 900,
    "ai_triager": 900,
    "wechat_vision": 2700,      # 30min 轮 + 扫描耗时，留 15min 余量
}

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

# P2-1：告警标题/影响/排查按检查项枚举 —— 原来标题是英文内部名、影响只有
# 二分法（health/其它）、排查恒为通配 pc/logs/*.log，收到告警的人不知道
# 该开哪个文件。detail 里点得出服务名的（quiet/link/procs）直接给精确路径。
CHECK_CN = {"health": "服务健康", "procs": "服务进程", "link": "链路报错",
            "disk": "磁盘空间", "quiet": "日志静默", "vps": "VPS监控"}
CHECK_IMPACT = {
    "health": "全链路推送中断（ntfy 隧道不通，PC↔手机所有推送断开）",
    "procs": "对应服务未运行，它负责的功能停摆",
    "link": "服务活着但持续报错，对应功能半残",
    "disk": "磁盘不足：日志轮转/状态归档会失败，可观测性先死",
    "quiet": "进程在但日志心跳断了，可能已卡死或空转",
    "vps": "VPS 侧监控链路不可达，服务器告警收不到",
}
CHECK_DEBUG = {
    "health": "curl health 端点 + pc/logs/ntfy_tunnel.log",
    "disk": "清理磁盘；检查 pc/logs/*.log 轮转是否失效",
    "vps": "VPS 上: journalctl -u fuckpush-monitor -n 50 --no-pager",
    "link": "pc/logs/<出错服务>.log（详情已附最近一条错误原文）",
    "quiet": "详情点名的服务对应的 pc/logs/<stem>.log",
    "procs": "详情点名的服务对应的 pc/logs/<stem>.log；pc/start_services.py",
}


def _debug_hint(name: str, detail: str) -> str:
    """P2-1：detail 里点到的服务名 -> 精确日志路径；点不到用固定排查位。"""
    hits = sorted({s for s in LINK_LOGS if s in detail})
    if hits:
        return "、".join(f"pc/logs/{s}.log" for s in hits)
    return CHECK_DEBUG.get(name, "pc/logs/watchdog.log")


import pclog
from alert_fallback import push_vps, push_toast   # P2-5：2/3 层与 listener 共用
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
    """{脚本名: 进程数}, 5 个被监控服务的枚举总数（**不含 watchdog 自己**）。

    r7-1：原来 total 把 watchdog 自身 2 个进程也算进去，revive 的第一道闸
    「total == 0 才可信」永远 >= 2 —— 该闸实际不可达。现在总枚举数只统计
    WATCHED，「连自己都看得见」改由 revive 里单独检查（那是枚举是否被
    权限/AV 挡住的真探针）。
    """
    counts: dict[str, int] = {}
    for cl in _our_pythons():
        m = re.search(r"([a-z_]+\.py)", cl)
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts, sum(counts.get(s, 0) for s in WATCHED)


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

    四道保守闸门（r7-1 修订），每条都是为了不制造比原故障更糟的双份进程：
    0. 本进程起来 5 分钟内不动手 —— 开机时 6 个 HKCU Run 各自独立启动、
       无编排，首轮枚举早于某些服务写出首行日志，这时「计数 0 + 日志静默」
       是必然状态而不是故障信号。
    1. **连 watchdog 自己都枚举不到**（counts 里没有 watchdog.py）才认为
       枚举被权限/安全软件挡住、0 不可信 —— 这才是「枚举是否可靠」的真探针。
       （原来的「total==0」把 watchdog 自己算进 total，恒 >=2，该闸不可达。）
    2. 只在计数 **恰为 0** 时动手 —— 计数 1 可能是 shim 正在派生真身
       （秒级），这时再起一份必然变成 3-4 个；计数 1 交给 check_procs 报警。
    3. 该服务日志在**它自己的节奏窗口**内有动静就不动手（LOG_QUIET，
       120s 一刀切会把 30min 一轮的 wechat / 事件驱动的 listener 永久判成
       「有动静」= 永远不拉起）。
    同一服务 5 分钟内只重试一次，防止拉起失败变成每轮一次的进程风暴。
    """
    if time.time() - _START_TS < REVIVE_GRACE:
        return
    counts, _ = _counts()
    if counts.get("watchdog.py", 0) == 0:
        return          # 枚举不可靠（连自己都看不见）
    for svc in WATCHED:
        if counts.get(svc, 0) != 0:
            continue
        if _log_fresh(LOG_STEM[svc], LOG_QUIET[LOG_STEM[svc]]):
            continue
        if _elapsed(now, last_revive.get(svc, 0)) < REVIVE_INTERVAL:
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
        err_secs: set[int] = set()
        last_err = None            # P2-1：窗口内最近一条错误原文，告警里附带
        for ln in lines:
            ts = _log_ts(ln)
            if ts is None or now - ts > LINK_ERR_MIN:
                continue
            if _ERR_PAT.search(ln):
                # r7-2：一次异常的多行 traceback 逐行计入的话，单次异常就
                # 瞬间满足 errs>=3。同一秒的多行 = 同一次异常，算 1 条。
                err_secs.add(int(ts))
                last_err = ln      # P2-1：拿到实证，不用再让人对着 stem 猜
            elif "[INFO" in ln:
                # r7-2：[WARN] 不算「正常」—— 慢速重试刷 WARN 时 oks 被灌大，
                # errs<oks 恒绿，真正的错误被压住。
                oks += 1
        errs = len(err_secs)
        if errs >= LINK_ERR_THRESHOLD and (oks == 0 or errs >= oks):
            seg = (f"{stem}: {errs} 条错误/{oks} 条正常"
                   f"（近 {LINK_ERR_MIN//60}min）")
            if last_err:
                seg += f"｜最近错误: {last_err.strip()[:120]}"
            suspects.append(seg)
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


def check_quiet() -> tuple[bool, str]:
    """r7-2/r7-10：反向判据 ——「活着但没干活」和「日志写不进去」。

    check_link 只在**错误很多**时判红，纯静默（进程活着、零输出、日志文件
    缺失/轮转失败后写不进去）恒绿 —— 正是 P0-1 想抓的静默停摆形态留下的
    一半缺口。各服务已配周期心跳行（listener/triager/subscriber/tunnel 每
    5min、wechat 每轮/夜间每 10min），超过 LOG_QUIET 没写任何东西 = 心跳断了。
    刚启动时不判（日志还没首行）。
    """
    if time.time() - _START_TS < REVIVE_GRACE:
        return True, "quiet 启动宽限期"
    suspects = []
    for stem, budget in LOG_QUIET.items():
        p = LOG_DIR / f"{stem}.log"
        if not p.exists():
            suspects.append(f"{stem}: 日志文件缺失")
            continue
        last = None
        for ln in reversed(pclog.read_tail(p, 5)):
            last = _log_ts(ln)
            if last is not None:
                break
        if last is None:
            suspects.append(f"{stem}: 日志解析不出时间戳")
        elif time.time() - last > budget:
            suspects.append(f"{stem}: 静默 {int(time.time() - last)}s > {budget}s")
    if suspects:
        return False, "quiet " + "; ".join(suspects)
    return True, "quiet 正常"


# r8 P0-1：VPS 侧监控的存活与可达 —— PC 侧原来对它**零覆盖**。
# VPS 的 fuckpush-monitor 死了/被停/token 失效时，PC 五项全绿、手机零告警，
# 而它才是 OOM/磁盘/SSH 登录/unit 挂掉这些硬规则的唯一采集者 —— 典型的
# 「最需要它的时刻它哑了，还没人知道」。
VPS_CHECK_INTERVAL = 300   # SSH 探测 VPS 的最小间隔（结果缓存，别每 60s 都连）
VPS_FAIL_TS_WINDOW = 900   # gray.log 最后一条 push-failed 多新才算「推送坏了」
_vps_cache: dict = {"ts": 0.0, "ok": True, "detail": "vps 尚未首查"}


def check_vps() -> tuple[bool, str]:
    """VPS 监控存活 + 推送可达（每 VPS_CHECK_INTERVAL 秒真探一次，余下吃缓存）。

    三种要抓的黑屏形态（第 8 轮报告 P0-1）：
      1. fuckpush-monitor 持续死亡或持续崩溃循环 —— systemd 的
         Restart=on-failure 兜得住瞬时崩溃，兜不住「起来就崩」（那种
         is-active 会停在 activating，不算活）；
      2. FP_NTFY_TOKEN 失效 -> _publish 恒 401 -> 只落它自己的 gray.log，
         PC 的 health 用的是 PC 自己的 token、照常 200（2026-09-22 实测
         两个 token 同值且都 200，此形态当下未发生，但没有机制能发现它
         哪天发生 —— 这条检查就是那个机制）；
      3. 崩溃循环的「启动 push」救不了场 —— 它会被 AI 判「忽略」静默归档，
         再被 dedup 压 1800s。

    判据与故障域切分：
      - SSH 通 -> 看 systemctl is-active（active 才算活）+ gray.log 最后
        一条是否为新近的 ntfy-push-failed（推送可达性，只报「已知坏」
        不报「未知好」）；
      - SSH 不通 -> 先探本地隧道：隧道通说明 VPS 活着、是本检查自己的
        凭据/端口坏了（health 不会报这个）-> 红；隧道也不通则是整体网络
        故障，health 必然也红 -> 这里返回绿让 health 去喊，避免同一
        故障域双报（第 8 轮 P2-2 的告警风暴缺口）。
    结果缓存 VPS_CHECK_INTERVAL 秒：一次 SSH 最多十几秒，不能每轮都拖。
    """
    now = time.time()
    if now - _vps_cache["ts"] < VPS_CHECK_INTERVAL:
        return _vps_cache["ok"], _vps_cache["detail"]

    def _set(ok: bool, detail: str) -> tuple[bool, str]:
        _vps_cache.update(ts=now, ok=ok, detail=detail)
        return ok, detail

    # 全程按 P1-2 的教训包住：这条检查自己绝不许抛（抛了会被主循环记成
    # 「检查器异常」红 5 分钟，制造假告警）
    try:
        secret = HERE.parent / "vps.secret"
        if not secret.exists():
            return _set(False, "vps: vps.secret 缺失（无法探测 VPS 监控）")
        host, port, user, pwd = secret.read_text().split()
        import paramiko
        cli = None
        try:
            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            cli.connect(host, port=int(port), username=user, password=pwd,
                        timeout=10, banner_timeout=15, auth_timeout=10)
            _, out, err = cli.exec_command(
                "systemctl is-active fuckpush-monitor 2>/dev/null; echo ' --- '; "
                "tail -1 /var/log/fuckpush/gray.log 2>/dev/null",
                timeout=15)
            text = out.read().decode("utf-8", "replace")
        finally:
            if cli is not None:
                try:
                    cli.close()
                except Exception:
                    pass
    except Exception as e:
        # SSH 失败：先问隧道。隧道活着 -> 是本检查的凭据/端口坏了（health
        # 永远不会报这个，必须这里红）；隧道也死 -> 网络故障域，交 health。
        tunnel_ok = False
        try:
            tunnel_ok = httpx.get(f"{NTFY_BASE}/v1/health", timeout=5,
                                  trust_env=False).status_code == 200
        except Exception:
            tunnel_ok = False
        if tunnel_ok:
            return _set(False, f"vps: SSH 探测失败(隧道却通,凭据/端口问题): "
                               f"{type(e).__name__}: {e}")
        return _set(True, "vps: SSH 与隧道都不通=网络故障域，由 health 覆盖")

    active_part, _, gray_part = text.partition(" --- ")
    active = active_part.strip()
    if active != "active":
        return _set(False, f"vps: fuckpush-monitor 状态={active or '空'}"
                           f"（持续崩溃或被停，硬规则告警全发不出）")

    # gray.log 最后一条是新近的 ntfy-push-failed -> token 失效/ntfy 拒收
    gray_line = gray_part.strip().splitlines()
    if gray_line:
        try:
            last = json.loads(gray_line[-1])
            if (last.get("kind") == "ntfy-push-failed"
                    and now - float(last.get("ts", 0)) < VPS_FAIL_TS_WINDOW):
                data = last.get("data") or {}
                return _set(False, "vps: 推送被拒 http="
                           f"{data.get('http_code')}（token 失效或 ntfy 拒收，"
                           f"VPS 硬规则发不出去，只落了它本地 gray.log）")
        except Exception:
            pass    # 尾行不是 JSON/时间戳坏 -> 不据此判红，is-active 已覆盖
    return _set(True, "vps: monitor active 且无新推送失败")


CHECKS = [("health", check_health), ("procs", check_procs),
          ("link", check_link), ("disk", check_disk),
          ("quiet", check_quiet), ("vps", check_vps)]


def _elapsed(now: float, then: float) -> float:
    """r7-8：墙钟差值钳制。NTP/手动把系统时间往回拨几小时后，now-then 变负 ——
    故障 dur 永远到不了 FAIL_MIN（永不告警）、last_alert 落在未来让
    now-prev<RENOTIFY 恒真（告警被永久静音）、last_revive 同理（该服务
    永久不再被拉起）。差值为负一律按「已过期/刚发生」处理：让判定继续走，
    最多多告警一次，好过无声失效。日志时间戳与墙钟同步回拨，这里挡不住，
    但那只会让下一轮巡检用同一套新时间重算（自洽）。
    """
    d = now - then
    return d if d >= 0 else float("inf")


# ------------------------------------------------- 告警通道（独立于隧道）
def _cut(s: str, n: int) -> str:
    """r8 P2-7：截断必须留标记。原来 message[:900] 悄悄砍掉「影响/排查」
    的尾巴，收的人看不出被截 —— 照着半截指引排查只会浪费时间。"""
    return s if len(s) <= n else s[:n - 8] + "…(已截断)"


def _ntfy_payload(title: str, message: str) -> dict:
    # 告警自带一个新 trace，但必须用 with 恢复 —— 直接 set_trace_id(None)
    # 会把 trace 留在上下文里，之后的心跳日志全都挂上最后一次告警的 id，
    # grep 心跳时会串到无关的告警上。
    with pclog.trace(None):
        return {
            "topic": "fp-phone",
            "title": _cut(title, 120),
            "message": _cut(message, 900),
            "priority": 4,                 # hard rule: high, 不经 AI
            "tags": pclog.tags_with_trace(["bell", "rotating_light"]),
        }


def push_local(payload: dict) -> bool:
    """本地隧道（快）。隧道挂了这里必然失败 —— 由 push_vps 兜底。"""
    try:
        r = httpx.post(f"{NTFY_BASE}/", json=payload,
                       headers={"Authorization": f"Bearer {_token()}"},
                       timeout=8, trust_env=False)
        # r8 P1-3③：记下服务端分配的 message id —— 「已送达」原来只证明
        # 收到 200，事后想和手机端对账（到底推的哪条）没有抓手。
        if r.status_code == 200:
            try:
                mid = (r.json() or {}).get("id")
                if mid:
                    LOG.info(f"alert message id={mid} title={payload.get('title', '')[:60]}")
            except Exception:
                pass    # 响应体解析失败不影响送达判定
        return r.status_code == 200
    except Exception as e:
        LOG.warning(f"local alert channel failed: {e!r}")
        return False


def send_alert(title: str, message: str) -> bool:
    """降级链：本地隧道（秒回）→ VPS SSH → 本机 toast（零网络）。

    前两条共享「外部网络」故障域（17:21 实测同时挂），toast 只碰本机 ——
    三条里至少有一条在任何网络状态下都可用。
    """
    # r8 P1-2：每层各自 try —— push_local/push_vps/push_toast 内部虽有
    # 兜底，但 _ntfy_payload 的 pclog.trace/tags、以及未来任何一层的新增
    # 代码抛异常，都会让后面的层**永远没机会执行**（尤其 toast 这条零网络
    # 最后防线）。一层失败只降级，绝不击穿整条链。
    payload = None
    try:
        payload = _ntfy_payload(title, message)
    except Exception as e:
        LOG.error(f"alert payload build failed: {e!r}")
    if payload is not None:
        try:
            if push_local(payload):
                LOG.info(f"告警已送达(本地隧道) {title}")
                return True
        except Exception as e:
            LOG.error(f"local alert channel raised: {e!r}")
        try:
            if push_vps(payload, LOG):
                LOG.info(f"告警已送达(VPS-SSH兜底) {title}")
                return True
        except Exception as e:
            LOG.error(f"vps alert channel raised: {e!r}")
    try:
        if push_toast(title, message, LOG):
            LOG.info(f"告警已送达(本机toast，两条网络通道都失败) {title}")
            return True
    except Exception as e:
        LOG.error(f"toast channel raised: {e!r}")
    LOG.error(f"告警三条通道都失败！{title}")
    return False


# r8 P1-3①：端到端金丝雀。三层告警的「已送达」都只证明**ntfy 服务端收了
# 200**，手机 App 是否还在订阅、有没有被 doze/卸载/退订，任何一层都自证
# 不了 —— 只能靠一条固定节奏、固定标题的 priority=1（最低档，不响铃）消息，
# 人眼核对「今天这条到没到」。间隔 24h：每天一条，缺了=最后一跳断了。
CANARY_INTERVAL = 24 * 3600
CANARY_TITLE = "[FxxkPush] 链路自检"


def send_canary() -> bool:
    """发一条 canary；失败不风暴，由主循环按 CANARY_INTERVAL 节奏驱动。

    走 send_alert 全链（local -> vps -> toast）：canary 本身也是对三层降级
    的日常演练。payload 用 priority=1（最低档，只入列不响铃）。
    """
    try:
        payload = _ntfy_payload(CANARY_TITLE,
                                "金丝雀：手机最后一跳是否可达，看这条。"
                                "固定每 24h 一条；哪天没收到 = App 退订/被杀/"
                                "勿扰吞了通知。")
        payload["priority"] = 1     # 最低档：能入列就行，别响铃
        if push_local(payload):
            LOG.info("canary 已送达(本地隧道)")
            return True
        if push_vps(payload, LOG):
            LOG.info("canary 已送达(VPS-SSH兜底)")
            return True
        if push_toast(CANARY_TITLE,
                      "canary：两条网络通道都失败（本机 toast 兜底）", LOG):
            LOG.warning("canary 仅本机 toast（网络通道全失败，本身即故障信号）")
            return True
        LOG.error("canary 三条通道都失败")
        return False
    except Exception as e:
        LOG.error(f"canary 异常: {e!r}")
        return False


# ---------------------------------------------------------------- 主循环
def fmt_dur(sec: float) -> str:
    m = int(sec // 60)
    return f"{m}分钟" if m >= 1 else f"{int(sec)}秒"


def main() -> int:
    LOG.info(f"watchdog up: interval={CHECK_INTERVAL}s fail_threshold={FAIL_MIN}min")
    fault_since: dict[str, float] = {}
    last_alert: dict[str, float] = {}   # name -> 上次告警时间
    last_any_alert = 0.0                # P2-2 全局键：任何一条告警的发送时间
    recover_pending: dict[str, tuple[str, float, str]] = {}  # P2-2 发失败的恢复通知
    cycle = 0

    last_revive: dict[str, float] = {}   # svc -> 上次自动拉起时间
    # r8 P1-3①：启动即发第一条 canary（人工立刻能核对「手机收得到吗」），
    # 之后每 CANARY_INTERVAL 一条。失败置 0 -> 下一轮（60s）重试，但成功后
    # 才打时间戳，所以失败风暴上限是 1 次/分钟且三层全挂时本就是故障态。
    last_canary = 0.0

    while True:
        cycle += 1
        now = time.time()
        # r8 P2-15：_elapsed 钳制 —— 时钟回拨后 now-last_canary 为负，
        # 裸比较会让金丝雀永久哑掉；钳制成 inf = 立即再发一条，多发无害。
        if _elapsed(now, last_canary) >= CANARY_INTERVAL:
            if send_canary():
                last_canary = now
            else:
                LOG.error("canary 发送失败，60s 后重试")
        healthy_now = 0
        health_ok = False
        red_batch: list[tuple[str, str, float, float]] = []  # name, detail, dur, start
        rec_batch: list[tuple[str, float, str]] = []         # name, dur, detail
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
                    # r8 P1-2：与告警段同理，恢复通知的组装/发送也整体隔离
                    try:
                        dur = _elapsed(now, fault_since.pop(name))
                        if dur == float("inf"):
                            dur = 0.0     # 时间回拨：按「刚恢复」展示，别报负数
                        LOG.info(f"{name} 恢复（故障持续 {fmt_dur(dur)}）")
                        if name in last_alert:
                            # 告警过就告知恢复，否则静默（没打扰过就别吵）。
                            # P2-2：攒进 rec_batch 同轮合并发送；发送失败真进
                            # recover_pending 下一轮重试 —— 原实现 pop 了
                            # fault_since 后「下一轮重试」根本回不来（r7-13
                            # 留的 last_alert 只在再出新故障时才起作用）。
                            rec_batch.append((name, dur, detail))
                    except Exception as e:
                        LOG.error(f"恢复通知段异常(已隔离): {name} {e!r}")
                continue

            # 不健康
            start = fault_since.setdefault(name, now)
            dur = max(0.0, now - start)   # r7-8：回拨后为负 -> 视为刚发现
            if dur < FAIL_MIN * 60:
                LOG.warning(f"{name} 不健康 {fmt_dur(dur)}（阈值 {FAIL_MIN}min）: {detail}")
                continue
            # 超阈值：先攒批发批处理段（P2-2），双键决定发不发
            red_batch.append((name, detail, dur, start))

        # ---- P2-2：同轮红项合并成一条「N 项异常」，恢复也合并 ----
        # 单项键：同一项每 RENOTIFY 才重复；全局键：任何告警刚发过，重复项
        # 先憋着 —— 否则 5 项错峰到期就是每轮各发各的、240 条/天（backlog
        # P2-2）。首次告警不等全局键：新故障要立刻喊。发送失败不落任何时间
        # 戳，下一轮（60s）整批重试（P2-7 语义原样保留）。
        try:
            if red_batch:
                due = [(n, d, du, st_) for n, d, du, st_ in red_batch
                       if n not in last_alert
                       or (_elapsed(now, last_alert[n]) >= RENOTIFY
                           and _elapsed(now, last_any_alert) >= RENOTIFY)]
                if due:
                    parts, max_dur = [], 0.0
                    for n, d, du, st_ in due:
                        started = datetime.fromtimestamp(st_).strftime("%Y-%m-%d %H:%M:%S")
                        LOG.error(f"告警 {n}: {d}")
                        max_dur = max(max_dur, du)
                        parts.append(
                            f"检查项: {CHECK_CN.get(n, n)}（{n}）\n"
                            f"详情: {d}\n"
                            f"已持续: {fmt_dur(du)}（阈值 {FAIL_MIN} 分钟）\n"
                            f"首次发现: {started}\n"
                            f"影响: {CHECK_IMPACT.get(n, '对应功能不可用')}\n"
                            f"排查: {_debug_hint(n, d)}")
                    if len(due) == 1:
                        n0 = due[0][0]
                        title = f"[FxxkPush] {CHECK_CN.get(n0, n0)}故障 {fmt_dur(due[0][2])}"
                    else:
                        title = f"[FxxkPush] {len(due)} 项检查异常（最长 {fmt_dur(max_dur)}）"
                    if send_alert(title, "\n\n".join(parts)):
                        sent_at = time.time()
                        for n, *_ in due:
                            last_alert[n] = sent_at   # P2-7：真送出去才记
                        last_any_alert = sent_at
                    else:
                        LOG.error(f"告警发送失败，下一轮重试: {[n for n, *_ in due]}")
        except Exception as e:
            # r8 P1-2：msg 组装/send_alert 外围的任何异常都不许穿透到主循环
            LOG.error(f"告警段异常(已隔离, 下一轮重试): {[n for n, *_ in red_batch]} {e!r}")

        # ---- P2-2：恢复合并成一条；失败进 recover_pending，下一轮真重试 ----
        try:
            cands: dict[str, tuple[str, float, str]] = {}
            for item in recover_pending.values():
                if item[0] not in fault_since:    # 又红了 = 恢复取消，不发旧账
                    cands[item[0]] = item
            for item in rec_batch:
                cands[item[0]] = item             # 本轮新恢复覆盖旧的
            if cands:
                parts = [f"检查项: {CHECK_CN.get(n, n)}（{n}）\n"
                         f"已恢复（故障持续 {fmt_dur(d)}）\n{det}"
                         for n, d, det in cands.values()]
                if len(cands) == 1:
                    title = f"[FxxkPush] {next(iter(cands))} 已恢复"
                else:
                    title = f"[FxxkPush] {len(cands)} 项检查已恢复"
                if send_alert(title, "\n\n".join(parts)):
                    for n in cands:
                        last_alert.pop(n, None)
                        recover_pending.pop(n, None)
                else:
                    for n, d, det in cands.values():
                        recover_pending[n] = (n, d, det)
                    LOG.error(f"恢复通知发送失败，下一轮重试: {list(cands)}")
        except Exception as e:
            LOG.error(f"恢复通知段异常(已隔离): {e!r}")

        # P1-1：隧道是通的才自动拉起死掉的服务 —— 和 start_services 的健康门
        # 同一条哲学：隧道没就绪时把它们全拉起来，只会对着 2586 空转刷错
        # （v0.3.0 之前那次启动顺序事故的形态），健康门失败时尤其不能补刀。
        if health_ok:
            # r8 P1-2：revive_dead 里有 Popen/psutil/文件读写，异常不能
            # 带走整个巡检循环（excepthook → send_alert 再炸就直接停摆）。
            try:
                revive_dead(now, last_revive)
            except Exception as e:
                LOG.error(f"revive_dead 异常(已隔离): {e!r}")

        # 心跳：全绿时也留痕迹，否则 watchdog 静默运行时分不清它是健康还是已经死了
        if healthy_now == len(CHECKS) and cycle % HEARTBEAT_CYCLES == 0:
            names = "/".join(n for n, _ in CHECKS)
            LOG.info(f"heartbeat: {len(CHECKS)}/{len(CHECKS)} 检查通过 "
                     f"({names}) 第{cycle}轮")
        elif cycle % HEARTBEAT_CYCLES == 0:
            # r7-10：非全绿也留心跳 —— 否则「巡检在跑但有项红着」和
            #「巡检卡死」在日志里同形，只能靠翻上一条 ERROR 猜。
            red = [n for n, _ in CHECKS if n in fault_since]
            LOG.warning(f"heartbeat: {len(CHECKS) - len(red)}/{len(CHECKS)} "
                        f"通过，红={red} 第{cycle}轮")

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
