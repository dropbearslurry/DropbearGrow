"""
Dropbear Team Site — backend server
FastAPI + WebSockets for chat, REST for file repo, account-based auth
"""

import json
import os
import re
import hashlib
import secrets
import smtplib
import string
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import httpx
from fastapi import (
    FastAPI, WebSocket, WebSocketDisconnect,
    UploadFile, File, HTTPException, Form, Query, Header
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from passlib.context import CryptContext

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ── Config ─────────────────────────────────────────────────────────────────
UPLOAD_DIR      = Path(__file__).parent / "uploads"
STATIC_DIR      = Path(__file__).parent / "static"
ACCOUNTS_FILE   = Path(__file__).parent / "accounts.json"
MAX_CHAT_HISTORY = 200
MAX_UPLOAD_MB    = 50
ALLOWED_DOMAIN   = "dropbearslurry.com.au"
SESSION_TTL      = 90 * 24 * 3600   # 90 days
PASSWORD_EXPIRY  = 90 * 24 * 3600   # 90 days
CHANGE_TTL       = 900              # 15 min to complete a forced change

SMTP_HOST  = os.getenv("SMTP_HOST",  "mail.dropbearslurry.com.au")
SMTP_PORT  = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER  = os.getenv("SMTP_USER",  "admin@dropbearslurry.com.au")
SMTP_PASS  = os.getenv("SMTP_PASS",  "")
SMTP_FROM  = os.getenv("SMTP_FROM",  "internal@dropbearslurry.com.au")
ADMIN_KEY  = os.getenv("ADMIN_KEY",  "")

SQUARE_TOKEN     = os.getenv("SQUARE_ACCESS_TOKEN", "")
SQUARE_ENV       = os.getenv("SQUARE_ENVIRONMENT", "production")  # or "sandbox"
GCAL_EMBED_URL   = os.getenv("GCAL_EMBED_URL", "")  # full Google Calendar embed URL

CATEGORIES = ["general", "receipts", "marketing", "production", "assets"]

UPLOAD_DIR.mkdir(exist_ok=True)
for _cat in CATEGORIES:
    (UPLOAD_DIR / _cat).mkdir(exist_ok=True)

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

app = FastAPI(title="Dropbear", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


# ── Account storage ─────────────────────────────────────────────────────────
def _load_accounts() -> dict:
    if ACCOUNTS_FILE.exists():
        return json.loads(ACCOUNTS_FILE.read_text())
    return {}


def _save_accounts(accounts: dict):
    ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=2))


# ── Auth state (in-memory) ───────────────────────────────────────────────────
# token → {email, username, expires}
session_store: dict[str, dict] = {}
# change_token → {email, expires}
change_store: dict[str, dict] = {}


def _ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_session(token: str) -> dict | None:
    entry = session_store.get(token)
    if not entry or _ts() > entry["expires"]:
        session_store.pop(token, None)
        return None
    return entry


def _require_token(token: str) -> dict:
    s = _valid_session(token)
    if not s:
        raise HTTPException(401, "Valid session token required")
    return s


# ── Password rules ───────────────────────────────────────────────────────────
def _check_password(pw: str) -> str | None:
    """Return error string or None if password is acceptable."""
    if len(pw) < 8:
        return "At least 8 characters required"
    if not re.search(r"[A-Z]", pw):
        return "Must contain an uppercase letter"
    if not re.search(r"[a-z]", pw):
        return "Must contain a lowercase letter"
    if not re.search(r"\d", pw):
        return "Must contain a number"
    if not re.search(r"[^A-Za-z0-9]", pw):
        return "Must contain a special character"
    return None


def _gen_initial_password() -> str:
    """Generate a readable but secure temporary password."""
    alpha  = string.ascii_letters
    digits = string.digits
    special = "!@#$%^&*"
    pool = alpha + digits + special
    while True:
        pw = "".join(secrets.choice(pool) for _ in range(12))
        if _check_password(pw) is None:
            return pw


# ── Chat state ───────────────────────────────────────────────────────────────
chat_history: list[dict]              = []
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


# ── Admin: create account ────────────────────────────────────────────────────
@app.post("/api/admin/create-account")
async def create_account(
    email: str = Form(...),
    x_admin_key: str = Header(default=""),
):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(403, "Invalid admin key")

    email = email.lower().strip()
    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        raise HTTPException(400, f"Must be a @{ALLOWED_DOMAIN} address")

    accounts = _load_accounts()
    if email in accounts:
        raise HTTPException(409, "Account already exists")

    initial_pw   = _gen_initial_password()
    username     = email.split("@")[0]
    accounts[email] = {
        "username":            username,
        "password_hash":       pwd_ctx.hash(initial_pw),
        "is_initial":          True,
        "created_at":          _iso(),
        "password_changed_at": _iso(),
    }
    _save_accounts(accounts)

    try:
        _send_welcome_email(email, username, initial_pw)
    except Exception as e:
        # Roll back so a retry is possible
        accounts.pop(email)
        _save_accounts(accounts)
        raise HTTPException(502, f"Account created but email failed: {e}")

    return {"created": email, "username": username}


# ── Auth: check email (auto-provisions valid domain addresses) ────────────────
@app.post("/api/auth/check-email")
async def check_email(email: str = Form(...)):
    email = email.lower().strip()
    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        raise HTTPException(403, f"A @{ALLOWED_DOMAIN} email address is required")

    accounts = _load_accounts()

    if email not in accounts:
        # Auto-create account and send welcome email
        initial_pw = _gen_initial_password()
        username   = email.split("@")[0]
        accounts[email] = {
            "username":            username,
            "password_hash":       pwd_ctx.hash(initial_pw),
            "is_initial":          True,
            "created_at":          _iso(),
            "password_changed_at": _iso(),
        }
        _save_accounts(accounts)
        try:
            _send_welcome_email(email, username, initial_pw)
        except Exception as e:
            accounts.pop(email)
            _save_accounts(accounts)
            raise HTTPException(502, f"Failed to send welcome email: {e}")
        return {"is_initial": True, "is_expired": False, "created": True}

    account    = accounts[email]
    changed_at = datetime.fromisoformat(account["password_changed_at"])
    expired    = (datetime.now(timezone.utc) - changed_at).total_seconds() > PASSWORD_EXPIRY
    return {
        "is_initial": account.get("is_initial", False),
        "is_expired": expired,
        "created":    False,
    }


