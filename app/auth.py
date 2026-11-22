"""Sign-in. Every approval is tied to a signed-in person.

- Google sign-in (Google Identity Services): the browser gets an ID token from Google; the server
  verifies it with Google's tokeninfo endpoint (audience = our client ID, email verified, not
  expired) before creating a session. Enabled when GOOGLE_CLIENT_ID is set. GOOGLE_ALLOWED_DOMAIN
  optionally restricts sign-in to one Workspace domain.
- Local sign-up (name + email, no password): ONLY when Google is not configured, for a local
  demo. Labelled as such on the page and in every approval it records.

Sessions are HMAC-signed cookies; no passwords are ever stored.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

import httpx

COOKIE = "pd_session"
TTL = 12 * 3600


def _secret() -> bytes:
    s = os.environ.get("PAYDESK_SECRET")
    if s:
        return s.encode()
    path = os.path.join(os.path.dirname(__file__), "..", "data", ".session_secret")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    return open(path).read().strip().encode()


def google_client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def make_session(user: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps({**user, "exp": int(time.time()) + TTL}).encode()).decode()
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_session(cookie: str | None) -> dict | None:
    if not cookie or "." not in cookie:
        return None
    body, sig = cookie.rsplit(".", 1)
    if not hmac.compare_digest(sig, hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body.encode()))
    except ValueError:
        return None
    return data if data.get("exp", 0) > time.time() else None


def verify_google(id_token: str) -> dict:
    """Returns {email, name, provider} or raises ValueError."""
    cid = google_client_id()
    if not cid:
        raise ValueError("Google sign-in is not configured (GOOGLE_CLIENT_ID)")
    r = httpx.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": id_token}, timeout=15)
    if r.status_code != 200:
        raise ValueError("Google rejected the sign-in token")
    t = r.json()
    if t.get("aud") != cid:
        raise ValueError("token was issued for a different app")
    if str(t.get("email_verified")).lower() != "true":
        raise ValueError("Google email is not verified")
    if int(t.get("exp", 0)) < time.time():
        raise ValueError("token expired")
    dom = os.environ.get("GOOGLE_ALLOWED_DOMAIN", "").strip().lower()
    if dom and not t.get("email", "").lower().endswith("@" + dom):
        raise ValueError(f"only @{dom} accounts may sign in")
    return {"email": t["email"], "name": t.get("name") or t["email"].split("@")[0], "provider": "google"}


def upsert_user(conn, user: dict):
    conn.execute("""CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL,
                    created_at REAL NOT NULL, last_login REAL NOT NULL)""")
    now = time.time()
    conn.execute("INSERT INTO users VALUES (?,?,?,?,?) ON CONFLICT(email) DO UPDATE SET name=excluded.name, "
                 "provider=excluded.provider, last_login=excluded.last_login",
                 (user["email"].lower(), user["name"], user["provider"], now, now))


def approver_label(user: dict) -> str:
    tag = {"google": "", "admin": " (administrator)"}.get(user.get("provider"), " (local account)")
    return f"{user['name']} <{user['email']}>{tag}"


# ---------------------------------------------------------------- administrator account
# One admin login (username + password). The password is stored only as a salted scrypt hash in
# data/admin.json (git-ignored). On first start, if ADMIN_USERNAME/ADMIN_PASSWORD are not set, a random
# password is generated and written once to data/admin_credentials.txt (git-ignored, owner-only).
# Rotate it with: .venv/bin/python scripts/admin_password.py
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
ADMIN_FILE = os.path.join(DATA_DIR, "admin.json")
CRED_FILE = os.path.join(DATA_DIR, "admin_credentials.txt")
_failures: dict = {}


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()


def set_admin(username: str, password: str):
    salt = secrets.token_bytes(16)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(ADMIN_FILE, "w") as f:
        json.dump({"username": username, "salt": salt.hex(), "hash": _hash(password, salt), "created_at": int(time.time())}, f)
    os.chmod(ADMIN_FILE, 0o600)


def ensure_admin() -> bool:
    """Create the admin account if there is none. Returns True if a password was generated."""
    if os.path.exists(ADMIN_FILE):
        return False
    user, pw = os.environ.get("ADMIN_USERNAME", "").strip(), os.environ.get("ADMIN_PASSWORD", "")
    generated = not pw
    if generated:
        user = user or "admin"
        pw = secrets.token_urlsafe(14)
    set_admin(user, pw)
    if generated:
        with open(CRED_FILE, "w") as f:
            f.write(f"Payables desk administrator (local only; keep private)\nusername: {user}\npassword: {pw}\n"
                    f"Rotate with: .venv/bin/python scripts/admin_password.py\n")
        os.chmod(CRED_FILE, 0o600)
    return generated


def verify_admin(username: str, password: str, client: str = "local") -> dict:
    """Returns the admin session user or raises ValueError. 5 wrong tries lock the login for 5 minutes."""
    now = time.time()
    fails = [t for t in _failures.get(client, []) if now - t < 300]
    if len(fails) >= 5:
        raise ValueError("Too many wrong tries. Wait 5 minutes and try again.")
    try:
        with open(ADMIN_FILE) as f:
            rec = json.load(f)
    except OSError:
        raise ValueError("No administrator account is set up")
    ok = hmac.compare_digest(username.strip(), rec["username"]) & hmac.compare_digest(
        _hash(password, bytes.fromhex(rec["salt"])), rec["hash"])
    if not ok:
        _failures[client] = fails + [now]
        raise ValueError("Wrong username or password")
    _failures.pop(client, None)
    return {"name": "Administrator", "email": f"{rec['username']}@admin.local", "provider": "admin", "role": "admin"}


def is_admin(user: dict | None) -> bool:
    return bool(user and user.get("role") == "admin")
