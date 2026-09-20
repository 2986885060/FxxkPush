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
TOPIC_HARD = "fp-vps"        # hard-rule alerts -> PC (PC relays to phone after AI)
TOPIC_GRAY = "fp-gray"       # gray-zone events -> PC for AI triage
GRAY_LOG = Path("/var/log/fuckpush/gray.log")
STATE_FILE = Path("/var/lib/fuckpush/state.json")
COOLDOWN_DEFAULT = 1800      # 30 min
CHECK_INTERVAL = 60          # main loop, seconds
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

def save_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(_state))

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
    p = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "-H", f"Authorization: Bearer {NTFY_TOKEN}",
         "-H", "Content-Type: application/json",
         "-d", "@-", NTFY_URL],
        input=payload, capture_output=True)
    code = p.stdout.decode().strip()
    if code != "200":
        # never die on push failure; local spool as fallback
        GRAY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with GRAY_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "kind": "ntfy-push-failed",
                                "data": {"http_code": code, "title": title}},
                               ensure_ascii=False) + "\n")
    return code == "200"

def push(title, message, priority=4, tags=None):
    return _publish(TOPIC_HARD, title, message, priority, tags)

def gray(kind, data):
    """Gray-zone event: local spool + publish to fp-gray for the PC."""
    entry = {"ts": time.time(), "kind": kind, "data": data}
    line = json.dumps(entry, ensure_ascii=False)
    GRAY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with GRAY_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    _publish(TOPIC_GRAY, f"灰区事件: {kind}", line, priority=1,
             tags=["eyes"])

# ---------- hard rules ----------
def check_oom(since_ts):
    # kernel OOM lines
    p = subprocess.run(
        ["journalctl", "-k", "--since", f"@{int(since_ts)}", "--no-pager"],
        capture_output=True, text=True)
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
    p = subprocess.run(["df", "-P", "/"], capture_output=True, text=True)
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
        capture_output=True, text=True)
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
        if ip in ("185.170.211.73",) or ip.startswith("127."):
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
        capture_output=True, text=True)
    failed = [l.split()[0] for l in p.stdout.splitlines() if l.strip()]
    for unit in failed:
        base = unit.split(".")[0]
        if base in UNITS_TO_WATCH or base.startswith("fuckpush"):
            if allowed(f"unit-failed:{unit}", cooldown=1800):
                n = _state["counts"].get(f"unit-failed:{unit}", 0)
                extra = f" (+{n})" if n else ""
                push("VPS 服务挂了", f"{unit} 进入 failed 状态{extra}",
                     priority=4, tags=["boom"])
        else:
            gray("unit-failed", {"unit": unit})

# ---------- main ----------
def main():
    load_state()
    last_check = time.time()
    push("FuckPush VPS 监控已启动", "监控进程上线，硬规则生效", priority=2,
         tags=["white_check_mark"])
    while True:
        loop_start = time.time()
        try:
            check_oom(last_check)
            check_disk()
            check_ssh_login(last_check)
            check_units()
        except Exception as e:
            gray("monitor-error", {"error": repr(e)})
        last_check = loop_start
        save_state()
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
