"""Telethon helpers: per-browser client pool, protected-content detection, serialization, link parsing."""
from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

from fastapi import HTTPException
from telethon import TelegramClient, utils
from telethon.errors import RPCError
from telethon.tl.types import Channel, User

from . import config

SID_RE = re.compile(r"^[a-f0-9]{32}$")

PROTECTED_MSG = (
    "The owner of this chat has turned off saving and forwarding for its content. "
    "You can view the preview, but this app does not download or forward it."
)


# ------------------------------------------------------------------ client pool
class ClientPool:
    """One Telethon client per signed-in browser session. Sessions live only on the server."""

    def __init__(self) -> None:
        self.clients: dict[str, TelegramClient] = {}
        self.last_used: dict[str, float] = {}
        self.pending: dict[str, tuple[str, str]] = {}  # sid -> (phone, phone_code_hash)
        self.is_busy: Callable[[str], bool] = lambda sid: False
        self._lock = asyncio.Lock()

    @staticmethod
    def _base(sid: str) -> str:
        return str(config.SESSION_DIR / sid)

    async def get(self, sid: Optional[str], create: bool = True) -> Optional[TelegramClient]:
        if not sid or not SID_RE.match(sid):
            return None
        async with self._lock:
            client = self.clients.get(sid)
            if client is None:
                if not create and not os.path.exists(self._base(sid) + ".session"):
                    return None
                client = TelegramClient(self._base(sid), config.API_ID, config.API_HASH)
                await client.connect()
                self.clients[sid] = client
                try:
                    os.chmod(self._base(sid) + ".session", 0o600)
                except OSError:
                    pass
            elif not client.is_connected():
                await client.connect()
            self.last_used[sid] = time.time()
            return client

    def touch(self, sid: str) -> None:
        self.last_used[sid] = time.time()

    async def drop(self, sid: Optional[str], logout: bool = False) -> None:
        if not sid or not SID_RE.match(sid):
            return
        client = self.clients.pop(sid, None)
        self.last_used.pop(sid, None)
        self.pending.pop(sid, None)
        if client is not None:
            try:
                if logout:
                    await client.log_out()  # revokes the session on Telegram's side
                else:
                    await client.disconnect()
            except Exception:  # noqa: BLE001
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
        if logout:
            for suffix in (".session", ".session-journal"):
                try:
                    os.remove(self._base(sid) + suffix)
                except OSError:
                    pass

    async def reap_idle(self) -> None:
        limit = config.IDLE_DISCONNECT_MINUTES * 60
        now = time.time()
        for sid, last in list(self.last_used.items()):
            if now - last > limit and not self.is_busy(sid):
                client = self.clients.pop(sid, None)
                self.last_used.pop(sid, None)
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass

    async def close_all(self) -> None:
        for client in list(self.clients.values()):
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.clients.clear()


pool = ClientPool()


# ------------------------------------------------------------------ entities
async def entity_for(client: TelegramClient, chat_id: int):
    """Resolve a chat the account can access. Never joins anything."""
    try:
        return await client.get_entity(chat_id)
    except ValueError:
        pass
    except RPCError:
        raise HTTPException(403, "This account can't access that chat.")
    await client.get_dialogs(limit=300)  # refresh the entity cache once
    try:
        return await client.get_entity(chat_id)
    except Exception:  # noqa: BLE001
        raise HTTPException(404, "Chat not found, or this account is not a member of it.")


def chat_type(e) -> str:
    if isinstance(e, User):
        return "bot" if e.bot else "user"
    if isinstance(e, Channel):
        return "channel" if e.broadcast else "group"
    return "group"


def name_of(e) -> str:
    if e is None:
        return ""
    if isinstance(e, User):
        return " ".join(p for p in (e.first_name, e.last_name) if p) or e.username or "Deleted account"
    return getattr(e, "title", "") or ""


def chat_summary(e) -> dict:
    return {
        "id": utils.get_peer_id(e),
        "name": name_of(e) or "Unknown",
        "type": chat_type(e),
        "protected": is_protected(e),
    }


# ------------------------------------------------------------------ protected content
def is_protected(entity, message=None) -> bool:
    """True when the chat owner enabled 'Restrict saving content' (chat-level or message-level flag)."""
    if getattr(entity, "noforwards", False):
        return True
    return bool(message is not None and getattr(message, "noforwards", False))


def restriction_notes(entity) -> list[str]:
    notes = []
    for r in getattr(entity, "restriction_reason", None) or []:
        notes.append(f"{getattr(r, 'platform', '?')}: {getattr(r, 'reason', '?')} {getattr(r, 'text', '')}".strip())
    return notes


# ------------------------------------------------------------------ messages
def media_kind(m) -> Optional[str]:
    if m is None or not getattr(m, "media", None):
        return None
    if m.photo:
        return "photo"
    if m.sticker:
        return "sticker"
    if m.gif:
        return "gif"
    if m.video or m.video_note:
        return "video"
    if m.voice:
        return "voice"
    if m.audio:
        return "audio"
    if m.document:
        return "document"
    return None


def media_name(m) -> str:
    f = m.file
    kind = media_kind(m) or "file"
    ext = f.ext if f and f.ext else ""
    return (f.name if f and f.name else f"{kind}_{m.id}{ext}")


_LABELS = {"photo": "Photo", "video": "Video", "gif": "GIF", "voice": "Voice message",
           "audio": "Audio", "sticker": "Sticker", "document": "File"}


def preview(m) -> str:
    if m is None:
        return ""
    if m.message:
        return m.message[:80]
    return _LABELS.get(media_kind(m) or "", "Service message" if m.action else "")


def serialize(m, chat_protected: bool) -> dict:
    kind = media_kind(m)
    media = None
    if kind:
        f = m.file
        media = {
            "kind": kind,
            "name": media_name(m),
            "size": f.size if f else None,
            "mime": f.mime_type if f else None,
            "duration": getattr(f, "duration", None) if f else None,
        }
    return {
        "id": m.id,
        "text": m.message or "",
        "date": m.date.isoformat() if m.date else None,
        "out": bool(m.out),
        "sender": name_of(m.sender) if not m.out else "",
        "service": type(m.action).__name__.replace("MessageAction", "") if m.action else None,
        "media": media,
        "protected": chat_protected or bool(getattr(m, "noforwards", False)),
    }


# ------------------------------------------------------------------ links
_BASE = r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/"


def parse_link(text: str) -> Optional[dict]:
    t = text.strip()
    m = re.match(_BASE + r"(?:joinchat/|\+)([\w-]{8,})/?$", t, re.I)
    if m:
        return {"kind": "invite", "hash": m.group(1)}
    m = re.match(_BASE + r"c/(\d+)/(\d+)(?:/(\d+))?/?(?:\?.*)?$", t, re.I)
    if m:
        return {"kind": "private", "channel": int(m.group(1)), "msg": int(m.group(3) or m.group(2))}
    m = re.match(_BASE + r"([A-Za-z]\w{3,31})(?:/(\d+))?(?:/(\d+))?/?(?:\?.*)?$", t, re.I)
    if m:
        msg = m.group(3) or m.group(2)
        return {"kind": "public", "username": m.group(1), "msg": int(msg) if msg else None}
    return None
