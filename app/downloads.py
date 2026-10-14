"""Server-side download jobs: queue, progress, cancellation and safe file paths."""
from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from telethon import errors

from . import config
from .tg import PROTECTED_MSG, is_protected, media_kind, media_name

_BAD_CHARS = re.compile(r"[^\w.\- ()\[\]]+", re.U)
_SEGMENT = re.compile(r"^\w[\w .\-()]{0,63}$", re.U)


def safe_name(name: str) -> str:
    return _BAD_CHARS.sub("_", name).strip(" .")[:120] or "file"


def safe_folder(name: str) -> Path:
    """Resolve a user-supplied sub-folder inside DOWNLOAD_ROOT. Rejects traversal and odd names."""
    root = config.DOWNLOAD_ROOT
    name = (name or "").strip().replace("\\", "/").strip("/")
    path = root
    if name:
        segs = name.split("/")
        if len(segs) > 4:
            raise HTTPException(400, "Folder path is too deep (max 4 levels).")
        for seg in segs:
            if not _SEGMENT.match(seg) or seg.endswith("."):
                raise HTTPException(400, "Folder names may use letters, numbers, spaces, dots, dashes and brackets.")
        path = root.joinpath(*segs)
    path = path.resolve()
    if path != root and root not in path.parents:
        raise HTTPException(400, "Folder must stay inside the download directory.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_folders(limit: int = 200) -> list[str]:
    root = config.DOWNLOAD_ROOT
    out: list[str] = []
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        rel = Path(dirpath).relative_to(root)
        if len(rel.parts) >= 3:
            dirnames[:] = []
        if rel.parts:
            out.append(rel.as_posix())
        if len(out) >= limit:
            break
    return sorted(out)


def unique_path(folder: Path, filename: str) -> Path:
    path = folder / filename
    stem, suffix, n = path.stem, path.suffix, 1
    while path.exists():
        path = folder / f"{stem} ({n}){suffix}"
        n += 1
    return path


class Cancelled(Exception):
    pass


@dataclass
class Item:
    msg_id: int
    name: str
    size: Optional[int] = None
    status: str = "queued"  # queued | downloading | done | failed | blocked | cancelled
    done: int = 0
    error: Optional[str] = None
    path: Optional[str] = None

    def to_dict(self) -> dict:
        return {"msg_id": self.msg_id, "name": self.name, "size": self.size, "status": self.status,
                "done": self.done, "error": self.error, "path": self.path}


@dataclass
class Job:
    id: str
    chat_id: int
    folder: Path
    items: list[Item]
    status: str = "running"  # running | done | cancelled | failed
    error: Optional[str] = None
    created: float = field(default_factory=time.time)
    cancel: bool = False
    task: Optional[asyncio.Task] = None

    def to_dict(self) -> dict:
        total = sum(i.size or 0 for i in self.items)
        done = sum(i.done for i in self.items)
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "status": self.status,
            "error": self.error,
            "folder": self.folder.relative_to(config.DOWNLOAD_ROOT).as_posix() or ".",
            "created": self.created,
            "files_done": sum(1 for i in self.items if i.status == "done"),
            "files_total": len(self.items),
            "bytes_done": done,
            "bytes_total": total,
            "items": [i.to_dict() for i in self.items],
        }


class DownloadManager:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Job]] = {}
        self.active: dict[str, int] = {}
        self.sem = asyncio.Semaphore(config.MAX_CONCURRENT_DOWNLOADS)

    def busy(self, sid: str) -> bool:
        return self.active.get(sid, 0) > 0

    def create(self, sid: str, client, chat_id: int, folder: Path, msgs: list) -> Job:
        items = [Item(msg_id=m.id, name=media_name(m), size=m.file.size if m.file else None) for m in msgs]
        job = Job(id=uuid.uuid4().hex[:12], chat_id=chat_id, folder=folder, items=items)
        self.jobs.setdefault(sid, {})[job.id] = job
        self.active[sid] = self.active.get(sid, 0) + 1
        job.task = asyncio.create_task(self._run(sid, client, job))
        return job

    def list(self, sid: str) -> list[dict]:
        jobs = sorted(self.jobs.get(sid, {}).values(), key=lambda j: j.created, reverse=True)
        return [j.to_dict() for j in jobs]

    def cancel(self, sid: str, job_id: str) -> bool:
        job = self.jobs.get(sid, {}).get(job_id)
        if not job:
            return False
        job.cancel = True
        return True

    def clear_finished(self, sid: str) -> None:
        jobs = self.jobs.get(sid, {})
        for jid in [k for k, j in jobs.items() if j.status != "running"]:
            del jobs[jid]

    def drop(self, sid: str) -> None:
        for job in self.jobs.pop(sid, {}).values():
            job.cancel = True

    # -------------------------------------------------------------- internals
    async def _run(self, sid: str, client, job: Job) -> None:
        try:
            entity = await client.get_entity(job.chat_id)
            for item in job.items:
                if job.cancel:
                    item.status = "cancelled"
                    continue
                async with self.sem:
                    if job.cancel:
                        item.status = "cancelled"
                        continue
                    await self._one(client, entity, job, item)
            if job.cancel:
                job.status = "cancelled"
            elif all(i.status in ("failed", "blocked") for i in job.items):
                job.status = "failed"
            else:
                job.status = "done"
        except asyncio.CancelledError:
            job.status = "cancelled"
            raise
        except Exception as e:  # noqa: BLE001
            job.status = "failed"
            job.error = str(e) or type(e).__name__
        finally:
            self.active[sid] = max(0, self.active.get(sid, 1) - 1)

    async def _one(self, client, entity, job: Job, item: Item) -> None:
        item.status = "downloading"
        target: Optional[Path] = None
        try:
            m = await client.get_messages(entity, ids=item.msg_id)
            if not m or not media_kind(m):
                item.status, item.error = "failed", "The message or its media is no longer available."
                return
            if is_protected(entity, m):  # re-checked at download time, not only when queued
                item.status, item.error = "blocked", PROTECTED_MSG
                return
            if m.file and m.file.size:
                item.size = m.file.size
            target = unique_path(job.folder, f"{item.msg_id}_{safe_name(item.name)}")

            def progress(current: int, total: int) -> None:
                item.done = current
                if total:
                    item.size = total
                if job.cancel:
                    raise Cancelled()

            result = await client.download_media(m, file=str(target), progress_callback=progress)
            final = Path(result) if result else target
            item.path = final.resolve().relative_to(config.DOWNLOAD_ROOT).as_posix()
            item.done = item.size or item.done
            item.status = "done"
        except Cancelled:
            item.status = "cancelled"
        except errors.FloodWaitError as e:
            item.status, item.error = "failed", f"Telegram asked to slow down. Retry in {e.seconds} seconds."
        except asyncio.CancelledError:
            item.status = "cancelled"
            raise
        except Exception as e:  # noqa: BLE001
            item.status, item.error = "failed", str(e) or type(e).__name__
        finally:
            if item.status != "done" and target is not None:
                try:
                    target.unlink(missing_ok=True)  # never leave partial files behind
                except OSError:
                    pass


downloads = DownloadManager()
