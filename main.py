"""Portable hosting entry point: python main.py."""

import json
import os
import secrets
import sys
from pathlib import Path


def prepare_host():
    from cryptography.fernet import Fernet
    from dotenv import load_dotenv

    load_dotenv(encoding="utf-8-sig")
    directory = os.environ.get("DATA_DIR")
    if not directory and os.name != "nt" and Path("/data").is_dir():
        directory = "/data/cryptobot-state"
    if directory:
        target = Path(directory).resolve()
        target.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///" + (target / "assistant.db").as_posix())
        os.environ.setdefault("USER_SESSION_FILE", str(target / "session.enc"))
        keyfile = target / "keys.json"
        missing = [key for key in ("APP_KEY", "WEBHOOK_SECRET") if not os.environ.get(key)]
        if missing:
            if not keyfile.exists():
                if (target / "assistant.db").exists() or not os.environ["DATABASE_URL"].startswith("sqlite"):
                    raise SystemExit(
                        "Existing/external database requires its original APP_KEY in hosting secrets."
                    )
                values = {
                    "APP_KEY": os.environ.get("APP_KEY") or Fernet.generate_key().decode(),
                    "WEBHOOK_SECRET": os.environ.get("WEBHOOK_SECRET") or secrets.token_urlsafe(36),
                }
                fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump(values, file)
            values = json.loads(keyfile.read_text(encoding="utf-8"))
            for key in missing:
                os.environ[key] = values[key]
    os.environ.setdefault("BOT_TRANSPORT", "polling")
    os.environ.setdefault("MONITOR_SOURCE", "userbot")


if __name__ == "__main__":
    prepare_host()
    from sniper.__main__ import main

    sys.argv = [sys.argv[0], "serve"]
    main()
