"""Create hosting keys locally without printing their values."""

import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

target = Path("hosting-secrets.env")
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as file:
    file.write("APP_KEY=" + Fernet.generate_key().decode() + "\n")
    file.write("WEBHOOK_SECRET=" + secrets.token_urlsafe(36) + "\n")
print("Created hosting-secrets.env. Copy its two values into hosting secrets; keep the file private.")
