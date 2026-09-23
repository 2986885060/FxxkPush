"""pc/alert_fallback.py — 三层告警降级的第 2 层（VPS SSH）与第 3 层（本机 toast），
watchdog 和 notification_listener 共用（P2-5）。

为什么抽出来而不是互相 import：
- listener import watchdog → 本进程执行 get_logger("watchdog")，从此持有
  watchdog.log 的 RotatingFileHandler；Windows 下轮转 rename 需要其它进程
  先关句柄，谁先轮转谁 PermissionError 被 handleError 静默吞掉（r7-7 同型）。
- 反向 import（watchdog 拿 listener 的东西）同理会持有
  notification_listener.log，完全对称的坑。
- 两份拷贝 = 改一处漏一处：P1-2 修过的「secret 解包必须在 try 内」击穿点
  要在两边各维护一遍。

约定：log 由调用方传入（各自的 logger），本模块**不建任何文件句柄** —— 这
是它能被两个进程同时 import 而不制造轮转竞争的全部原因。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path

import pclog

HERE = Path(__file__).parent       # pc/
ECHO_FILE = HERE / "toast_echo.jsonl"


def _cut(s: str, n: int) -> str:
    """r8 P2-7：截断必须留标记（与 watchdog._cut 同格式的共用副本）。"""
    return s if len(s) <= n else s[:n - 8] + "…(已截断)"


def _token() -> str:
    p = HERE / "ntfy.secret"
    return p.read_text().strip() if p.exists() else ""


def push_vps(payload: dict, log) -> bool:
    """SSH 到 VPS，在它本机发 ntfy —— 隧道死了这条路还活着。

    payload 走 base64：JSON 里有中文和引号，塞进 shell 命令会被转义啃掉，
    base64 全 ASCII 就没这问题；VPS 端用 curl -d @file 读，零转义。
    """
    # r8 P1-2：secret 解包 / import paramiko / SSHClient 构造原来在 try
    # **之外** —— vps.secret 被改坏（字段数≠4 抛 ValueError）、AV 瞬间拒读
    # （OSError）、paramiko 导入失败，且同时本地隧道不可用（正是要走第二层
    # 的时刻）时，异常沿 push_vps → send_alert → main 冒出，excepthook 再
    # 调 send_alert 撞同一异常被 except 吞掉，**第三层 toast 根本没机会
    # 执行** → watchdog os._exit，监控整体停摆 —— 复刻 17:21「最该报警时
    # 一条都没送出去」。全部收进 try。
    cli = None
    try:
        secret = HERE.parent / "vps.secret"
        if not secret.exists():
            log.error("vps.secret 不存在，无法走 SSH 兜底")
            return False
        host, port, user, pwd = secret.read_text().split()
        import paramiko

        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cli.connect(host, port=int(port), username=user, password=pwd,
                    timeout=15, banner_timeout=20, auth_timeout=20)
        b64 = base64.b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode()
        # curl 走 shell 命令行，两个约束：整条命令的单引号必须是偶数
        # （否则 shell 报未闭合、$() 拿到空串、push_vps 恒返回 False），
        # 以及 token 只在取得到时才带 header。token 出现在命令行只在自己的
        # VPS 上短暂可见（ps aux），可接受。
        tok = _token()
        auth = f"-H 'Authorization: Bearer ***' " if tok else ""
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
        log.error(f"vps alert rejected: code={out!r} stderr={err[:200]!r}")
        return False
    except Exception as e:
        log.error(f"vps alert channel failed: {e!r}")
        return False
    finally:
        if cli is not None:      # r8 P1-2：构造阶段就异常时 cli 还是 None
            try:
                cli.close()
            except Exception:
                pass


def _record_echo(title: str, message: str, log) -> None:
    """把弹出去的 toast 记进 toast_echo.jsonl，让 notification_listener 认出
    这是自己人（P2-9）。

    不记的话，自己弹的告警 toast 会被 listener 当成新通知重新入队分诊 ——
    实测 18:17/18:18 有两条 ``PUSH [?] FxxkPush watchdog 通道测试``；断网
    期间还会变成每 30 分钟一条必失败的 PUSH_FAIL，恢复后再补推一条过期的
    「已恢复」。

    r7-7：**不能** import pc_subscriber 来复用它的 record_echo —— 模块导入
    会执行 get_logger("pc_subscriber")，watchdog 进程从此长期持有
    pc_subscriber.log（347KB，最吵的那个）的 RotatingFileHandler；Windows 下
    rename 需要另一进程先关闭句柄，谁先轮转谁 PermissionError，被
    logging.handleError 静默吞掉 —— 该日志的 2MBx3 上限实际失效、无限增长。
    这里按同一格式（同一文件、同一字段）直接写，不再连带 import。
    """
    try:
        pclog.append_rotating(
            ECHO_FILE,
            json.dumps({"ts": time.time(), "title": (title or "").strip(),
                        "msg": (message or "").strip()}, ensure_ascii=False),
            max_bytes=256 * 1024, mode="tail")
    except Exception as e:
        log.warning(f"record echo failed (listener may re-triage this toast): {e!r}")


def push_toast(title: str, message: str, log) -> bool:
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
    env = {**os.environ, "FP_T": _cut(title, 120), "FP_M": _cut(message, 900)}
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
            _record_echo(title, message, log)   # 别让 listener 把这条告警再推一遍
            # r8 P1-3②：SHOWN 只证明 PowerShell 把通知**提交**给了通知中心，
            # 不证明用户看见 —— 勿扰/专注模式会静默吞掉它。日志措辞降级，
            # 免得事后拿「已送达」当「已看到」的证据。
            log.info("toast 已提交给通知中心（未证实显示；勿扰/专注模式会静默吞掉）")
            return True
        log.error(f"toast channel failed: rc={p.returncode} "
                  f"stderr={(p.stderr or '').strip()[:300]!r}")
        return False
    except Exception as e:
        log.error(f"toast channel failed: {e!r}")
        return False
