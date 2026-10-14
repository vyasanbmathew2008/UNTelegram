"""FastAPI application. API credentials and Telegram sessions never leave this process/disk."""
from __future__ import annotations

import asyncio
import math
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.sessions import SessionMiddleware
from telethon import errors, utils
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import (ChatInviteAlready, InputMessagesFilterDocument, InputMessagesFilterMusic,
                               InputMessagesFilterPhotos, InputMessagesFilterVideo, InputMessagesFilterVoice,
                               PeerChannel)

from . import config
from .downloads import downloads, list_folders, safe_folder
from .tg import (PROTECTED_MSG, SID_RE, chat_summary, chat_type, entity_for, is_protected, media_kind,
                 media_name, name_of, parse_link, pool, preview, restriction_notes, serialize)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
FILTERS = {
    "photo": InputMessagesFilterPhotos,
    "video": InputMessagesFilterVideo,
    "audio": InputMessagesFilterMusic,
    "voice": InputMessagesFilterVoice,
    "document": InputMessagesFilterDocument,
}


# ------------------------------------------------------------------ lifecycle
@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.is_busy = downloads.busy

    async def reaper():
        while True:
            await asyncio.sleep(60)
            await pool.reap_idle()

    task = asyncio.create_task(reaper())
    yield
    task.cancel()
    await pool.close_all()


app = FastAPI(title="Telegram Web", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    session_cookie="tgweb_session",
    max_age=config.WEB_SESSION_HOURS * 3600,
    same_site="strict",
    https_only=config.COOKIE_SECURE,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# ------------------------------------------------------------------ security helpers
async def csrf(request: Request) -> None:
    """State-changing requests must carry a custom header, which cross-site forms cannot send."""
    if request.headers.get("x-tg-web") != "1":
        raise HTTPException(403, "Missing request header.")


class RateLimiter:
    def __init__(self) -> None:
        self.hits: dict[str, deque] = defaultdict(deque)

    def hit(self, key: str, limit: int, window: int) -> None:
        now, q = time.time(), self.hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, f"Too many attempts. Try again in {math.ceil(window - (now - q[0]))} seconds.")
        q.append(now)


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def require_configured() -> None:
    if not config.CONFIGURED:
        raise HTTPException(500, "Server is missing API_ID / API_HASH in the .env file.")


async def authed(request: Request):
    sid = request.session.get("sid")
    client = await pool.get(sid, create=False) if config.CONFIGURED else None
    if client is None or not await client.is_user_authorized():
        raise HTTPException(401, "Not signed in.")
    pool.touch(sid)
    return client


# ------------------------------------------------------------------ models
class PhoneIn(BaseModel):
    phone: str = Field(max_length=32)

    @field_validator("phone")
    @classmethod
    def clean(cls, v: str) -> str:
        v = re.sub(r"[\s\-().]", "", v)
        if not re.fullmatch(r"\+?\d{7,15}", v):
            raise ValueError("Enter the phone number with country code, for example +919876543210.")
        return v if v.startswith("+") else "+" + v


class CodeIn(BaseModel):
    code: str = Field(pattern=r"^\d{4,8}$")


class PasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class FolderIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class LinkIn(BaseModel):
    url: str = Field(min_length=5, max_length=300)


class JobIn(BaseModel):
    chat_id: int
    message_ids: list[int] = Field(default_factory=list, max_length=config.MAX_BATCH)
    all_media: bool = False
    kinds: list[Literal["photo", "video", "audio", "voice", "document"]] = Field(default_factory=list)
    limit: int = Field(100, ge=1, le=config.MAX_BATCH)
    folder: str = Field("", max_length=200)


# ------------------------------------------------------------------ pages
@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/config")
async def get_config():
    # Only non-secret facts. API_ID / API_HASH are never returned.
    return {"configured": config.CONFIGURED, "download_root": config.DOWNLOAD_ROOT.name,
            "max_batch": config.MAX_BATCH}


