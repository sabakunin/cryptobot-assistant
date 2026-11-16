import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("host_entry", Path(__file__).resolve().parents[1] / "main.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


def prepare_env(monkeypatch, tmp_path):
    import os

    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dotenv.load_dotenv", lambda **kwargs: None)
    for key in (
        "APP_KEY",
        "WEBHOOK_SECRET",
        "DATABASE_URL",
        "USER_SESSION_FILE",
        "BOT_TRANSPORT",
        "MONITOR_SOURCE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))


def test_host_keys_persist_across_restarts(monkeypatch, tmp_path):
    import os

    prepare_env(monkeypatch, tmp_path)
    entry.prepare_host()
    original = {key: os.environ[key] for key in ("APP_KEY", "WEBHOOK_SECRET")}
    assert json.loads((tmp_path / "data" / "keys.json").read_text()) == original
    for key in original:
        monkeypatch.delenv(key)
    entry.prepare_host()
    assert {key: os.environ[key] for key in original} == original
    assert os.environ["DATABASE_URL"].endswith("/data/assistant.db")


def test_existing_database_without_key_does_not_generate_replacement(monkeypatch, tmp_path):
    prepare_env(monkeypatch, tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "assistant.db").write_bytes(b"existing")
    with pytest.raises(SystemExit):
        entry.prepare_host()
    assert not (tmp_path / "data" / "keys.json").exists()


def test_external_database_requires_persistent_secret_configuration(monkeypatch, tmp_path):
    prepare_env(monkeypatch, tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")
    with pytest.raises(SystemExit):
        entry.prepare_host()


def test_partial_host_secrets_preserve_existing_key(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    prepare_env(monkeypatch, tmp_path)
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("APP_KEY", key)
    entry.prepare_host()
    assert json.loads((tmp_path / "data" / "keys.json").read_text())["APP_KEY"] == key
