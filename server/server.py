"""
Dropbear Team Site — backend server
FastAPI + WebSockets for chat, REST for file repo
"""

import json
import os
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect,
    UploadFile, File, HTTPException, Form
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ── Config ─────────────────────────────────────────────────────────────────
UPLOAD_DIR   = Path(__file__).parent / "uploads"
STATIC_DIR   = Path(__file__).parent / "static"
MAX_CHAT_HISTORY = 200
MAX_UPLOAD_MB    = 50

CATEGORIES = ["general", "receipts", "marketing", "production", "assets"]

# Ensure base upload dir and all category subdirs exist at startup
UPLOAD_DIR.mkdir(exist_ok=True)
for _cat in CATEGORIES:
    (UPLOAD_DIR / _cat).mkdir(exist_ok=True)

app = FastAPI(title="Dropbear", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# ── Chat state ──────────────────────────────────────────────────────────────
chat_history: list[dict] = []
connected_users: dict[str, WebSocket] = {}


class ConnectionManager:
    async def disconnect(self, username: str):
        connected_users.pop(username, None)
        await self.broadcast_system(f"{username} left")
        await self.broadcast_userlist()

    async def broadcast(self, message: dict):
        chat_history.append(message)
        if len(chat_history) > MAX_CHAT_HISTORY:
            del chat_history[:-MAX_CHAT_HISTORY]
        dead = []
        for uname, ws in connected_users.items():
            try:
                await ws.send_text(json.dumps(message))
            except Exception:
                dead.append(uname)
        for u in dead:
            connected_users.pop(u, None)

    async def broadcast_system(self, text: str):
        await self.broadcast({"type": "system", "text": text, "ts": _now()})

    async def broadcast_userlist(self):
        payload = json.dumps({"type": "userlist", "users": list(connected_users.keys())})
        dead = []
        for uname, ws in connected_users.items():
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(uname)
        for u in dead:
            connected_users.pop(u, None)


manager = ConnectionManager()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


# ── WebSocket endpoint ──────────────────────────────────────────────────────
@app.websocket("/ws/{username}")
async def chat_ws(websocket: WebSocket, username: str):
    username = username.strip()[:24] or "anon"
    await websocket.accept()
    connected_users[username] = websocket
    for msg in chat_history[-50:]:
        await websocket.send_text(json.dumps(msg))
    await manager.broadcast_system(f"{username} joined")
    await manager.broadcast_userlist()
    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            text = str(data.get("text", "")).strip()[:2000]
            if text:
                await manager.broadcast({
                    "type": "chat",
                    "user": username,
                    "text": text,
                    "ts": _now(),
                })
    except (WebSocketDisconnect, Exception):
        await manager.disconnect(username)


# ── File repo endpoints ─────────────────────────────────────────────────────
def _safe_name(filename: str) -> str:
    name = Path(filename).name
    safe = "".join(c for c in name if c.isalnum() or c in "-_. ()[]")
    return safe or "unnamed"


def _safe_category(category: str) -> str:
    return category if category in CATEGORIES else "general"


def _file_entry(p: Path, category: str) -> dict:
    stat = p.stat()
    return {
        "name": p.name,
        "category": category,
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "sha256": _file_hash(p),
    }


@app.get("/api/categories")
async def get_categories():
    return CATEGORIES


@app.get("/api/files")
async def list_files(category: str = None):
    cats = [_safe_category(category)] if category else CATEGORIES
    files = []
    for cat in cats:
        cat_dir = UPLOAD_DIR / cat
        for p in sorted(cat_dir.iterdir()):
            if p.is_file():
                files.append(_file_entry(p, cat))
    return files


@app.post("/api/files")
async def upload_file(
    file: UploadFile = File(...),
    uploader: str = Form(default="unknown"),
    category: str = Form(default="general"),
):
    category = _safe_category(category)
    cat_dir = UPLOAD_DIR / category

    name = _safe_name(file.filename or "upload")
    dest = cat_dir / name
    counter = 1
    stem, suffix = os.path.splitext(name)
    while dest.exists():
        dest = cat_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    content = await file.read()
    if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {MAX_UPLOAD_MB} MB limit")
    dest.write_bytes(content)

    await manager.broadcast({
        "type": "system",
        "text": f"{uploader} uploaded \"{dest.name}\" → {category} ({_human_size(len(content))})",
        "ts": _now(),
    })
    return {"name": dest.name, "category": category, "size": len(content)}


@app.get("/api/files/{category}/{filename}")
async def download_file(category: str, filename: str):
    category = _safe_category(category)
    path = UPLOAD_DIR / category / _safe_name(filename)
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(path, filename=path.name)


@app.delete("/api/files/{category}/{filename}")
async def delete_file(category: str, filename: str):
    category = _safe_category(category)
    path = UPLOAD_DIR / category / _safe_name(filename)
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "File not found")
    path.unlink()
    return {"deleted": path.name, "category": category}


# ── Static / root ───────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


# ── Helpers ─────────────────────────────────────────────────────────────────
def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=7331, reload=True)
