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


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


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
    try:
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                if not transport.is_active():
                    raise ConnectionError("transport closed")
                continue
            on_conn(conn, transport)
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
