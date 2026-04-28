import argparse
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
    parser.add_argument("local_path")
    parser.add_argument("remote_path")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    local_path = Path(args.local_path).expanduser().resolve(strict=True)

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

    sftp = client.open_sftp()
    try:
        sftp.put(str(local_path), args.remote_path)
    finally:
        sftp.close()
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
