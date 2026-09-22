#!/usr/bin/env python3
"""FxxkPush ntfy SSH tunnel (PC side).

Listens on PC 127.0.0.1:2586 and pipes each connection through SSH to
VPS 127.0.0.1:2586 (a LOCAL forward: the bind happens on the PC, the VPS just
relays), so the push channel survives networks that block the ntfy port.

All PC-side services should talk to http://127.0.0.1:2586.

Credentials: vps.secret ("host port user password"), same file as vps_exec.py.
Run detached; auto-reconnects on drop.
"""
import select
import socket
import sys
import threading
import time
from pathlib import Path

import paramiko

HERE = Path(__file__).parent
LOCAL_HOST, LOCAL_PORT = "127.0.0.1", 2586
REMOTE_HOST, REMOTE_PORT = "127.0.0.1", 2586


import pclog
LOG = pclog.get_logger("ntfy_tunnel")


def log(msg):
    # tunnel used to print to a console that pythonw does not have — every
    # line it ever produced was silently lost. pclog writes to pc/logs too.
    pclog.log_auto(LOG, msg)


def read_secret():
    host, port, user, password = (HERE.parent / "vps.secret").read_text().split()
    return host, int(port), user, password


def pump(chan, sock):
    """Shovel bytes both ways until either side closes."""
    try:
        while True:
            r, _, _ = select.select([sock, chan], [], [], 60)
            if sock in r:
                data = sock.recv(16384)
                if not data:
                    break
                chan.sendall(data)
            if chan in r:
                data = chan.recv(16384)
                if not data:
                    break
                sock.sendall(data)
    except Exception:
        pass
    finally:
        try:
            chan.close()
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass


def on_conn(client_sock, transport: paramiko.Transport):
    try:
        chan = transport.open_channel(
            "direct-tcpip", (REMOTE_HOST, REMOTE_PORT), client_sock.getpeername())
    except Exception as e:
        log(f"channel open failed: {e!r}")
        client_sock.close()
        return
    threading.Thread(target=pump, args=(chan, client_sock), daemon=True).start()


def serve(client: paramiko.SSHClient):
    transport = client.get_transport()
    transport.set_keepalive(30)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LOCAL_HOST, LOCAL_PORT))
    srv.listen(16)
    srv.settimeout(1.0)
    log(f"tunnel up: {LOCAL_HOST}:{LOCAL_PORT} -> vps:{REMOTE_PORT} (over ssh)")
    # r8 P1-1：心跳按**墙钟**驱动，不按 accept 计数。原实现 beats 每次
    # 成功 accept 就清零，而 watchdog.check_health 每 60s 必然新建连接
    # （httpx keepalive_expiry=5s，连不上复用），健康状态下 beats 永远
    # 到不了 300 —— 隧道日志整天零心跳，quiet 检查（1800s）必然误报：
    # 2026-09-22 22:19:35 实际发出过一条假告警（报告 P1-1，每 30min 一对
    # 假故障/假恢复）。改成「只要 serve 循环还在转，每 300s 无条件留一条」。
    last_beat = time.time()
    try:
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                if not transport.is_active():
                    raise ConnectionError("transport closed")
            else:
                on_conn(conn, transport)
            now = time.time()
            if now - last_beat >= 300:
                last_beat = now
                log(f"idle heartbeat: tunnel serving, transport="
                    f"{'active' if transport.is_active() else 'DEAD'}")
    finally:
        srv.close()


def main():
    host, port, user, password = read_secret()
    while True:
        client = None
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(host, port=port, username=user, password=password,
                           timeout=15, banner_timeout=20, auth_timeout=20)
            log(f"ssh connected to {host}:{port}")
            serve(client)
        except Exception as e:
            log(f"tunnel error: {e!r}, retry in 15s")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        time.sleep(15)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
