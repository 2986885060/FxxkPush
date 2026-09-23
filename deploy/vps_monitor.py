#!/usr/bin/env python3
"""FuckPush VPS monitor — hard rules + gray-zone spool.

Runs on the VPS. Single process, tiny memory (~20MB).

Hard rules (grep-level, direct ntfy push to phone):
  - OOM killer events
  - disk usage > 90%
  - SSH login success (root) — with sliding-window dedup
  - systemd unit failure (crash loop)
  - s-ui / maddy / nginx process death

Gray zone (new error patterns, weird frequency) → append to
/var/log/fuckpush/gray.log in JSON lines; the PC picks them up for AI triage.

Dedup: every push kind has a cooldown (default 30 min). Repeated events
inside cooldown increment a counter and are summarized in the next push.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

# ---------- config ----------
NTFY_URL = os.environ.get("FP_NTFY_URL", "http://127.0.0.1:2586/")
NTFY_TOKEN = os.environ.get("FP_NTFY_TOKEN", "")
# IPs that never trigger a "root SSH login" alert (e.g. the VPS's own address,
# or your home/office egress). Comma separated.
SELF_IPS = tuple(x.strip() for x in os.environ.get("FP_SELF_IPS", "").split(",") if x.strip())
TOPIC_HARD = "fp-vps"        # hard-rule alerts -> PC (PC relays to phone after AI)
TOPIC_GRAY = "fp-gray"       # gray-zone events -> PC for AI triage
TOPIC_HB = "fp-vps-hb"       # heartbeat -> PC watchdog pulls (reverse probe, B方案)
GRAY_LOG = Path("/var/log/fuckpush/gray.log")
STATE_FILE = Path("/var/lib/fuckpush/state.json")
COOLDOWN_DEFAULT = 1800      # 30 min
CHECK_INTERVAL = 60          # main loop, seconds
HB_INTERVAL = 300            # heartbeat every 5 min (PC watchdog judges by age)
DISK_THRESHOLD = 90          # percent

UNITS_TO_WATCH = ["s-ui", "maddy", "nginx", "ntfy"]

# ---------- state / dedup ----------
_state = {"cooldowns": {}, "counts": {}}

def load_state():
    if STATE_FILE.exists():
        try:
            _state.update(json.loads(STATE_FILE.read_text()))
        except Exception:
            pass
    # P2-8：形状校验。文件可能变成「合法 JSON 但类型不对」（手工改坏、或
    # 半截内容被外力拼成合法结构），那时 allowed() 里的 .get 会在每个
    # check_* 里恒抛 -> gray("monitor-error") 每 60s 一条，而灰区要过 AI
    # 大概率被判忽略 —— 等于硬规则永久失效且无人发现。类型不对就重置：
    # 冷却状态丢了可以重建，最坏是同一件事多推一次，远好过永不推送。
    if not isinstance(_state.get("cooldowns"), dict):
        _state["cooldowns"] = {}
    if not isinstance(_state.get("counts"), dict):
        _state["counts"] = {}
    if not isinstance(_state.get("unit_seen"), dict):
        _state["unit_seen"] = {}

def save_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # P2-8：原子写（PC 侧早就 tmp+replace，这里是最后一处直写）。
    # write_text 被 OOM kill / 断电打断会留下半截 JSON，load_state 的
    # try 只能整份丢弃 -> 冷却全失、同一批告警风暴重推。
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(_state))
    os.replace(tmp, STATE_FILE)

def allowed(kind, cooldown=COOLDOWN_DEFAULT):
    """True if this push kind is outside its cooldown window."""
    last = _state["cooldowns"].get(kind, 0)
    now = time.time()
    if now - last >= cooldown:
        _state["cooldowns"][kind] = now
        _state["counts"][kind] = 0
        return True
    _state["counts"][kind] = _state["counts"].get(kind, 0) + 1
    return False

# ---------- ntfy ----------
def _publish(topic, title, message, priority=4, tags=None):
    payload = json.dumps({
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags or ["rotating_light"],
    }).encode("utf-8")
    # curl 必须自带超时：这是唯一的采集侧组件，网络卡住会让整个监控主循环
    # 停在 push 这一步（journalctl/systemctl/df 同样补了 timeout=30）。
    # header 只在配置了 FP_NTFY_TOKEN 时才带。
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
           "--max-time", "20"]
    if NTFY_TOKEN:
        cmd += ["-H", f"Authorization: Bearer {NTFY_TOKEN}"]
    cmd += ["-H", "Content-Type: application/json", "-d", "@-", NTFY_URL]
    p = subprocess.run(cmd, input=payload, capture_output=True, timeout=30)
    code = p.stdout.decode().strip()
    if code != "200":
        # never die on push failure; local spool as fallback
        _gray_write(json.dumps({"ts": time.time(), "kind": "ntfy-push-failed",
                                "data": {"http_code": code, "title": title}},
                               ensure_ascii=False))
    return code == "200"

def push(title, message, priority=4, tags=None):
    return _publish(TOPIC_HARD, title, message, priority, tags)

def _gray_write(line: str) -> None:
    """Append one spool line to gray.log, rotating past 512KB -> .1.

    PC-side review round 4 gave all four jsonls a rotation cap and left this
    file as "deploy together next time" — this is that change. append-only
    with no cap means dozens of MB a year of gray-zone spool on a 1C1G box.
    Rotation failure must not drop the line being written, hence the guard.
    """
    GRAY_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        if GRAY_LOG.exists() and GRAY_LOG.stat().st_size > 512 * 1024:
            bak = GRAY_LOG.with_name(GRAY_LOG.name + ".1")
            bak.unlink(missing_ok=True)
            GRAY_LOG.replace(bak)
    except Exception:
        pass
    with GRAY_LOG.open("a", encoding="utf-8") as f:
        f.write(line if line.endswith("\n") else line + "\n")


def gray(kind, data):
    """Gray-zone event: local spool + publish to fp-gray for the PC.

    r7-11：整体必须吞异常 —— main 的 except 里正是调 gray("monitor-error")，
    gray 自己再抛（磁盘满 -> mkdir/open 失败、publish 的 subprocess 超时）
    会穿透 main -> 监控进程退出，而 PC 侧对 VPS 监控零覆盖，一崩全盲。
    灰区通道宁可丢一条事件，也不能把主监控带走。
    """
    try:
        _gray_impl(kind, data)
    except Exception as e:
        try:
            print(f"gray({kind}) failed: {e!r}", flush=True)
        except Exception:
            pass


def _gray_impl(kind, data):
    entry = {"ts": time.time(), "kind": kind, "data": data}
    line = json.dumps(entry, ensure_ascii=False)
    _gray_write(line)
    # P2-11：message 用同一行 JSON。原实现引用了**不存在的变量 line** ->
    # NameError，于是每条灰区事件（磁盘百分比变化 / OOM / 新 SSH IP / 非监控
    # unit failed，以及 main 的 except 兜底）都会炸；main 的 except 里再调
    # gray 会二次 NameError 直接穿透 -> 监控进程退出，而磁盘百分比几乎每小时
    # 必变，部署后很快崩。PC 侧 watchdog 对 VPS 监控零覆盖，这一崩就全盲。
    # （代码修复，**上 VPS 部署后才生效** —— 需要用户点头。）
    _publish(TOPIC_GRAY, f"灰区事件: {kind}", line[:500], priority=1,
             tags=["eyes"])

# ---------- hard rules ----------
def check_oom(since_ts):
    # kernel OOM lines
    p = subprocess.run(
        ["journalctl", "-k", "--since", f"@{int(since_ts)}", "--no-pager"],
        capture_output=True, text=True, timeout=30)
    kills = re.findall(
        r"Out of memory: Killed process .*?(\d+) \((.*?)\)", p.stdout)
    if kills:
        procs = ", ".join(sorted({name for _, name in kills}))
        if allowed("oom"):
            n = _state["counts"].get("oom", 0)
            extra = f" (+{n} suppressed)" if n else ""
            push("VPS 内存不足 OOM", f"被杀进程: {procs}{extra}", priority=4)
    for pid, name in kills:
        gray("oom-kill", {"pid": pid, "process": name})

def check_disk():
    p = subprocess.run(["df", "-P", "/"], capture_output=True, text=True, timeout=30)
    m = re.search(r"(\d+)%", p.stdout)
    if not m:
        return
    pct = int(m.group(1))
    if pct >= DISK_THRESHOLD:
        if allowed("disk", cooldown=3600):
            n = _state["counts"].get("disk", 0)
            extra = f" (+{n} suppressed)" if n else ""
            push("VPS 磁盘告急", f"根分区 {pct}% 已用{extra}", priority=4)
    else:
        # gray-log only when the number changes (avoid per-minute spam)
        if _state.get("last_disk_pct") != pct:
            _state["last_disk_pct"] = pct
            gray("disk-usage", {"percent": pct})

def check_ssh_login(since_ts):
    p = subprocess.run(
        ["journalctl", "-u", "ssh", "--since", f"@{int(since_ts)}",
         "--no-pager", "-g", "Accepted (password|publickey)"],
        capture_output=True, text=True, timeout=30)
    logins = re.findall(r"Accepted \S+ for (\S+) from (\S+)", p.stdout)
    for user, ip in logins:
        rec = {"user": user, "ip": ip}
        # dedupe: log each (user, ip) once per monitor lifetime
        key = f"seen-login:{user}:{ip}"
        if not _state.get(key):
            _state[key] = True
            gray("ssh-login", rec)
        # root login or any login from a new IP is push-worthy.
        # skip monitor/paramiko self-connections from the VPS itself
        # and our own PC's known egress below.
        if ip in SELF_IPS or ip.startswith("127."):
            continue
        if user == "root" and allowed(f"ssh-login:{ip}", cooldown=86400):
            n = _state["counts"].get(f"ssh-login:{ip}", 0)
            extra = f" (+{n})" if n else ""
            push("VPS SSH 登录", f"root 从 {ip} 登录成功{extra}",
                 priority=3, tags=["door"])

def check_units():
    p = subprocess.run(
        ["systemctl", "list-units", "--state=failed", "--no-legend",
         "--no-pager", "--plain"],
        capture_output=True, text=True, timeout=30)
    failed = [l.split()[0] for l in p.stdout.splitlines() if l.strip()]
    seen = _state.get("unit_seen")
    if not isinstance(seen, dict):
        seen = _state["unit_seen"] = {}
    for unit in failed:
        base = unit.split(".")[0]
        if base in UNITS_TO_WATCH or base.startswith("fuckpush"):
            seen.setdefault(unit, time.time())   # P2-1：首次进入 failed 的时刻
            if allowed(f"unit-failed:{unit}", cooldown=1800):
                n = _state["counts"].get(f"unit-failed:{unit}", 0)
                extra = f" (+{n})" if n else ""
                dur_min = int((time.time() - seen.get(unit, time.time())) // 60)
                push("VPS 服务挂了",
                     f"{unit} 进入 failed 状态，已持续约 {dur_min} 分钟{extra}\n"
                     f"排查: systemctl status {unit}; "
                     f"journalctl -u {unit} -n 50 --no-pager",
                     priority=4, tags=["boom"])
        else:
            gray("unit-failed", {"unit": unit})
    # P2-1：已恢复的 unit 清出计时，下次再挂从零开始
    for u in [k for k in list(seen) if k not in failed]:
        seen.pop(u, None)

# ---------- main ----------
def main():
    load_state()
    last_check = time.time()
    last_hb = 0.0              # 0 -> first loop iteration sends heartbeat immediately
    try:
        push("FuckPush VPS 监控已启动", "监控进程上线，硬规则生效", priority=2,
             tags=["white_check_mark"])
    except Exception as e:
        # r7-11：启动 push 原来在 try 外 —— 磁盘满/STATE_FILE 只读一击退出，
        # 监控循环根本进不去。
        print(f"startup push failed: {e!r}", flush=True)
    while True:
        loop_start = time.time()
        try:
            check_oom(last_check)
            check_disk()
            check_ssh_login(last_check)
            check_units()
        except Exception as e:
            gray("monitor-error", {"error": repr(e)})   # gray 自身不再抛 (r7-11)
        last_check = loop_start
        # Reverse heartbeat (B plan): PC watchdog no longer opens new SSH
        # connections to probe us (TUN/proxy swallows new handshakes ->
        # false alarms); it just pulls fp-vps-hb events over the tunnel and
        # judges their age. Startup counts as t=0 so the first heartbeat
        # goes out on the first loop.
        if loop_start - last_hb >= HB_INTERVAL:
            _publish(TOPIC_HB, "hb",
                     json.dumps({"ts": int(loop_start),
                                 "load": round(os.getloadavg()[0], 2)},
                                ensure_ascii=False),
                     priority=1, tags=["heartbeat"])
            last_hb = loop_start
        try:
            save_state()
        except Exception as e:
            # r7-11：save_state(mkdir+write) 原来在 try 外，磁盘满一击退出；
            # 落盘失败只是丢一次冷却状态（下次多推一条），不该带走监控。
            print(f"save_state failed: {e!r}", flush=True)
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