# ------------------------------------------------------------------ auth
@app.post("/api/auth/send_code", dependencies=[Depends(csrf)])
async def send_code(body: PhoneIn, request: Request):
    require_configured()
    limiter.hit(f"send:{client_ip(request)}", 5, 600)
    sid = request.session.get("sid")
    if not sid or not SID_RE.match(sid):
        sid = secrets.token_hex(16)
        request.session["sid"] = sid
    client = await pool.get(sid)
    try:
        sent = await client.send_code_request(body.phone)
    except errors.PhoneNumberInvalidError:
        raise HTTPException(400, "Telegram says that phone number is not valid.")
    except errors.PhoneNumberBannedError:
        raise HTTPException(400, "That phone number is banned from Telegram.")
    except errors.ApiIdInvalidError:
        raise HTTPException(500, "Telegram rejected API_ID / API_HASH. Check the .env file.")
    except errors.FloodWaitError as e:
        raise HTTPException(429, f"Telegram asked you to wait {e.seconds} seconds before requesting another code.")
    pool.pending[sid] = (body.phone, sent.phone_code_hash)
    return {"ok": True}


@app.post("/api/auth/verify_code", dependencies=[Depends(csrf)])
async def verify_code(body: CodeIn, request: Request):
    require_configured()
    limiter.hit(f"verify:{client_ip(request)}", 10, 600)
    sid = request.session.get("sid")
    client = await pool.get(sid, create=False)
    phone, code_hash = pool.pending.get(sid, (None, None))
    if client is None or not phone:
        raise HTTPException(400, "Request a new code first.")
    try:
        await client.sign_in(phone, body.code, phone_code_hash=code_hash)
    except errors.SessionPasswordNeededError:
        return {"need_password": True}
    except errors.PhoneCodeInvalidError:
        raise HTTPException(400, "That code is wrong. Check it and try again.")
    except errors.PhoneCodeExpiredError:
        raise HTTPException(400, "That code has expired. Go back and request a new one.")
    except errors.FloodWaitError as e:
        raise HTTPException(429, f"Too many attempts. Try again in {e.seconds} seconds.")
    pool.pending.pop(sid, None)
    return {"ok": True}


@app.post("/api/auth/verify_password", dependencies=[Depends(csrf)])
async def verify_password(body: PasswordIn, request: Request):
    require_configured()
    limiter.hit(f"pass:{client_ip(request)}", 6, 600)
    client = await pool.get(request.session.get("sid"), create=False)
    if client is None:
        raise HTTPException(400, "Start again from the phone number.")
    try:
        await client.sign_in(password=body.password)
    except errors.PasswordHashInvalidError:
        raise HTTPException(400, "Wrong two-step verification password.")
    except errors.FloodWaitError as e:
        raise HTTPException(429, f"Too many attempts. Try again in {e.seconds} seconds.")
    pool.pending.pop(request.session.get("sid"), None)
    return {"ok": True}


@app.get("/api/auth/me")
async def me(client=Depends(authed)):
    u = await client.get_me()
    return {"id": u.id, "name": name_of(u), "username": u.username}


@app.post("/api/auth/logout", dependencies=[Depends(csrf)])
async def logout(request: Request):
    sid = request.session.get("sid")
    downloads.drop(sid or "")
    await pool.drop(sid, logout=True)
    request.session.clear()
    return {"ok": True}


# ------------------------------------------------------------------ chats & messages
@app.get("/api/dialogs")
async def dialogs(limit: int = Query(150, ge=1, le=300), client=Depends(authed)):
    out = []
    async for d in client.iter_dialogs(limit=limit):
        e = d.entity
        out.append({
            "id": d.id,
            "name": d.name or name_of(e) or "Deleted account",
            "type": chat_type(e),
            "unread": d.unread_count,
            "pinned": d.pinned,
            "protected": is_protected(e),
            "last": preview(d.message),
            "date": d.date.isoformat() if d.date else None,
        })
    return out


@app.get("/api/messages/{chat_id}")
async def messages(chat_id: int, before: int = Query(0, ge=0), around: int = Query(0, ge=0),
                   q: str = Query("", max_length=100), client=Depends(authed)):
    entity = await entity_for(client, chat_id)
    kwargs = {"limit": 40}
    if q.strip():
        kwargs["search"] = q.strip()
    if around:
        kwargs["offset_id"] = around + 21  # window that includes the target message
    elif before:
        kwargs["offset_id"] = before
    msgs = await client.get_messages(entity, **kwargs)
    protected = is_protected(entity)
    return {
        "protected": protected,
        "restrictions": restriction_notes(entity),
        "messages": [serialize(m, protected) for m in reversed(msgs)],
        "end": len(msgs) < 40,
    }


