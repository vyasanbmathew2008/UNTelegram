"""Configuration loaded from the .env file next to run.py. Nothing here is sent to the browser."""
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


API_ID = _int("API_ID", 0)
API_HASH = (os.getenv("API_HASH") or "").strip()
HOST = os.getenv("HOST", "127.0.0.1")
PORT = _int("PORT", 8000)

SESSION_DIR = (BASE_DIR / os.getenv("SESSION_DIR", "sessions")).expanduser().resolve()
DOWNLOAD_ROOT = Path(os.getenv("DOWNLOAD_ROOT", "downloads")).expanduser()
if not DOWNLOAD_ROOT.is_absolute():
    DOWNLOAD_ROOT = BASE_DIR / DOWNLOAD_ROOT
DOWNLOAD_ROOT = DOWNLOAD_ROOT.resolve()

COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"
WEB_SESSION_HOURS = _int("WEB_SESSION_HOURS", 168)
IDLE_DISCONNECT_MINUTES = _int("IDLE_DISCONNECT_MINUTES", 30)
MAX_BATCH = max(1, _int("MAX_BATCH", 500))
MAX_CONCURRENT_DOWNLOADS = max(1, _int("MAX_CONCURRENT_DOWNLOADS", 2))

SESSION_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(SESSION_DIR, 0o700)
except OSError:
    pass


def _secret() -> str:
    env = (os.getenv("SECRET_KEY") or "").strip()
    if env:
        return env
    path = SESSION_DIR / ".secret_key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path.read_text().strip()


SECRET_KEY = _secret()
CONFIGURED = bool(API_ID and API_HASH)
