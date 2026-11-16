"""Optional HTTPS for a PC-hosted Mini App; Bot API continues using polling."""

import contextlib
import os
import re
import shutil
import subprocess
import time

from .config import Config

TUNNEL_URL = re.compile(
    r"https://(?!admin\.|www\.)[a-z0-9-]+(?:\.app)?\.(?:lhr\.life|localhost\.run|lhr\.rocks)\b"
)


def publish_url(path, url):
    if url and not TUNNEL_URL.fullmatch(url):
        raise ValueError("Unexpected tunnel hostname")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(url, encoding="utf-8")
    temporary.replace(path)


def main():
    config = Config()
    if not config.public_base_url_file:
        raise SystemExit("Set PUBLIC_BASE_URL_FILE=data/public-url.txt before starting the local tunnel.")
    ssh = shutil.which("ssh")
    if not ssh:
        raise SystemExit("OpenSSH client is required for local HTTPS.")
    target = config.public_base_url_file.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_suffix(".lock").open("a+b")
    lock.write(b"0")
    lock.flush()
    lock.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("The local tunnel is already running.") from None
    command = [
        ssh,
        "-T",
        "-F",
        os.devnull,
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentityFile=none",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={target.parent / 'tunnel_known_hosts'}",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ExitOnForwardFailure=yes",
        "-R",
        f"80:127.0.0.1:{config.port}",
        "nokey@localhost.run",
    ]
    child = None
    try:
        while True:
            publish_url(target, "")
            child = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            for line in child.stdout:
                found = TUNNEL_URL.search(line)
                if found:
                    publish_url(target, found.group())
                    print("Local HTTPS connected; bot uses the current address.", flush=True)
            child.wait()
            publish_url(target, "")
            print("Local HTTPS disconnected; reconnecting in five seconds.", flush=True)
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        if child and child.poll() is None:
            child.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=10)
        publish_url(target, "")
        lock.close()


if __name__ == "__main__":
    main()