@app.get("/api/avatar/{chat_id}")
async def avatar(chat_id: int, client=Depends(authed)):
    try:
        entity = await entity_for(client, chat_id)
        data = await client.download_profile_photo(entity, file=bytes)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        data = None
    if not data:
        raise HTTPException(404, "No photo.")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/thumb/{chat_id}/{msg_id}")
async def thumb(chat_id: int, msg_id: int, client=Depends(authed)):
    """Low-resolution preview, the same kind Telegram shows before a file is opened."""
    entity = await entity_for(client, chat_id)
    m = await client.get_messages(entity, ids=msg_id)
    if not m or not media_kind(m):
        raise HTTPException(404, "No media.")
    try:
        data = await m.download_media(file=bytes, thumb=-1)
    except Exception:  # noqa: BLE001
        data = None
    if not data:
        raise HTTPException(404, "No preview available.")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/inspect/{chat_id}/{msg_id}")
async def inspect(chat_id: int, msg_id: int, client=Depends(authed)):
    """Explains whether a message can be downloaded, and why not if it can't."""
    entity = await entity_for(client, chat_id)
    m = await client.get_messages(entity, ids=msg_id)
    if not m:
        raise HTTPException(404, "Message not found.")
    kind = media_kind(m)
    chat_flag = bool(getattr(entity, "noforwards", False))
    msg_flag = bool(getattr(m, "noforwards", False))
    if not kind:
        reason = "This message has no downloadable media."
    elif chat_flag or msg_flag:
        reason = PROTECTED_MSG
    else:
        reason = "Allowed. Saving is not restricted for this message."
    return {
        "chat": {"id": chat_id, "name": name_of(entity), "type": chat_type(entity),
                 "saving_restricted": chat_flag, "restrictions": restriction_notes(entity)},
        "message": {"id": m.id, "media_kind": kind, "name": media_name(m) if kind else None,
                    "size": m.file.size if kind and m.file else None,
                    "mime": m.file.mime_type if kind and m.file else None,
                    "saving_restricted": msg_flag, "date": m.date.isoformat() if m.date else None},
        "can_download": bool(kind) and not (chat_flag or msg_flag),
        "reason": reason,
    }


# ------------------------------------------------------------------ media streaming (with Range)
@app.get("/api/media/{chat_id}/{msg_id}")
async def media(chat_id: int, msg_id: int, request: Request, download: int = 0, client=Depends(authed)):
    entity = await entity_for(client, chat_id)
    m = await client.get_messages(entity, ids=msg_id)
    if not m or not media_kind(m):
        raise HTTPException(404, "No media in this message.")
    if is_protected(entity, m):
        raise HTTPException(403, PROTECTED_MSG)

    f = m.file
    name = media_name(m)
    mime = (f.mime_type if f and f.mime_type else "application/octet-stream")
    size = f.size if (m.document and f and f.size) else None  # photos: size is not exact, so no ranges
    target = m.photo or m.document

    start, end, status = 0, (size - 1 if size else None), 200
    rng = request.headers.get("range")
    if rng and size:
        mt = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
        if not mt or (mt.group(1) == "" and mt.group(2) == ""):
            raise HTTPException(416, "Bad range.")
        if mt.group(1) == "":
            start = max(0, size - int(mt.group(2)))
        else:
            start = int(mt.group(1))
            if mt.group(2):
                end = min(int(mt.group(2)), size - 1)
        if start >= size or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206

    chunk = 512 * 1024
    aligned = start - (start % chunk)

    async def body():
        pos = aligned
        async for part in client.iter_download(target, offset=aligned, request_size=chunk, file_size=size):
            lo = max(start - pos, 0)
            hi = len(part) if end is None else min(end - pos + 1, len(part))
            if lo < hi:
                yield bytes(part[lo:hi])
            pos += len(part)
            if end is not None and pos > end:
                break

    disposition = "attachment" if download else "inline"
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name)}",
        "Accept-Ranges": "bytes" if size else "none",
        "Cache-Control": "private, max-age=3600",
    }
    if size:
        headers["Content-Length"] = str(end - start + 1)
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(body(), status_code=status, media_type=mime, headers=headers)


