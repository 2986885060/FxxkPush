#!/usr/bin/env python3
"""FuckPush VPS executor: run commands on the VPS over SSH (password auth).

Usage:
    python vps_exec.py "command"              # run one command, stream output
    python vps_exec.py --put local remote     # upload a file
    python vps_exec.py --get remote local     # download a file

Credentials come from vps.secret next to this file (never committed to git).
"""
import sys
import os
import stat
import time
from pathlib import Path

import paramiko

HERE = Path(__file__).resolve().parent
SECRET = HERE / "vps.secret"


def load_secret():
    parts = SECRET.read_text().strip().split()
    if len(parts) != 4:
        raise SystemExit("vps.secret must be: host port user password")
    return parts


def connect():
    host, port, user, password = load_secret()
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(host, port=int(port), username=user, password=password,
                timeout=15, banner_timeout=15, auth_timeout=15,
                look_for_keys=False, allow_agent=False)
    return cli


def run(cli, cmd):
    stdin, stdout, stderr = cli.exec_command(cmd, timeout=120)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    if out:
        print(out, end="")
    if err:
        print(err, end="", file=sys.stderr)
    return rc


def put(cli, local, remote, retries=3):
    """Upload via a temp file + atomic rename.

    A dropped connection mid-transfer truncates whatever the SFTP client
    opened, so never write straight to the destination: a flaky link would
    leave an empty (or half-written) file behind and the service would die
    on next restart. Upload to <remote>.tmp, verify the size, then move.
    """
    want = os.path.getsize(local)
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            sftp = cli.open_sftp()
            tmp = remote + ".tmp"
            sftp.put(local, tmp)
            got = sftp.stat(tmp).st_size
            if got != want:
                raise IOError(f"short upload: {got} of {want} bytes")
            try:
                sftp.remove(remote)
            except IOError:
                pass
            sftp.rename(tmp, remote)
            final = sftp.stat(remote).st_size
            sftp.close()
            if final != want:
                raise IOError(f"rename mismatch: {final} of {want} bytes")
            print(f"uploaded {local} -> {remote} ({final} bytes)")
            return
        except Exception as e:
            last_err = e
            print(f"upload attempt {attempt}/{retries} failed: {e!r}", file=sys.stderr)
            time.sleep(3)
    raise SystemExit(f"upload failed after {retries} attempts: {last_err!r}")


def get(cli, remote, local):
    sftp = cli.open_sftp()
    sftp.get(remote, local)
    print(f"downloaded {remote} -> {local}")
    sftp.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cli = connect()
    try:
        if sys.argv[1] == "--put":
            put(cli, sys.argv[2], sys.argv[3])
            return 0
        if sys.argv[1] == "--get":
            get(cli, sys.argv[2], sys.argv[3])
            return 0
        return run(cli, sys.argv[1])
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
