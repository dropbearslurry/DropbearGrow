"""
Dropbear Team Site — backend server
FastAPI + WebSockets for chat, REST for file repo, OTP email auth
"""

import json
import os
import hashlib
import secrets
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect,
    UploadFile, File, HTTPException, Form, Query
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ── Config ─────────────────────────────────────────────────────────────────
UPLOAD_DIR     = Path(__file__).parent / "uploads"
STATIC_DIR     = Path(__file__).parent / "static"
MAX_CHAT_HISTORY = 200
MAX_UPLOAD_MB    = 50
ALLOWED_DOMAIN   = "dropbearslurry.com.au"
OTP_TTL          = 600   # 10 minutes
SESSION_TTL      = 86400 # 24 hours

SMTP_HOST = os.getenv("SMTP_HOST", "mail.dropbearslurry.com.au")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "admin@dropbearslurry.com.au")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "internal@dropbearslurry.com.au")

CATEGORIES = ["general", "receipts", "marketing", "production", "assets"]

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

# ── Auth state ──────────────────────────────────────────────────────────────
otp_store: dict[str, dict]     = {}  # email  → {otp, expires}
session_store: dict[str, dict] = {}  # token  → {email, username, expires}


def _ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _valid_session(token: str) -> dict | None:
    entry = session_store.get(token)
    if not entry:
        return None
    if _ts() > entry["expires"]:
        session_store.pop(token, None)
        return None
    return entry


def _require_token(token: str):
    if not _valid_session(token):
        raise HTTPException(401, "Valid session token required")


# ── Chat state ──────────────────────────────────────────────────────────────
chat_history: list[dict]           = []
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


# ── Auth endpoints ──────────────────────────────────────────────────────────
@app.post("/api/auth/request-otp")
async def request_otp(email: str = Form(...)):
    email = email.lower().strip()
    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        raise HTTPException(403, f"A @{ALLOWED_DOMAIN} email address is required")

    otp = f"{secrets.randbelow(1000000):06d}"
    otp_store[email] = {"otp": otp, "expires": _ts() + OTP_TTL}

    try:
        _send_otp_email(email, otp)
    except Exception as e:
        otp_store.pop(email, None)
        raise HTTPException(502, f"Failed to send email: {e}")

    return {"sent": True}


@app.post("/api/auth/verify-otp")
async def verify_otp(email: str = Form(...), otp: str = Form(...)):
    email = email.lower().strip()
    entry = otp_store.get(email)

    if not entry:
        raise HTTPException(401, "No code was requested for this address")
    if _ts() > entry["expires"]:
        otp_store.pop(email, None)
        raise HTTPException(401, "Code has expired — request a new one")
    if entry["otp"] != otp.strip():
        raise HTTPException(401, "Incorrect code")

    otp_store.pop(email)
    token    = secrets.token_urlsafe(32)
    username = email.split("@")[0]
    session_store[token] = {
        "email":    email,
        "username": username,
        "expires":  _ts() + SESSION_TTL,
    }
    return {"token": token, "username": username}


# ── WebSocket endpoint ──────────────────────────────────────────────────────
@app.websocket("/ws/{username}")
async def chat_ws(websocket: WebSocket, username: str, token: str = Query(...)):
    session = _valid_session(token)
    if not session or session["username"] != username:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    connected_users[username] = websocket
    for msg in chat_history[-50:]:
        await websocket.send_text(json.dumps(msg))
    await manager.broadcast_system(f"{username} joined")
    await manager.broadcast_userlist()
    try:
        while True:
            raw  = await websocket.receive_text()
            data = json.loads(raw)
            text = str(data.get("text", "")).strip()[:2000]
            if text:
                await manager.broadcast({
                    "type": "chat",
                    "user": username,
                    "text": text,
                    "ts":   _now(),
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
        "name":     p.name,
        "category": category,
        "size":     stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "sha256":   _file_hash(p),
    }


@app.get("/api/categories")
async def get_categories(token: str = Query(...)):
    _require_token(token)
    return CATEGORIES


@app.get("/api/files")
async def list_files(token: str = Query(...), category: str = None):
    _require_token(token)
    cats  = [_safe_category(category)] if category else CATEGORIES
    files = []
    for cat in cats:
        for p in sorted((UPLOAD_DIR / cat).iterdir()):
            if p.is_file():
                files.append(_file_entry(p, cat))
    return files


@app.post("/api/files")
async def upload_file(
    file:     UploadFile = File(...),
    uploader: str        = Form(default="unknown"),
    category: str        = Form(default="general"),
    token:    str        = Form(...),
):
    _require_token(token)
    category = _safe_category(category)
    cat_dir  = UPLOAD_DIR / category

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
        "ts":   _now(),
    })
    return {"name": dest.name, "category": category, "size": len(content)}


@app.get("/api/files/{category}/{filename}")
async def download_file(category: str, filename: str, token: str = Query(...)):
    _require_token(token)
    path = UPLOAD_DIR / _safe_category(category) / _safe_name(filename)
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(path, filename=path.name)


@app.delete("/api/files/{category}/{filename}")
async def delete_file(category: str, filename: str, token: str = Query(...)):
    _require_token(token)
    path = UPLOAD_DIR / _safe_category(category) / _safe_name(filename)
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
def _send_otp_email(to: str, otp: str):
    body = (
        f"Your Dropbear Slurry access code is:\n\n"
        f"    {otp}\n\n"
        f"This code expires in 10 minutes. Do not share it.\n\n"
        f"If you did not request this, ignore this email."
    )
    msg            = MIMEText(body)
    msg["Subject"] = f"Dropbear access code: {otp}"
    msg["From"]    = SMTP_FROM
    msg["To"]      = to

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.ehlo()
        s.starttls()
        s.ehlo()
        if SMTP_PASS:
            s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


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
