import argparse
import sys
from pathlib import Path

import paramiko
import socks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("socks_host")
    parser.add_argument("socks_port", type=int)
    parser.add_argument("remote_host")
    parser.add_argument("remote_port", type=int)
    parser.add_argument("username")
    parser.add_argument("key_file")
    parser.add_argument("--command", default="bash -s")
    parser.add_argument("--stdin-file")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    stdin_text = None
    if args.stdin_file:
        stdin_text = Path(args.stdin_file).read_text(encoding="utf-8")

    sock = socks.socksocket()
    sock.set_proxy(socks.SOCKS5, args.socks_host, args.socks_port, rdns=True)
    sock.settimeout(args.timeout)
    sock.connect((args.remote_host, args.remote_port))
    sock.settimeout(None)

    key = paramiko.Ed25519Key.from_private_key_file(args.key_file)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=args.remote_host,
        port=args.remote_port,
        username=args.username,
        pkey=key,
        sock=sock,
        timeout=args.timeout,
        banner_timeout=args.timeout,
        auth_timeout=args.timeout,
        look_for_keys=False,
        allow_agent=False,
    )

    stdin, stdout, stderr = client.exec_command(args.command, timeout=args.timeout)
    if stdin_text is not None:
        stdin.write(stdin_text)
        stdin.channel.shutdown_write()

    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    exit_code = stdout.channel.recv_exit_status()
    client.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