# ------------------------------------------------------------------ message links & private channels
@app.post("/api/resolve_link", dependencies=[Depends(csrf)])
async def resolve_link(body: LinkIn, client=Depends(authed)):
    """Opens t.me links only if this account can already access the chat. It never joins anything."""
    info = parse_link(body.url)
    if not info:
        raise HTTPException(400, "That doesn't look like a t.me link.")

    if info["kind"] == "invite":
        try:
            res = await client(CheckChatInviteRequest(info["hash"]))
        except errors.RPCError:
            raise HTTPException(400, "That invite link is invalid or has expired.")
        if isinstance(res, ChatInviteAlready):
            return {"status": "ok", "chat": chat_summary(res.chat), "message_id": None}
        return {"status": "not_member", "title": getattr(res, "title", "this chat"),
                "detail": "This account hasn't joined this chat. Join it in Telegram first, then paste a message link."}

    try:
        if info["kind"] == "private":
            try:
                entity = await client.get_entity(PeerChannel(info["channel"]))
            except ValueError:
                await client.get_dialogs(limit=300)
                entity = await client.get_entity(PeerChannel(info["channel"]))
        else:
            entity = await client.get_entity(info["username"])
    except (ValueError, errors.RPCError):
        raise HTTPException(403, "This account can't access that chat. Private chats need membership.")

    msg_id = info.get("msg")
    if msg_id:
        found = await client.get_messages(entity, ids=msg_id)
        if not found:
            raise HTTPException(404, "That message doesn't exist or has been deleted.")
    return {"status": "ok", "chat": chat_summary(entity), "message_id": msg_id}


# ------------------------------------------------------------------ folders & download jobs
@app.get("/api/folders")
async def folders(client=Depends(authed)):
    return {"root": str(config.DOWNLOAD_ROOT), "folders": list_folders()}


@app.post("/api/folders", dependencies=[Depends(csrf)])
async def make_folder(body: FolderIn, client=Depends(authed)):
    path = safe_folder(body.name)
    return {"folder": path.relative_to(config.DOWNLOAD_ROOT).as_posix()}


@app.post("/api/jobs", dependencies=[Depends(csrf)])
async def create_job(body: JobIn, request: Request, client=Depends(authed)):
    sid = request.session["sid"]
    entity = await entity_for(client, body.chat_id)
    folder = safe_folder(body.folder)
    chosen: dict[int, object] = {}
    skipped: list[dict] = []

    if body.all_media:
        if is_protected(entity):
            raise HTTPException(403, PROTECTED_MSG)
        for kind in (body.kinds or list(FILTERS)):
            remaining = body.limit - len(chosen)
            if remaining <= 0:
                break
            async for m in client.iter_messages(entity, filter=FILTERS[kind](), limit=remaining):
                if media_kind(m) and not is_protected(entity, m):
                    chosen[m.id] = m
    else:
        ids = list(dict.fromkeys(body.message_ids))
        if not ids:
            raise HTTPException(400, "Select at least one message.")
        fetched = await client.get_messages(entity, ids=ids)
        for mid, m in zip(ids, fetched):
            if not m or not media_kind(m):
                skipped.append({"id": mid, "reason": "No downloadable media."})
            elif is_protected(entity, m):
                skipped.append({"id": mid, "reason": "Saving is restricted by the chat owner."})
            else:
                chosen[m.id] = m

    if not chosen:
        raise HTTPException(400, "Nothing can be downloaded from that selection.")
    job = downloads.create(sid, client, body.chat_id, folder, list(chosen.values()))
    return {"job": job.to_dict(), "skipped": skipped}


@app.get("/api/jobs")
async def list_jobs(request: Request, client=Depends(authed)):
    return downloads.list(request.session["sid"])


@app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(csrf)])
async def cancel_job(job_id: str, request: Request, client=Depends(authed)):
    if not downloads.cancel(request.session["sid"], job_id):
        raise HTTPException(404, "Job not found.")
    return {"ok": True}


@app.post("/api/jobs/clear", dependencies=[Depends(csrf)])
async def clear_jobs(request: Request, client=Depends(authed)):
    downloads.clear_finished(request.session["sid"])
    return {"ok": True}
