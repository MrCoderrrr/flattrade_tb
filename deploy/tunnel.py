"""Publish the loopback dashboard over TLS; trust only the exact current tunnel origin."""
import argparse
import os
from pathlib import Path
import re
import signal
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--origin-file', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    args.origin_file.parent.mkdir(parents=True, exist_ok=True)
    args.origin_file.write_text('')
    proc = subprocess.Popen(['/usr/local/bin/cloudflared', 'tunnel', '--url',
                             f'http://127.0.0.1:{args.port}', '--http-host-header',
                             f'localhost:{args.port}', '--no-autoupdate'],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    def stop(*_):
        proc.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for line in proc.stdout:
            match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
            if match:
                temporary = args.origin_file.with_suffix('.tmp')
                temporary.write_text(match.group())
                os.replace(temporary, args.origin_file)
                print('Dashboard URL: ' + match.group(), flush=True)
        return proc.wait()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        args.origin_file.write_text('')


if __name__ == '__main__':
    raise SystemExit(main())