# ── Auth: login ──────────────────────────────────────────────────────────────
@app.post("/api/auth/login")
async def login(email: str = Form(...), password: str = Form(...)):
    email    = email.lower().strip()
    accounts = _load_accounts()
    account  = accounts.get(email)

    if not account or not pwd_ctx.verify(password, account["password_hash"]):
        raise HTTPException(401, "Incorrect email or password")

    username = account["username"]

    # Check if password change is required (initial or expired)
    changed_at = datetime.fromisoformat(account["password_changed_at"])
    expired    = (datetime.now(timezone.utc) - changed_at).total_seconds() > PASSWORD_EXPIRY
    need_change = account.get("is_initial", False) or expired

    if need_change:
        reason = "initial" if account.get("is_initial") else "expired"
        change_token = secrets.token_urlsafe(32)
        change_store[change_token] = {
            "email":   email,
            "expires": _ts() + CHANGE_TTL,
        }
        return {
            "require_password_change": True,
            "reason":                  reason,
            "change_token":            change_token,
            "username":                username,
        }

    token = secrets.token_urlsafe(32)
    session_store[token] = {
        "email":    email,
        "username": username,
        "expires":  _ts() + SESSION_TTL,
    }
    return {"token": token, "username": username}


# ── Auth: set password ───────────────────────────────────────────────────────
@app.post("/api/auth/set-password")
async def set_password(
    change_token: str = Form(...),
    new_password: str = Form(...),
):
    entry = change_store.get(change_token)
    if not entry or _ts() > entry["expires"]:
        change_store.pop(change_token, None)
        raise HTTPException(401, "Password change session expired — please log in again")

    err = _check_password(new_password)
    if err:
        raise HTTPException(400, err)

    email    = entry["email"]
    accounts = _load_accounts()
    if email not in accounts:
        raise HTTPException(404, "Account not found")

    accounts[email]["password_hash"]       = pwd_ctx.hash(new_password)
    accounts[email]["is_initial"]          = False
    accounts[email]["password_changed_at"] = _iso()
    _save_accounts(accounts)
    change_store.pop(change_token)

    username = accounts[email]["username"]
    token    = secrets.token_urlsafe(32)
    session_store[token] = {
        "email":    email,
        "username": username,
        "expires":  _ts() + SESSION_TTL,
    }
    return {"token": token, "username": username}


# ── WebSocket ────────────────────────────────────────────────────────────────
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


# ── File repo ────────────────────────────────────────────────────────────────
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


# ── Integrations ─────────────────────────────────────────────────────────────

@app.get("/api/integrations/config")
async def integrations_config(token: str = Query(...)):
    """Tell the client which integrations are configured."""
    _require_token(token)
    return {
        "square":   bool(SQUARE_TOKEN),
        "gcal_url": GCAL_EMBED_URL or None,
    }


@app.get("/api/integrations/square")
async def square_summary(token: str = Query(...)):
    """Proxy today's Square sales summary — keeps the access token server-side."""
    _require_token(token)
    if not SQUARE_TOKEN:
        raise HTTPException(503, "Square not configured")

    base_url = (
        "https://connect.squareupsandbox.com"
        if SQUARE_ENV == "sandbox"
        else "https://connect.squareup.com"
    )

    now   = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    headers = {
        "Authorization": f"Bearer {SQUARE_TOKEN}",
        "Square-Version": "2024-04-17",
        "Content-Type":  "application/json",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        # Fetch today's completed payments
        resp = await client.get(
            f"{base_url}/v2/payments",
            headers=headers,
            params={
                "begin_time": start,
                "sort_order": "DESC",
                "limit":      200,
            },
        )

    if resp.status_code != 200:
        raise HTTPException(502, f"Square API error: {resp.status_code}")

    payments = resp.json().get("payments", [])
    completed = [p for p in payments if p.get("status") == "COMPLETED"]

    total_cents = sum(
        p.get("total_money", {}).get("amount", 0) for p in completed
    )
    currency = (completed[0].get("total_money", {}).get("currency", "AUD")
                if completed else "AUD")

    return {
        "date":        now.strftime("%d %b %Y"),
        "order_count": len(completed),
        "total":       total_cents / 100,
        "currency":    currency,
        "as_of":       now.strftime("%H:%M"),
    }


# ── Static / root ────────────────────────────────────────────────────────────
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def root():
        return FileResponse(STATIC_DIR / "index.html")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _send_welcome_email(to: str, username: str, initial_pw: str):
    body = (
        f"Hi {username},\n\n"
        f"Your Dropbear Slurry internal account has been created.\n\n"
        f"    Temporary password: {initial_pw}\n\n"
        f"You will be required to set a new password on first login.\n\n"
        f"Passwords must be at least 8 characters and include uppercase, "
        f"lowercase, a number, and a special character.\n\n"
        f"Passwords expire every 90 days.\n\n"
        f"Do not share this email."
    )
    msg            = MIMEText(body)
    msg["Subject"] = "Your Dropbear Slurry account"
    msg["From"]    = SMTP_FROM
    msg["To"]      = to
    _smtp_send(msg)


def _smtp_send(msg):
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


