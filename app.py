#!/usr/bin/env python3
"""
Dropzone v6 - a private, self-hosted share point for your home network.

Features: file & text sharing, live device detection, activity feed, chat,
pinned items, per-item share links, bulk delete / zip download, send-to-device,
burn-after-read notes, per-item expiry, server-side URL fetch, storage stats,
device renaming, instant push updates (SSE with polling fallback), and
password-protected admin / user accounts.

Environment variables (all optional):
    DROPZONE_PORT            port to listen on                      (default 8000)
    DROPZONE_DATA            data directory                         (default ./data)
    DROPZONE_ADMIN_PASSWORD  bootstrap/overwrite the "admin" account password at startup
    DROPZONE_USER_PASSWORD   bootstrap/overwrite the "user" account password at startup
    DROPZONE_PIN             legacy shared PIN; acts as the "user" password if no user account exists
    DROPZONE_SESSION_HOURS   how long a login lasts                 (default 24)
    DROPZONE_HTTPS           1 = mark cookies Secure (set when served over HTTPS, e.g. tailscale serve)
    DROPZONE_TRUST_PROXY     1 = trust X-Forwarded-* from a reverse proxy in front of the app
    DROPZONE_PUBLIC_SHARE    1 = /s/<token> share links work without logging in
    DROPZONE_MAX_MB          max upload size in MB                  (default 2048)
    DROPZONE_EXPIRE_DAYS     auto-delete items older than           (default 0 = never)
    DROPZONE_WS_PUBLIC_PORT  bridge port browsers use over HTTPS    (default = DROPZONE_WS_PORT)

Account management (run on the server, then restart the service):
    python app.py set-password admin      # prompts for a new admin password
    python app.py set-password user       # prompts for a new user password
    python app.py users                   # list accounts
    python app.py remove-user user        # delete an account
"""

import io
import os
import re
import sys
import json
import time
import uuid
import hmac
import shutil
import sqlite3
import zipfile
import hashlib
import secrets
import getpass
import tempfile
import threading
import mimetypes
import ipaddress
import urllib.request
from datetime import timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import (
    Flask, request, session, redirect, url_for,
    render_template, jsonify, send_file, abort, Response, make_response,
)
import qrcode
import qrcode.image.svg

# --------------------------------------------------------------------------- config
def _flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")

HERE          = os.path.dirname(os.path.abspath(__file__))
PORT          = int(os.environ.get("DROPZONE_PORT", "8000"))
DATA_DIR      = os.environ.get("DROPZONE_DATA", os.path.join(HERE, "data"))
PIN           = os.environ.get("DROPZONE_PIN", "").strip()
MAX_MB        = int(os.environ.get("DROPZONE_MAX_MB", "2048"))
EXPIRE_DAYS   = float(os.environ.get("DROPZONE_EXPIRE_DAYS", "0"))
SESSION_HOURS = float(os.environ.get("DROPZONE_SESSION_HOURS", "24"))
HTTPS         = _flag("DROPZONE_HTTPS")
TRUST_PROXY   = _flag("DROPZONE_TRUST_PROXY")
PUBLIC_SHARE  = _flag("DROPZONE_PUBLIC_SHARE")

FILES_DIR   = os.path.join(DATA_DIR, "files")
CAPTURES_DIR = os.path.join(DATA_DIR, "captures")
CHUNK_DIR    = os.path.join(DATA_DIR, "chunks")
DB_PATH     = os.path.join(DATA_DIR, "dropzone.db")
SECRET_PATH = os.path.join(DATA_DIR, "secret.key")
USERS_PATH  = os.path.join(DATA_DIR, "users.json")
INDEX_HTML  = os.path.join(HERE, "templates", "index.html")

DEVICE_LIST_WINDOW   = 300
DEVICE_ACTIVE_WINDOW = 60
URL_FETCH_CAP        = MAX_MB * 1024 * 1024

LOGIN_MAX_FAILS    = 5       # failed attempts per IP before lockout
LOGIN_LOCK_SECONDS = 300     # lockout length
PBKDF2_ITERATIONS  = 600_000
ROLES              = ("admin", "user")
PERM_KEYS          = ("download", "upload", "notes", "delete", "chat", "screen", "fetch_url", "see_all")
DEFAULT_PERMS      = {"download": True, "upload": True, "notes": True, "delete": True, "chat": True,
                      "screen": False, "fetch_url": False, "see_all": True}
LIMIT_KEYS         = ("max_upload_mb", "quota_mb", "max_items", "lan_only")
_UNSET             = object()
CLI_COMMANDS       = ("set-password", "users", "remove-user")

os.makedirs(FILES_DIR, exist_ok=True)
os.makedirs(CAPTURES_DIR, exist_ok=True)
os.makedirs(CHUNK_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = False   # works on both the http LAN and the https Funnel address
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=SESSION_HOURS)

if TRUST_PROXY:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

def _write_private(path, data):
    """Write a file readable only by the service user."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    try: os.chmod(path, 0o600)
    except OSError: pass

if os.path.exists(SECRET_PATH):
    app.secret_key = open(SECRET_PATH, "rb").read()
else:
    app.secret_key = os.urandom(32)
    _write_private(SECRET_PATH, app.secret_key)

# --------------------------------------------------------------------------- accounts
_users_lock = threading.Lock()

def load_users():
    try:
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

def save_users(users):
    _write_private(USERS_PATH, json.dumps(users, indent=1).encode("utf-8"))

def _hash_password(password, salt, iterations):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()

def set_password(name, password, role=None):
    name = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{1,32}", name):
        raise ValueError("username may only contain a-z, 0-9, _ . - (max 32 chars)")
    if len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    with _users_lock:
        users = load_users()
        cur = users.get(name, {})
        role = role or cur.get("role") or ("admin" if name == "admin" else "user")
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        salt = secrets.token_bytes(16)
        users[name] = {
            "role": role,
            "salt": salt.hex(),
            "iter": PBKDF2_ITERATIONS,
            "hash": _hash_password(password, salt, PBKDF2_ITERATIONS),
            "ver": int(cur.get("ver", 0)) + 1,          # bumping this logs out existing sessions
            "updated": time.time(),
        }
        save_users(users)
    return role

def remove_user(name):
    with _users_lock:
        users = load_users()
        if name not in users: return False
        del users[name]
        save_users(users)
    return True

def effective_perms(rec):
    if (rec or {}).get("role") == "admin":
        return {k: True for k in PERM_KEYS}
    p = dict(DEFAULT_PERMS)
    p.update({k: bool(v) for k, v in ((rec or {}).get("perms") or {}).items() if k in PERM_KEYS})
    return p

def account_expired(name):
    rec = load_users().get((name or "").strip().lower())
    exp = (rec or {}).get("expires")
    return bool(exp) and time.time() > float(exp)

def update_account(name, role=None, perms=None, limits=None, expires=_UNSET):
    """Change an account's role/privileges/limits/expiry without touching its password or logging it out."""
    name = (name or "").strip().lower()
    with _users_lock:
        users = load_users(); rec = users.get(name)
        if not rec: raise ValueError("no such account")
        if role in ROLES: rec["role"] = role
        if isinstance(perms, dict):
            rec.setdefault("perms", {}).update({k: bool(v) for k, v in perms.items() if k in PERM_KEYS})
        if isinstance(limits, dict):
            L = rec.setdefault("limits", {})
            for k in LIMIT_KEYS:
                if k in limits:
                    L[k] = bool(limits[k]) if k == "lan_only" else max(0, int(limits[k] or 0))
        if expires is not _UNSET:
            rec["expires"] = float(expires) if expires else None
        rec["updated"] = time.time()
        save_users(users)
    return rec

def auth_enabled():
    """True once any account (or the legacy PIN) exists. Otherwise the share point is open."""
    return bool(PIN) or bool(load_users())

def check_password(name, password):
    """Return the account record on success, else None. Constant-time comparisons throughout."""
    name = (name or "").strip().lower()
    users = load_users()
    rec = users.get(name)
    if rec:
        salt = bytes.fromhex(rec["salt"])
        calc = _hash_password(password, salt, int(rec.get("iter", PBKDF2_ITERATIONS)))
        if hmac.compare_digest(calc, rec["hash"]):
            return {"name": name, "role": rec.get("role", "user"), "ver": int(rec.get("ver", 1))}
        return None
    # legacy: the shared PIN unlocks the "user" role when no user account has been created
    if PIN and name == "user" and hmac.compare_digest(password.encode(), PIN.encode()):
        return {"name": "user", "role": "user", "ver": 0}
    # burn the same time as a real check so usernames can't be probed by timing
    _hash_password(password, b"\0" * 16, PBKDF2_ITERATIONS)
    return None

def bootstrap_accounts_from_env():
    for env, name in (("DROPZONE_ADMIN_PASSWORD", "admin"), ("DROPZONE_USER_PASSWORD", "user")):
        pw = os.environ.get(env, "")
        if pw:
            try:
                set_password(name, pw, role="admin" if name == "admin" else "user")
            except ValueError as e:
                print(f"[dropzone] ignoring {env}: {e}", file=sys.stderr)

# ------------------------------------------------------------------ login throttling
_fails = {}            # ip -> {"n": int, "until": float}
_fails_lock = threading.Lock()

def login_locked(ip):
    with _fails_lock:
        f = _fails.get(ip)
        if f and f["until"] > time.time():
            return int(f["until"] - time.time()) + 1
        return 0

def login_failed(ip):
    with _fails_lock:
        f = _fails.setdefault(ip, {"n": 0, "until": 0})
        f["n"] += 1
        if f["n"] >= LOGIN_MAX_FAILS:
            f["n"] = 0
            f["until"] = time.time() + LOGIN_LOCK_SECONDS

def login_succeeded(ip):
    with _fails_lock:
        _fails.pop(ip, None)

# --------------------------------------------------------------------------- database
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def column_names(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS files   (id TEXT PRIMARY KEY, name TEXT, stored TEXT, size INTEGER, mime TEXT, created REAL,
                                            pinned INTEGER DEFAULT 0, token TEXT, owner_ip TEXT, target_ip TEXT, expires REAL, label TEXT, owner_user TEXT);
        CREATE TABLE IF NOT EXISTS notes   (id TEXT PRIMARY KEY, content TEXT, created REAL,
                                            pinned INTEGER DEFAULT 0, owner_ip TEXT, target_ip TEXT, expires REAL, burn INTEGER DEFAULT 0, label TEXT);
        CREATE TABLE IF NOT EXISTS devices (ip TEXT PRIMARY KEY, name TEXT, custom_name TEXT, type TEXT, first_seen REAL, last_seen REAL);
        CREATE TABLE IF NOT EXISTS activity(id TEXT PRIMARY KEY, icon TEXT, text TEXT, ip TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, ip TEXT, dev TEXT, text TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS meta    (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS access_log(id TEXT PRIMARY KEY, ts REAL, ip TEXT, via TEXT, user TEXT, role TEXT,
                                            method TEXT, path TEXT, status INTEGER, detail TEXT, ua TEXT, browser TEXT,
                                            os TEXT, device TEXT, referer TEXT, lang TEXT, client TEXT);
        CREATE INDEX IF NOT EXISTS access_log_ts ON access_log(ts);
        CREATE INDEX IF NOT EXISTS access_log_ip ON access_log(ip);
        CREATE TABLE IF NOT EXISTS geo_cache (ip TEXT PRIMARY KEY, json TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS shares  (token TEXT PRIMARY KEY, ids TEXT, pin_hash TEXT, pin_salt TEXT, pin_iter INTEGER,
                                            created REAL, owner_user TEXT, owner_ip TEXT, expires REAL, downloads INTEGER DEFAULT 0, label TEXT);
        """)
        # migrations for anyone upgrading from v3
        for table, cols in (("files", ["pinned INTEGER DEFAULT 0", "token TEXT", "owner_ip TEXT", "target_ip TEXT", "expires REAL", "label TEXT", "owner_user TEXT"]),
                            ("notes", ["pinned INTEGER DEFAULT 0", "owner_ip TEXT", "target_ip TEXT", "expires REAL", "burn INTEGER DEFAULT 0", "label TEXT", "owner_user TEXT"]),
                            ("devices", ["custom_name TEXT"])):
            have = column_names(c, table)
            for col in cols:
                if col.split()[0] not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        if c.execute("SELECT v FROM meta WHERE k='ver'").fetchone() is None:
            c.execute("INSERT INTO meta (k,v) VALUES ('ver','0')")

# ------------------------------------------------------------------ change signal (SSE)
_change = threading.Condition()
_version = {"n": 0}

def bump(conn=None):
    with _change:
        _version["n"] += 1
        _change.notify_all()

# in-flight uploads, so receivers can watch progress live
_uploads = {}            # id -> {name, who, owner_ip, target_ip, pct, count, ts}
_uploads_lock = threading.Lock()

def _prune_uploads():
    now = time.time()
    with _uploads_lock:
        for k in [k for k, v in _uploads.items() if now - v["ts"] > 20]:
            _uploads.pop(k, None)

# --------------------------------------------------------------------------- helpers
IMAGE_MIMES = {"image/png","image/jpeg","image/gif","image/webp","image/svg+xml","image/bmp"}
VIDEO_PREFIX, AUDIO_PREFIX = "video/", "audio/"
TEXT_PREVIEW = {"text/plain","text/csv","text/markdown","application/json","text/html","application/javascript"}
# uploaded content that could run script in the app's origin if rendered inline
FORCE_DOWNLOAD_MIMES = {"text/html","application/xhtml+xml","text/xml","application/xml",
                        "application/javascript","text/javascript","application/x-javascript"}

def client_ip():
    # X-Forwarded-For is only honoured when DROPZONE_TRUST_PROXY=1 (ProxyFix rewrites remote_addr);
    # otherwise anyone could spoof another device's address and read items sent to it.
    return request.remote_addr or "?"

def is_remote(ip):
    try:
        a = ipaddress.ip_address(ip)
        if a.is_loopback: return False
        if a in ipaddress.ip_network("100.64.0.0/10"): return True   # tailscale CGNAT
        return not a.is_private
    except ValueError:
        return False

def ua_device(ua):
    u = (ua or "").lower()
    if "ipad" in u or "tablet" in u:                     t = "tablet"
    elif "iphone" in u or "ipod" in u or "mobi" in u:    t = "phone"
    elif "android" in u and "mobile" in u:               t = "phone"
    elif "android" in u:                                 t = "tablet"
    elif "smarttv" in u or "smart-tv" in u or "crkey" in u or "appletv" in u: t = "tv"
    else:                                                t = "laptop"
    if   "iphone" in u:                     n = "iPhone"
    elif "ipad" in u:                       n = "iPad"
    elif "android" in u:                    n = "Android"
    elif "macintosh" in u or "mac os" in u: n = "Mac"
    elif "windows" in u:                    n = "Windows PC"
    elif "cros" in u:                       n = "Chromebook"
    elif "linux" in u:                      n = "Linux"
    else:                                   n = "Device"
    return t, n

def touch_device(conn):
    ip = client_ip(); now = time.time()
    t, n = ua_device(request.headers.get("User-Agent",""))
    row = conn.execute("SELECT ip, name, custom_name FROM devices WHERE ip=?", (ip,)).fetchone()
    if row:
        conn.execute("UPDATE devices SET last_seen=?, type=?, name=? WHERE ip=?", (now, t, n, ip))
        return row["custom_name"] or row["name"] or n
    conn.execute("INSERT INTO devices (ip,name,type,first_seen,last_seen) VALUES (?,?,?,?,?)", (ip,n,t,now,now))
    log_activity(conn, "join", f"{n} joined the network", ip)
    return n

def display_name(conn, ip):
    r = conn.execute("SELECT name, custom_name FROM devices WHERE ip=?", (ip,)).fetchone()
    return (r["custom_name"] or r["name"]) if r else (ip or "Device")

def log_activity(conn, icon, text, ip):
    conn.execute("INSERT INTO activity (id,icon,text,ip,created) VALUES (?,?,?,?,?)",
                 (uuid.uuid4().hex, icon, text, ip, time.time()))
    conn.execute("""DELETE FROM activity WHERE id NOT IN
                    (SELECT id FROM activity ORDER BY created DESC LIMIT 120)""")

def purge_expired(conn):
    now = time.time()
    changed = False
    conds, args = ["(expires IS NOT NULL AND expires < ?)"], [now]
    if EXPIRE_DAYS > 0:
        conds.append("created < ?"); args.append(now - EXPIRE_DAYS*86400)
    where = " OR ".join(conds)
    for r in conn.execute(f"SELECT stored FROM files WHERE {where}", args).fetchall():
        try: os.remove(os.path.join(FILES_DIR, r["stored"]))
        except OSError: pass
        changed = True
    conn.execute(f"DELETE FROM files WHERE {where}", args)
    if conn.execute(f"SELECT COUNT(*) c FROM notes WHERE {where}", args).fetchone()["c"]:
        changed = True
    conn.execute(f"DELETE FROM notes WHERE {where}", args)
    if changed: bump()

def visible(row, me):
    tgt = row["target_ip"]
    return (not tgt) or tgt == me or row["owner_ip"] == me

def preview_kind(mime, name):
    m = mime or ""
    if m in IMAGE_MIMES: return "image"
    if m.startswith(VIDEO_PREFIX): return "video"
    if m.startswith(AUDIO_PREFIX): return "audio"
    if m == "application/pdf": return "pdf"
    if m in TEXT_PREVIEW or m.startswith("text/"): return "text"
    return ""

# --------------------------------------------------------------------------- auth
def current_user():
    """The logged-in account for this request, or an implicit admin when no accounts exist."""
    if not auth_enabled():
        return {"name": "", "role": "admin", "ver": 0, "perms": {k: True for k in PERM_KEYS}, "limits": {}, "expires": None}
    name = session.get("u"); role = session.get("r"); ver = session.get("v")
    if not name or role not in ROLES:
        return None
    rec = load_users().get(name)
    if rec is None:
        if PIN and name == "user" and ver == 0:     # legacy PIN session
            return {"name": "user", "role": "user", "ver": 0, "perms": dict(DEFAULT_PERMS), "limits": {}, "expires": None}
        return None
    if int(rec.get("ver", 1)) != ver:               # password changed -> session invalid
        return None
    exp = rec.get("expires")
    if exp and time.time() > float(exp):            # guest account past its expiry
        return None
    return {"name": name, "role": rec.get("role", "user"), "ver": ver,
            "perms": effective_perms(rec), "limits": dict(rec.get("limits") or {}), "expires": rec.get("expires")}

def is_admin():
    u = current_user()
    return bool(u) and u["role"] == "admin"

def _deny(code):
    if request.path.startswith("/api/"):
        return jsonify(error="locked" if code == 401 else "admin only"), code
    if code == 401:
        return redirect(url_for("login"))
    abort(403)

def require_auth(fn):
    @wraps(fn)
    def w(*a, **k):
        u = current_user()
        if u is None:
            return _deny(401)
        if not _net_ok(u):
            return (jsonify(error="your account is not allowed from the public internet"), 403) if request.path.startswith("/api/") else abort(403)
        return fn(*a, **k)
    return w

def require_admin(fn):
    @wraps(fn)
    def w(*a, **k):
        u = current_user()
        if u is None: return _deny(401)
        if u["role"] != "admin": return _deny(403)
        return fn(*a, **k)
    return w

def has_perm(name):
    u = current_user()
    if not u: return False
    if u["role"] == "admin": return True
    return bool(u.get("perms", {}).get(name, DEFAULT_PERMS.get(name, False)))

def _net_ok(u):
    """Enforce a 'private networks only' account limit: block it over the public internet."""
    if not u or u["role"] == "admin": return True
    if u.get("limits", {}).get("lan_only") and classify_ip(client_ip()) == "public":
        return False
    return True

def require_perm(perm):
    def deco(fn):
        @wraps(fn)
        def w(*a, **k):
            u = current_user()
            if u is None: return _deny(401)
            if not _net_ok(u):
                return (jsonify(error="your account is not allowed from the public internet"), 403) if request.path.startswith("/api/") else abort(403)
            if u["role"] != "admin" and not has_perm(perm):
                return (jsonify(error=f"your account is not allowed to {perm.replace('_',' ')}"), 403) if request.path.startswith("/api/") else abort(403)
            return fn(*a, **k)
        return w
    return deco

@app.before_request
def _csrf_guard():
    """State-changing requests must come from this site (SameSite cookies + Origin check)."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if origin:
            if urlparse(origin).netloc.lower() != request.host.lower():
                return (jsonify(error="cross-site request blocked"), 403) if request.path.startswith("/api/") else abort(403)

@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    # Lock powerful features off by default, but allow the ones an admin turned on in the visitor log,
    # otherwise this header silently blocks camera/mic/location even after the visitor grants permission.
    try:
        cfg = access_cfg(); exp = cfg.get("experimental", {}) if cfg.get("enabled") and cfg.get("client_details") else {}
    except Exception:
        exp = {}; cfg = {}
    def _pp(feature, on): return f"{feature}=(self)" if on else f"{feature}=()"
    pp = ", ".join([
        _pp("camera", exp.get("camera")),
        _pp("microphone", exp.get("mic")),
        _pp("geolocation", cfg.get("enabled") and cfg.get("client_details") and cfg.get("precise_location")),
        _pp("accelerometer", exp.get("motion")),
        _pp("gyroscope", exp.get("motion")),
        "payment=()",
    ])
    resp.headers.setdefault("Permissions-Policy", pp)
    if request.path.startswith("/api/") or request.path in ("/login", "/", "/admin", "/screen"):
        resp.headers["Cache-Control"] = "no-store"
    try: record_access(resp)
    except Exception: pass          # logging must never break a request
    return resp

# --------------------------------------------------------------------------- pages
@app.route("/")
@require_auth
def index():
    return send_file(INDEX_HTML)

@app.route("/login", methods=["GET","POST"])
def login():
    if not auth_enabled(): return redirect(url_for("index"))
    if current_user(): return redirect(url_for("index"))
    error = None
    ip = client_ip()
    if request.method == "POST":
        wait = login_locked(ip)
        if wait:
            error = f"Too many attempts. Try again in {wait} seconds."
        else:
            name = (request.form.get("username") or "").strip().lower()
            pw   = request.form.get("password") or ""
            acct = check_password(name, pw) if name and pw else None
            if acct and account_expired(acct["name"]):
                error = "This account has expired."
            elif acct:
                login_succeeded(ip)
                session.clear()
                session.permanent = True
                session["u"] = acct["name"]; session["r"] = acct["role"]; session["v"] = acct["ver"]
                session["t"] = time.time()
                with db() as conn:
                    who = touch_device(conn)
                    log_activity(conn, "join", f"{who} signed in as {acct['name']} ({acct['role']})", ip)
                bump()
                return redirect(url_for("index"))
            login_failed(ip)
            time.sleep(0.4)
            error = "Wrong username or password."
    users = load_users()
    return render_template("login.html", error=error,
                           has_admin="admin" in users, has_user=("user" in users) or bool(PIN))

@app.route("/api/password", methods=["POST"])
@require_auth
def api_password():
    """Change the password of the signed-in account (requires the current password)."""
    u = current_user()
    if not u["name"]:
        return jsonify(error="no accounts exist yet - create one with: python app.py set-password admin"), 400
    d = request.json or {}
    cur = d.get("current") or ""; new = d.get("new") or ""
    if not check_password(u["name"], cur):
        login_failed(client_ip())
        return jsonify(error="current password is wrong"), 403
    try:
        role = set_password(u["name"], new, u["role"])
    except ValueError as e:
        return jsonify(error=str(e)), 400
    rec = load_users()[u["name"]]
    session["v"] = int(rec["ver"]); session["r"] = role     # keep this session alive; all others are logged out
    with db() as conn:
        who = touch_device(conn)
        log_activity(conn, "join", f"{who} changed the {u['name']} password", client_ip())
    bump()
    return jsonify(ok=True)

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    resp = make_response(redirect(url_for("login") if auth_enabled() else url_for("index")))
    resp.delete_cookie("dz_screen", path="/")
    return resp

# --------------------------------------------------------------------------- state
@app.route("/api/state")
@require_auth
def api_state():
    me = client_ip(); now = time.time()
    user = current_user(); admin = user["role"] == "admin"
    with db() as conn:
        purge_expired(conn)
        touch_device(conn)

        see_all = admin or user.get("perms", {}).get("see_all", True)
        uname = user["name"]
        def _owns(r):
            try: ou = r["owner_user"]
            except (KeyError, IndexError): ou = None
            if ou: return ou == uname            # account-based ownership when known
            return r["owner_ip"] == me           # legacy items: fall back to device IP
        items = []
        for r in conn.execute("SELECT * FROM files ORDER BY pinned DESC, created DESC").fetchall():
            if not visible(r, me): continue
            if not see_all and not _owns(r): continue
            mime = r["mime"] or ""
            mine = r["owner_ip"] == me
            items.append({"kind":"file","id":r["id"],"name":r["name"],"size":r["size"],"mime":mime,
                          "is_image":mime in IMAGE_MIMES,"created":r["created"],
                          "pinned":bool(r["pinned"]), "label":r["label"] or "",
                          "preview":preview_kind(mime, r["name"]),
                          "target":r["target_ip"] or "", "mine_target":bool(r["target_ip"]) and r["target_ip"]==me,
                          "mine":mine, "can_delete":admin or (mine and user.get("perms",{}).get("delete",True)),
                          "share_url":url_for("share_page", token=r["token"], _external=False) if r["token"] else "",
                          "raw_url":url_for("file_raw", item_id=r["id"]),
                          "download_url":url_for("file_download", item_id=r["id"])})
        for r in conn.execute("SELECT * FROM notes ORDER BY pinned DESC, created DESC").fetchall():
            if not visible(r, me): continue
            if not see_all and not _owns(r): continue
            burn = bool(r["burn"]); mine = r["owner_ip"] == me
            items.append({"kind":"note","id":r["id"],
                          "content": None if burn else r["content"],
                          "burn":burn, "created":r["created"], "pinned":bool(r["pinned"]), "label":r["label"] or "",
                          "target":r["target_ip"] or "", "mine_target":bool(r["target_ip"]) and r["target_ip"]==me,
                          "mine":mine, "can_delete":admin or (mine and user.get("perms",{}).get("delete",True))})
        items.sort(key=lambda x: (not x["pinned"], -x["created"]))

        devices = []
        for r in conn.execute("SELECT * FROM devices WHERE last_seen > ? ORDER BY last_seen DESC",
                              (now - DEVICE_LIST_WINDOW,)).fetchall():
            devices.append({"id":r["ip"], "name":r["custom_name"] or r["name"], "ip":r["ip"],
                            "type":r["type"] or "laptop",
                            "status":"active" if (now - r["last_seen"]) < DEVICE_ACTIVE_WINDOW else "idle",
                            "you": r["ip"] == me})
        devices.sort(key=lambda d: (not d["you"], d["status"] != "active"))

        activity = [{"id":r["id"],"icon":r["icon"],"text":r["text"],"t":r["created"]}
                    for r in conn.execute("SELECT * FROM activity ORDER BY created DESC LIMIT 60").fetchall()]

        chat = [{"id":r["id"],"dev":r["dev"],"text":r["text"],"t":r["created"],"mine":r["ip"]==me}
                for r in conn.execute("SELECT * FROM messages ORDER BY created ASC LIMIT 200").fetchall()]

        stats = None
        if admin:   # storage figures and the largest-files list are admin-only
            used = conn.execute("SELECT COALESCE(SUM(size),0) s FROM files").fetchone()["s"]
            largest = [{"id":r["id"],"name":r["name"],"size":r["size"]}
                       for r in conn.execute("SELECT id,name,size FROM files ORDER BY size DESC LIMIT 10").fetchall()]
            du = shutil.disk_usage(DATA_DIR)
            stats = {"used":used, "free":du.free, "total":du.total, "largest":largest}
    _prune_uploads()
    with _uploads_lock:
        incoming = [{"id": k, "name": v["name"], "who": v["who"], "pct": v["pct"], "count": v["count"]}
                    for k, v in _uploads.items()
                    if v["owner_ip"] != me and (not v["target_ip"] or v["target_ip"] == me)]
    incoming.sort(key=lambda x: x["name"])
    return jsonify(items=items, devices=devices, activity=activity, chat=chat, incoming=incoming,
                   me=me, remote=is_remote(me), stats=stats,
                   user=user["name"], role=user["role"], admin=admin, auth=auth_enabled(),
                   perms=user.get("perms", {}), limits=user.get("limits", {}),
                   ask_location=bool(access_cfg().get("enabled") and access_cfg().get("client_details") and access_cfg().get("precise_location")),
                   experimental=(access_cfg().get("experimental", {}) if (access_cfg().get("enabled") and access_cfg().get("client_details")) else {}))

# --------------------------------------------------------------------------- SSE push
@app.route("/api/events")
@require_auth
def api_events():
    def gen():
        last = -1
        while True:
            with _change:
                if _version["n"] == last:
                    _change.wait(timeout=25)
                cur = _version["n"]
            if cur != last:
                last = cur
                yield f"data: {cur}\n\n"
            else:
                yield ": keepalive\n\n"
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache", "X-Accel-Buffering":"no"})

# --------------------------------------------------------------------------- create
def _store_file(conn, filename, stream_or_bytes, mime, owner, target, owner_user=""):
    ext = os.path.splitext(filename)[1][:12]
    stored = uuid.uuid4().hex + ext
    path = os.path.join(FILES_DIR, stored)
    if isinstance(stream_or_bytes, bytes):
        open(path, "wb").write(stream_or_bytes)
    else:
        stream_or_bytes.save(path)
    size = os.path.getsize(path)
    mime = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    conn.execute("""INSERT INTO files (id,name,stored,size,mime,created,pinned,token,owner_ip,target_ip,expires,owner_user)
                    VALUES (?,?,?,?,?,?,0,?,?,?,NULL,?)""",
                 (uuid.uuid4().hex, filename, stored, size, mime, time.time(),
                  uuid.uuid4().hex[:16], owner, target or None, owner_user or ""))
    return size

@app.route("/api/upload-progress", methods=["POST"])
@require_perm("upload")
def api_upload_progress():
    d = request.json or {}
    uid = (d.get("id") or "")[:40]
    if not uid: return jsonify(ok=False), 400
    try: pct = max(0, min(100, int(d.get("pct") or 0)))
    except (TypeError, ValueError): pct = 0
    ip = client_ip()
    with db() as conn:
        who = touch_device(conn)
    with _uploads_lock:
        prev = _uploads.get(uid, {})
        _uploads[uid] = {"name": (d.get("name") or "file")[:120], "who": who, "owner_ip": ip,
                         "target_ip": (d.get("target") or "") or None, "pct": pct,
                         "count": int(d.get("count") or 1), "ts": time.time()}
        changed = prev.get("pct") != pct
    if pct >= 100:
        with _uploads_lock: _uploads.pop(uid, None)
    if changed: bump()
    return jsonify(ok=True)

@app.route("/api/upload", methods=["POST"])
@require_perm("upload")
def api_upload():
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="No files received."), 400
    ip = client_ip(); u = current_user(); uname = u["name"]; lim = u.get("limits", {})
    cap_mb = lim.get("max_upload_mb") or 0
    quota_mb = lim.get("quota_mb") or 0
    max_items = lim.get("max_items") or 0
    target = (request.form.get("target") or "").strip()
    saved = 0
    with db() as conn:
        who = touch_device(conn)
        tgt_name = display_name(conn, target) if target else ""
        if uname and (quota_mb or max_items):
            row = conn.execute("SELECT COALESCE(SUM(size),0) s, COUNT(*) c FROM files WHERE owner_user=?", (uname,)).fetchone()
            used, cnt = row["s"], row["c"]
        else:
            used = cnt = 0
        for f in files:
            if not f or not f.filename: continue
            f.stream.seek(0, 2); fsize = f.stream.tell(); f.stream.seek(0)
            if cap_mb and fsize > cap_mb * 1024 * 1024:
                return jsonify(error=f"{f.filename} is over your {cap_mb} MB per-file limit", saved=saved), 413
            if max_items and cnt + 1 > max_items:
                return jsonify(error=f"you have reached your {max_items}-item limit", saved=saved), 413
            if quota_mb and used + fsize > quota_mb * 1024 * 1024:
                return jsonify(error=f"this would exceed your {quota_mb} MB storage quota", saved=saved), 413
            _store_file(conn, f.filename, f, f.mimetype, ip, target, uname)
            used += fsize; cnt += 1
            label = f"{who} shared {f.filename}" + (f" → {tgt_name}" if target else "")
            log_activity(conn, "up", label, ip)
            saved += 1
    if saved: bump()
    return jsonify(saved=saved)

def _chunk_name(n):
    n = (n or "file").strip().replace("\\", "/").split("/")[-1]
    if not n or n in (".", ".."): n = "file"
    return n[:200]

@app.route("/api/upload-chunk", methods=["GET", "POST"])
@require_perm("upload")
def api_upload_chunk():
    """Resumable upload: the client sends a big file in pieces and can resume after a drop.
    GET  ?id=  -> how many bytes we already have (so the client knows where to continue).
    POST ?id=&name=&size=&offset=&target=  with the raw chunk bytes as the body."""
    uid = re.sub(r"[^a-z0-9]", "", (request.args.get("id") or "").lower())[:48]
    if not uid: return jsonify(error="bad id"), 400
    part = os.path.join(CHUNK_DIR, uid + ".part")
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if request.method == "GET":
        return jsonify(received=have)
    try:
        total = int(request.args.get("size") or 0); offset = int(request.args.get("offset") or 0)
    except ValueError:
        return jsonify(error="bad size/offset"), 400
    name = _chunk_name(request.args.get("name"))
    u = current_user(); uname = u["name"]; lim = u.get("limits", {})
    if offset == 0:
        cap = (lim.get("max_upload_mb") or 0) * 1024 * 1024
        if cap and total > cap:
            return jsonify(error=f"over your {lim['max_upload_mb']} MB per-file limit"), 413
        if uname and (lim.get("quota_mb") or lim.get("max_items")):
            with db() as conn:
                row = conn.execute("SELECT COALESCE(SUM(size),0) s, COUNT(*) c FROM files WHERE owner_user=?", (uname,)).fetchone()
            if lim.get("max_items") and row["c"] + 1 > lim["max_items"]:
                return jsonify(error=f"you have reached your {lim['max_items']}-item limit"), 413
            if lim.get("quota_mb") and row["s"] + total > lim["quota_mb"] * 1024 * 1024:
                return jsonify(error=f"this would exceed your {lim['quota_mb']} MB storage quota"), 413
        open(part, "wb").close(); have = 0          # start fresh
    if offset != have:
        return jsonify(error="offset mismatch", received=have), 409   # tell the client where to resume
    data = request.get_data(cache=False, parse_form_data=False)
    with open(part, "ab") as f:
        f.write(data)
    have = os.path.getsize(part)
    if total and have >= total:
        ip = client_ip(); target = (request.args.get("target") or "").strip()
        stored = uuid.uuid4().hex + os.path.splitext(name)[1][:12]
        os.replace(part, os.path.join(FILES_DIR, stored))
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with db() as conn:
            who = touch_device(conn)
            conn.execute("""INSERT INTO files (id,name,stored,size,mime,created,pinned,token,owner_ip,target_ip,expires,owner_user)
                            VALUES (?,?,?,?,?,?,0,?,?,?,NULL,?)""",
                         (uuid.uuid4().hex, name, stored, have, mime, time.time(),
                          uuid.uuid4().hex[:16], ip, target or None, uname or ""))
            label = f"{who} shared {name}" + (f" → {display_name(conn, target)}" if target else "")
            log_activity(conn, "up", label, ip)
        bump()
        return jsonify(done=True, received=have)
    return jsonify(received=have)

@app.route("/api/note", methods=["POST"])
@require_perm("notes")
def api_note():
    d = request.json if request.is_json else request.form
    content = (d.get("content") or "").strip()
    if not content: return jsonify(error="Nothing to save."), 400
    burn = bool(d.get("burn"))
    hours = float(d.get("expire_hours") or 0)
    target = (d.get("target") or "").strip()
    ip = client_ip()
    with db() as conn:
        who = touch_device(conn)
        conn.execute("""INSERT INTO notes (id,content,created,pinned,owner_ip,target_ip,expires,burn,owner_user)
                        VALUES (?,?,?,0,?,?,?,?,?)""",
                     (uuid.uuid4().hex, content, time.time(), ip, target or None,
                      (time.time()+hours*3600) if hours>0 else None, 1 if burn else 0, current_user()["name"]))
        label = f"{who} shared " + ("a secret note" if burn else "text")
        if target: label += f" → {display_name(conn, target)}"
        log_activity(conn, "link", label, ip)
    bump()
    return jsonify(ok=True)

@app.route("/api/fetch-url", methods=["POST"])
@require_perm("fetch_url")   # the server fetches arbitrary URLs from inside your network
def api_fetch_url():
    d = request.json if request.is_json else request.form
    url = (d.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return jsonify(error="Not a valid http(s) URL."), 400
    ip = client_ip()
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Dropzone/6"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            mime = resp.headers.get_content_type()
            cd = resp.headers.get("Content-Disposition") or ""
            m = re.search(r'filename="?([^\";]+)"?', cd)
            name = m.group(1) if m else (os.path.basename(url.split("?")[0]) or "download")
            if "." not in name:
                guess = mimetypes.guess_extension(mime or "") or ""
                name += guess or ".bin"
            data = resp.read(URL_FETCH_CAP + 1)
            if len(data) > URL_FETCH_CAP:
                return jsonify(error="File larger than the upload limit."), 413
    except Exception as e:
        return jsonify(error=f"Fetch failed: {e.__class__.__name__}"), 502
    with db() as conn:
        who = touch_device(conn)
        _store_file(conn, name, data, mime, ip, None, current_user()["name"])
        log_activity(conn, "down", f"{who} pulled {name} from the web", ip)
    bump()
    return jsonify(ok=True, name=name)

@app.route("/api/chat", methods=["POST"])
@require_perm("chat")
def api_chat():
    d = request.json if request.is_json else request.form
    text = (d.get("text") or "").strip()
    if not text: return jsonify(error="empty"), 400
    ip = client_ip()
    with db() as conn:
        who = touch_device(conn)
        conn.execute("INSERT INTO messages (id,ip,dev,text,created) VALUES (?,?,?,?,?)",
                     (uuid.uuid4().hex, ip, who, text, time.time()))
        log_activity(conn, "chat", f"{who}: {text}", ip)
    bump()
    return jsonify(ok=True)

# --------------------------------------------------------------------------- item ops
@app.route("/api/pin/<kind>/<item_id>", methods=["POST"])
@require_auth
def api_pin(kind, item_id):
    table = "files" if kind == "file" else "notes" if kind == "note" else None
    if not table: return jsonify(error="unknown kind"), 400
    with db() as conn:
        touch_device(conn)
        conn.execute(f"UPDATE {table} SET pinned = 1 - pinned WHERE id=?", (item_id,))
    bump()
    return jsonify(ok=True)

@app.route("/api/reveal/<item_id>", methods=["POST"])
@require_auth
def api_reveal(item_id):
    ip = client_ip()
    with db() as conn:
        who = touch_device(conn)
        r = conn.execute("SELECT content FROM notes WHERE id=? AND burn=1", (item_id,)).fetchone()
        if not r: return jsonify(error="gone"), 404
        conn.execute("DELETE FROM notes WHERE id=?", (item_id,))
        log_activity(conn, "trash", f"{who} read a secret note (deleted)", ip)
        content = r["content"]
    bump()
    return jsonify(content=content)

def _may_delete(row, me):
    """Admins delete anything; users only what they shared themselves (by device IP or account)."""
    if is_admin() or row is None: return is_admin()
    if not has_perm("delete"): return False
    uname = (current_user() or {}).get("name")
    try: own_user = row["owner_user"]
    except (KeyError, IndexError): own_user = None
    return row["owner_ip"] == me or (uname and own_user == uname)

@app.route("/api/item/<kind>/<item_id>", methods=["DELETE"])
@require_auth
def api_delete(kind, item_id):
    ip = client_ip()
    with db() as conn:
        who = touch_device(conn)
        if kind == "file":
            row = conn.execute("SELECT name, stored, owner_ip FROM files WHERE id=?", (item_id,)).fetchone()
            if row:
                if not _may_delete(row, ip): return jsonify(error="you can only delete your own items"), 403
                try: os.remove(os.path.join(FILES_DIR, row["stored"]))
                except OSError: pass
                conn.execute("DELETE FROM files WHERE id=?", (item_id,))
                log_activity(conn, "trash", f"{who} removed {row['name']}", ip)
        elif kind == "note":
            row = conn.execute("SELECT owner_ip FROM notes WHERE id=?", (item_id,)).fetchone()
            if row:
                if not _may_delete(row, ip): return jsonify(error="you can only delete your own items"), 403
                conn.execute("DELETE FROM notes WHERE id=?", (item_id,))
                log_activity(conn, "trash", f"{who} removed a text snippet", ip)
        else:
            return jsonify(error="unknown kind"), 400
    bump()
    return jsonify(ok=True)

@app.route("/api/bulk-delete", methods=["POST"])
@require_auth
def api_bulk_delete():
    d = request.json or {}
    ids = d.get("ids") or []          # [{kind, id}, ...]
    ip = client_ip(); n = 0; skipped = 0
    with db() as conn:
        who = touch_device(conn)
        for it in ids:
            if it.get("kind") == "file":
                row = conn.execute("SELECT stored, owner_ip FROM files WHERE id=?", (it.get("id"),)).fetchone()
                if row:
                    if not _may_delete(row, ip): skipped += 1; continue
                    try: os.remove(os.path.join(FILES_DIR, row["stored"]))
                    except OSError: pass
                    conn.execute("DELETE FROM files WHERE id=?", (it.get("id"),)); n += 1
            elif it.get("kind") == "note":
                row = conn.execute("SELECT owner_ip FROM notes WHERE id=?", (it.get("id"),)).fetchone()
                if row:
                    if not _may_delete(row, ip): skipped += 1; continue
                    n += conn.execute("DELETE FROM notes WHERE id=?", (it.get("id"),)).rowcount
        if n: log_activity(conn, "trash", f"{who} removed {n} items", ip)
    if n: bump()
    return jsonify(deleted=n, skipped=skipped)

@app.route("/api/zip")
@require_perm("download")
def api_zip():
    ids = [x for x in (request.args.get("ids") or "").split(",") if x]
    if not ids: abort(400)
    me = client_ip()
    tmp = tempfile.SpooledTemporaryFile(max_size=64*1024*1024)
    with db() as conn, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        seen = set()
        for fid in ids:
            r = conn.execute("SELECT name, stored, owner_ip, target_ip FROM files WHERE id=?", (fid,)).fetchone()
            if not r or not visible(r, me): continue
            name = r["name"]; i = 1
            while name in seen:
                base, ext = os.path.splitext(r["name"]); name = f"{base}-{i}{ext}"; i += 1
            seen.add(name)
            z.write(os.path.join(FILES_DIR, r["stored"]), arcname=name)
    tmp.seek(0)
    return send_file(tmp, mimetype="application/zip", as_attachment=True,
                     download_name="dropzone-%s.zip" % time.strftime("%Y%m%d-%H%M"))

@app.route("/api/device/name", methods=["POST"])
@require_auth
def api_device_name():
    d = request.json or {}
    ip = (d.get("ip") or "").strip()
    name = (d.get("name") or "").strip()[:40]
    if not ip: return jsonify(error="no ip"), 400
    me = client_ip()
    if ip != me and not is_admin():
        return jsonify(error="you can only rename your own device"), 403
    with db() as conn:
        who = touch_device(conn)
        old = display_name(conn, ip)
        conn.execute("UPDATE devices SET custom_name=? WHERE ip=?", (name or None, ip))
        if name and name != old:
            log_activity(conn, "join", f"{old} is now “{name}”", me)
    bump()
    return jsonify(ok=True)

@app.route("/api/preview-text/<item_id>")
@require_perm("download")
def api_preview_text(item_id):
    me = client_ip()
    with db() as conn:
        r = conn.execute("SELECT stored, mime, size, owner_ip, target_ip FROM files WHERE id=?", (item_id,)).fetchone()
    if not r or not visible(r, me): abort(404)
    if (r["size"] or 0) > 512*1024: return jsonify(text="(file too large to preview — download instead)")
    try:
        txt = open(os.path.join(FILES_DIR, r["stored"]), "r", encoding="utf-8", errors="replace").read(200000)
    except OSError:
        abort(404)
    return jsonify(text=txt)

# --------------------------------------------------------------------------- v5: labels / cleanup / screen
@app.route("/api/label", methods=["POST"])
@require_auth
def api_label():
    d = request.json or {}
    ids = d.get("ids") or []
    label = (d.get("label") or "").strip()[:24]
    n = 0
    with db() as conn:
        touch_device(conn)
        for it in ids:
            table = "files" if it.get("kind") == "file" else "notes" if it.get("kind") == "note" else None
            if table:
                n += conn.execute(f"UPDATE {table} SET label=? WHERE id=?", (label or None, it.get("id"))).rowcount
    if n: bump()
    return jsonify(labeled=n)

@app.route("/api/cleanup", methods=["POST"])
@require_admin
def api_cleanup():
    mode = (request.json or {}).get("mode", "")
    ip = client_ip(); n = 0
    with db() as conn:
        who = touch_device(conn)
        if mode == "older30":
            cutoff = time.time() - 30*86400
            rows = conn.execute("SELECT id, stored FROM files WHERE created < ? AND pinned=0", (cutoff,)).fetchall()
            for r in rows:
                try: os.remove(os.path.join(FILES_DIR, r["stored"]))
                except OSError: pass
            conn.execute("DELETE FROM files WHERE created < ? AND pinned=0", (cutoff,))
            n = len(rows) + conn.execute("DELETE FROM notes WHERE created < ? AND pinned=0", (cutoff,)).rowcount
        elif mode == "unpinned":
            rows = conn.execute("SELECT id, stored FROM files WHERE pinned=0").fetchall()
            for r in rows:
                try: os.remove(os.path.join(FILES_DIR, r["stored"]))
                except OSError: pass
            conn.execute("DELETE FROM files WHERE pinned=0")
            n = len(rows) + conn.execute("DELETE FROM notes WHERE pinned=0").rowcount
        else:
            return jsonify(error="unknown mode"), 400
        if n: log_activity(conn, "trash", f"{who} cleaned up {n} items", ip)
    if n: bump()
    return jsonify(deleted=n)

# --------------------------------------------------------------------------- v6: admin panel
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
_settings_lock = threading.Lock()

def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    if not isinstance(s, dict): s = {}
    if not s.get("library_roots"):
        home = os.path.expanduser("~")
        s["library_roots"] = [{"name": "Home", "path": home}] if os.path.isdir(home) else []
    al = s.setdefault("access_log", {})
    al.setdefault("enabled", False); al.setdefault("geo", True); al.setdefault("client_details", True)
    al.setdefault("retention_days", 90)
    al.setdefault("precise_location", False)     # ask the browser for GPS position; the visitor sees a permission prompt and must allow it
    exp = al.setdefault("experimental", {})
    for k in ['webrtc_ip', 'battery', 'gpu', 'fingerprint', 'device_counts', 'notifications', 'motion', 'camera', 'mic', 'clipboard']:
        exp.setdefault(k, False)
    return s

def save_settings(s):
    with _settings_lock:
        _write_private(SETTINGS_PATH, json.dumps(s, indent=1).encode("utf-8"))

def storage_overview(conn):
    du = shutil.disk_usage(DATA_DIR)
    files = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM files").fetchone()
    notes = conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
    largest = [{"id":r["id"],"name":r["name"],"size":r["size"],"created":r["created"]}
               for r in conn.execute("SELECT id,name,size,created FROM files ORDER BY size DESC LIMIT 15").fetchall()]
    return {"disk_total": du.total, "disk_free": du.free, "disk_used": du.total - du.free,
            "dropzone_used": files["s"], "file_count": files["c"], "note_count": notes,
            "data_dir": DATA_DIR, "largest": largest}

@app.route("/admin")
@require_admin
def admin_page():
    return send_file(os.path.join(HERE, "templates", "admin.html"))

@app.route("/api/admin/overview")
@require_admin
def api_admin_overview():
    users = [{"name": n, "role": u.get("role", "user"), "updated": u.get("updated", 0)}
             for n, u in sorted(load_users().items())]
    if PIN and "user" not in load_users():
        users.append({"name": "user", "role": "user", "updated": 0, "legacy_pin": True})
    with db() as conn:
        storage = storage_overview(conn)
    rich = []
    for row in users:
        rec = load_users().get(row["name"], {})
        row = dict(row)
        row["perms"] = effective_perms(rec)
        row["limits"] = dict(rec.get("limits") or {})
        row["expires"] = rec.get("expires")
        rich.append(row)
    return jsonify(me=current_user()["name"], users=rich, storage=storage,
                   roots=load_settings()["library_roots"], perm_keys=list(PERM_KEYS), limit_keys=list(LIMIT_KEYS))

@app.route("/api/admin/users", methods=["POST"])
@require_admin
def api_admin_user_set():
    d = request.json or {}
    name = (d.get("name") or "").strip().lower(); role = d.get("role") or None; pw = d.get("password") or ""
    perms = d.get("perms"); limits = d.get("limits")
    expires = d.get("expires", "KEEP")
    exp_val = None if expires in (None, "", 0) else (float(expires) if expires != "KEEP" else "KEEP")
    users = load_users()
    try:
        if name == current_user()["name"] and role and role != "admin":
            return jsonify(error="you can't demote yourself"), 400
        if pw:
            role = set_password(name, pw, role)
        elif name not in users:
            return jsonify(error="a password is required for a new account"), 400
        # apply role/perms/limits/expiry on top (works for new and existing accounts)
        update_account(name, role=role if role in ROLES else None,
                       perms=perms if isinstance(perms, dict) else None,
                       limits=limits if isinstance(limits, dict) else None,
                       expires=(_UNSET if exp_val == "KEEP" else exp_val))
        role = load_users()[name].get("role")
    except ValueError as e:
        return jsonify(error=str(e)), 400
    with db() as conn:
        log_activity(conn, "join", f"{current_user()['name']} updated account '{name}' ({role})", client_ip())
    bump()
    return jsonify(ok=True, role=role)

@app.route("/api/admin/users/<name>", methods=["DELETE"])
@require_admin
def api_admin_user_delete(name):
    name = name.strip().lower()
    users = load_users()
    if name == current_user()["name"]:
        return jsonify(error="you can't delete the account you're signed in with"), 400
    admins = [n for n, u in users.items() if u.get("role") == "admin"]
    if name in admins and len(admins) == 1:
        return jsonify(error="that is the only admin account"), 400
    if not remove_user(name):
        return jsonify(error="no such account"), 404
    with db() as conn:
        log_activity(conn, "trash", f"{current_user()['name']} removed account '{name}'", client_ip())
    bump()
    return jsonify(ok=True)

@app.route("/api/admin/roots", methods=["POST"])
@require_admin
def api_admin_root_add():
    d = request.json or {}
    name = (d.get("name") or "").strip()[:40]; path = os.path.expanduser((d.get("path") or "").strip())
    if not name or not path: return jsonify(error="name and path are required"), 400
    path = os.path.realpath(path)
    if not os.path.isdir(path): return jsonify(error="that folder doesn't exist on the server"), 400
    s = load_settings()
    if any(r["name"].lower() == name.lower() for r in s["library_roots"]):
        return jsonify(error="a library with that name already exists"), 400
    s["library_roots"].append({"name": name, "path": path}); save_settings(s)
    return jsonify(ok=True, roots=s["library_roots"])

@app.route("/api/admin/roots/<name>", methods=["DELETE"])
@require_admin
def api_admin_root_remove(name):
    s = load_settings()
    before = len(s["library_roots"])
    s["library_roots"] = [r for r in s["library_roots"] if r["name"] != name]
    if len(s["library_roots"]) == before: return jsonify(error="no such library"), 404
    save_settings(s)
    return jsonify(ok=True, roots=s["library_roots"])

# ---- admin file manager over the library roots ----
def _fm_root(name):
    for r in load_settings()["library_roots"]:
        if r["name"] == name: return os.path.realpath(r["path"])
    abort(404)

def _fm_resolve(root_name, rel, must_exist=True):
    """Map (root, relative path) to an absolute path that is guaranteed to stay inside the root."""
    base = _fm_root(root_name)
    rel = (rel or "").replace("\\", "/").strip("/")
    target = os.path.realpath(os.path.join(base, rel)) if rel else base
    if target != base and not target.startswith(base + os.sep):
        abort(403)
    if must_exist and not os.path.lexists(target):
        abort(404)
    return base, target

def _safe_name(name):
    name = (name or "").strip().replace("\\", "/").split("/")[-1]
    if not name or name in (".", "..") or "\0" in name: abort(400)
    return name

@app.route("/api/fm/list")
@require_admin
def api_fm_list():
    base, target = _fm_resolve(request.args.get("root"), request.args.get("path"))
    if not os.path.isdir(target): abort(400)
    entries = []
    try:
        with os.scandir(target) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False); is_dir = e.is_dir()
                    entries.append({"name": e.name, "dir": is_dir, "link": e.is_symlink(),
                                    "size": 0 if is_dir else st.st_size, "mtime": st.st_mtime,
                                    "mime": "" if is_dir else (mimetypes.guess_type(e.name)[0] or "")})
                except OSError:
                    continue
    except PermissionError:
        return jsonify(error="permission denied"), 403
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    rel = os.path.relpath(target, base).replace(os.sep, "/")
    return jsonify(root=request.args.get("root"), path="" if rel == "." else rel, entries=entries)

@app.route("/api/fm/download")
@require_admin
def api_fm_download():
    base, target = _fm_resolve(request.args.get("root"), request.args.get("path"))
    if os.path.isdir(target):
        tmp = tempfile.SpooledTemporaryFile(max_size=64*1024*1024)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for dp, dns, fns in os.walk(target):
                for fn in fns:
                    full = os.path.join(dp, fn)
                    try: z.write(full, arcname=os.path.relpath(full, target))
                    except OSError: pass
        tmp.seek(0)
        return send_file(tmp, mimetype="application/zip", as_attachment=True,
                         download_name=(os.path.basename(target) or "folder") + ".zip")
    return send_file(target, as_attachment=True, download_name=os.path.basename(target))

@app.route("/api/fm/upload", methods=["POST"])
@require_admin
def api_fm_upload():
    base, target = _fm_resolve(request.form.get("root"), request.form.get("path"))
    if not os.path.isdir(target): abort(400)
    n = 0
    for f in request.files.getlist("files"):
        if not f or not f.filename: continue
        f.save(os.path.join(target, _safe_name(f.filename))); n += 1
    return jsonify(saved=n)

@app.route("/api/fm/mkdir", methods=["POST"])
@require_admin
def api_fm_mkdir():
    d = request.json or {}
    base, target = _fm_resolve(d.get("root"), d.get("path"))
    new = os.path.join(target, _safe_name(d.get("name")))
    if os.path.lexists(new): return jsonify(error="already exists"), 400
    os.makedirs(new)
    return jsonify(ok=True)

@app.route("/api/fm/rename", methods=["POST"])
@require_admin
def api_fm_rename():
    d = request.json or {}
    base, src = _fm_resolve(d.get("root"), d.get("path"))
    if src == base: abort(400)
    dst = os.path.join(os.path.dirname(src), _safe_name(d.get("name")))
    if os.path.lexists(dst): return jsonify(error="a file with that name already exists"), 400
    os.rename(src, dst)
    return jsonify(ok=True)

@app.route("/api/fm/move", methods=["POST"])
@require_admin
def api_fm_move():
    d = request.json or {}
    base, dest = _fm_resolve(d.get("root"), d.get("dest"))
    if not os.path.isdir(dest): return jsonify(error="destination is not a folder"), 400
    n = 0
    for p in d.get("paths") or []:
        _, src = _fm_resolve(d.get("root"), p)
        if src == base or dest == src or dest.startswith(src + os.sep): continue
        dst = os.path.join(dest, os.path.basename(src))
        if os.path.lexists(dst): continue
        shutil.move(src, dst); n += 1
    return jsonify(moved=n)

@app.route("/api/fm/delete", methods=["POST"])
@require_admin
def api_fm_delete():
    d = request.json or {}
    n = 0
    for p in d.get("paths") or []:
        base, target = _fm_resolve(d.get("root"), p)
        if target == base: continue
        try:
            if os.path.isdir(target) and not os.path.islink(target): shutil.rmtree(target)
            else: os.remove(target)
            n += 1
        except OSError:
            pass
    with db() as conn:
        log_activity(conn, "trash", f"{current_user()['name']} deleted {n} item(s) in library '{d.get('root')}'", client_ip())
    bump()
    return jsonify(deleted=n)

@app.route("/api/fm/share", methods=["POST"])
@require_admin
def api_fm_share():
    """Copy a file from a library folder into the shared feed so everyone can see it."""
    d = request.json or {}
    base, src = _fm_resolve(d.get("root"), d.get("path"))
    if not os.path.isfile(src): return jsonify(error="only files can be shared"), 400
    if os.path.getsize(src) > MAX_MB * 1024 * 1024: return jsonify(error="file larger than the upload limit"), 413
    ip = client_ip(); name = os.path.basename(src)
    with db() as conn:
        who = touch_device(conn)
        with open(src, "rb") as f:
            _store_file(conn, name, f.read(), mimetypes.guess_type(name)[0], ip, None, current_user()["name"])
        log_activity(conn, "up", f"{who} shared {name} from library '{d.get('root')}'", ip)
    bump()
    return jsonify(ok=True)

# --------------------------------------------------------------------------- visitor / access log
# Opt-in (admin panel -> Visitors). Records who reached the site, from where, with what device and what they did.
GEO_URL = "http://ip-api.com/json/{ip}?fields=status,query,country,regionName,city,zip,lat,lon,timezone,isp,org,as,proxy,hosting,mobile"
_geo_lock = threading.Lock(); _geo_inflight = set()
NOISY_PATHS = ("/api/events", "/api/state", "/api/screen-status", "/api/admin/access", "/api/telemetry", "/qr.svg", "/static/")
LOGGED_PREFIXES = ("/", "/login", "/logout", "/admin", "/screen", "/file/", "/s/", "/api/upload", "/api/note", "/api/chat",
                   "/api/item/", "/api/bulk-delete", "/api/cleanup", "/api/fm/", "/api/admin/", "/api/password",
                   "/api/fetch-url", "/api/reveal/", "/api/zip", "/api/device/name", "/api/label", "/api/pin/")

def access_cfg():
    return load_settings()["access_log"]

def classify_ip(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return "unknown"
    if a.is_loopback: return "local"
    if a in ipaddress.ip_network("100.64.0.0/10"): return "tailnet"
    if a.is_private or a.is_link_local: return "lan"
    return "public"

def parse_ua(ua):
    u = ua or ""
    if re.search(r"bot|crawl|spider|curl|wget|python-requests|httpclient|scanner", u, re.I): device = "bot"
    elif re.search(r"iPad|Tablet", u) or ("Android" in u and "Mobile" not in u): device = "tablet"
    elif re.search(r"Mobi|iPhone|iPod|Android", u): device = "phone"
    elif u: device = "desktop"
    else: device = "unknown"
    m = re.search(r"Windows NT ([\d.]+)", u)
    if m: osn = {"10.0": "Windows 10/11", "6.3": "Windows 8.1", "6.1": "Windows 7"}.get(m.group(1), "Windows " + m.group(1))
    elif re.search(r"iPhone|iPad|iPod", u):
        m = re.search(r"OS (\d+)[_.](\d+)", u); osn = "iOS " + (f"{m.group(1)}.{m.group(2)}" if m else "")
    elif "Android" in u:
        m = re.search(r"Android ([\d.]+)", u); osn = "Android " + (m.group(1) if m else "")
    elif "CrOS" in u: osn = "ChromeOS"
    elif "Mac OS X" in u:
        m = re.search(r"Mac OS X (\d+)[_.](\d+)", u); osn = "macOS " + (f"{m.group(1)}.{m.group(2)}" if m else "")
    elif "Linux" in u: osn = "Linux"
    else: osn = ""
    if "Edg/" in u: b = "Edge " + re.search(r"Edg/([\d.]+)", u).group(1).split(".")[0]
    elif "OPR/" in u: b = "Opera " + re.search(r"OPR/([\d.]+)", u).group(1).split(".")[0]
    elif "SamsungBrowser/" in u: b = "Samsung Internet"
    elif "Firefox/" in u: b = "Firefox " + re.search(r"Firefox/([\d.]+)", u).group(1).split(".")[0]
    elif "Chrome/" in u or "CriOS/" in u:
        m = re.search(r"(?:Chrome|CriOS)/([\d.]+)", u); b = "Chrome " + m.group(1).split(".")[0]
    elif "Safari/" in u and "Version/" in u: b = "Safari " + re.search(r"Version/([\d.]+)", u).group(1).split(".")[0]
    elif u: b = u.split("/")[0][:30]
    else: b = ""
    return b.strip(), osn.strip(), device

def _geo_fetch(ip):
    try:
        req = urllib.request.Request(GEO_URL.format(ip=ip), headers={"User-Agent": "Dropzone/6"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        if data.get("status") != "success": data = {"status": "fail"}
    except Exception as e:
        data = {"status": "error", "error": e.__class__.__name__}
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO geo_cache (ip, json, ts) VALUES (?,?,?)", (ip, json.dumps(data), time.time()))
    with _geo_lock: _geo_inflight.discard(ip)

def geo_lookup_async(ip):
    """Look a public IP up once (ip-api.com, free tier) in the background; results are cached for 30 days."""
    if classify_ip(ip) != "public": return
    with db() as conn:
        row = conn.execute("SELECT ts FROM geo_cache WHERE ip=?", (ip,)).fetchone()
    if row and time.time() - row["ts"] < 30 * 86400: return
    with _geo_lock:
        if ip in _geo_inflight: return
        _geo_inflight.add(ip)
    threading.Thread(target=_geo_fetch, args=(ip,), daemon=True).start()

def _should_log(path, status):
    if path.startswith(NOISY_PATHS) and path != "/": return False
    if status in (401, 403): return True
    return path == "/" or any(path.startswith(pfx) for pfx in LOGGED_PREFIXES if pfx != "/")

def record_access(resp):
    if request.method in ("HEAD", "OPTIONS"): return      # health checks / preflights are noise
    cfg = access_cfg()
    if not cfg.get("enabled"): return
    path = request.path; status = resp.status_code
    if not _should_log(path, status): return
    ip = client_ip(); u = current_user() or {}
    ua = request.headers.get("User-Agent", "")[:400]
    browser, osn, device = parse_ua(ua)
    detail = ""
    if path == "/login" and request.method == "POST":
        detail = f"login {'ok' if status == 302 else 'FAILED'} as '{(request.form.get('username') or '')[:32]}'"
    elif path.startswith("/file/") or path.startswith("/s/"):
        detail = "download" if path.endswith("/download") else "view"
    with db() as conn:
        conn.execute("""INSERT INTO access_log (id,ts,ip,via,user,role,method,path,status,detail,ua,browser,os,device,referer,lang,client)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                     (uuid.uuid4().hex, time.time(), ip, classify_ip(ip), u.get("name") or "", u.get("role") or "",
                      request.method, path[:200], status, detail, ua, browser, osn, device,
                      (request.headers.get("Referer") or "")[:200], (request.headers.get("Accept-Language") or "")[:80]))
        days = float(cfg.get("retention_days") or 0)
        if days > 0:
            conn.execute("DELETE FROM access_log WHERE ts < ?", (time.time() - days * 86400,))
    if cfg.get("geo"): geo_lookup_async(ip)

@app.route("/api/telemetry", methods=["POST"])
@require_auth
def api_telemetry():
    """Browser-side details (screen, timezone, platform...) sent once per page load when client details are enabled."""
    cfg = access_cfg()
    if not (cfg.get("enabled") and cfg.get("client_details")): return jsonify(ok=False)
    d = request.json or {}
    keep = {k: (str(v)[:120] if not isinstance(v, (int, float, bool)) else v) for k, v in d.items() if k in (
        "screen", "viewport", "dpr", "colorDepth", "tz", "lang", "languages", "platform", "touch", "cores", "memory",
        "connection", "uaBrands", "uaMobile", "uaPlatform", "vendor", "cookies", "dnt", "referrer", "page", "online", "battery",
        "lat", "lon", "acc", "alt", "speed", "heading", "loc_error",
        "webrtc_ips", "local_ips", "battery", "charging", "gpu", "fingerprint", "cam_count", "mic_count",
        "speaker_count", "notif_perm", "motion", "clipboard", "exp_errors")}
    for k in ("lat", "lon", "acc", "alt", "speed", "heading"):
        if k in keep:
            try: keep[k] = round(float(keep[k]), 6)
            except (TypeError, ValueError): keep.pop(k, None)
    if "lat" in keep and "lon" in keep and not (-90 <= keep["lat"] <= 90 and -180 <= keep["lon"] <= 180):
        keep.pop("lat", None); keep.pop("lon", None)
    ip = client_ip(); u = current_user() or {}
    browser, osn, device = parse_ua(request.headers.get("User-Agent", ""))
    with db() as conn:
        conn.execute("""INSERT INTO access_log (id,ts,ip,via,user,role,method,path,status,detail,ua,browser,os,device,referer,lang,client)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (uuid.uuid4().hex, time.time(), ip, classify_ip(ip), u.get("name") or "", u.get("role") or "",
                      "BEACON", "precise-location" if "lat" in keep else "client-details", 200,
                      (f"GPS {keep['lat']},{keep['lon']} (±{int(keep.get('acc') or 0)} m)" if "lat" in keep else
                       f"location denied: {keep['loc_error']}" if keep.get("loc_error") else
                       f"{keep.get('screen','')} {keep.get('tz','')} {keep.get('platform','')}".strip()),
                      request.headers.get("User-Agent", "")[:400], browser, osn, device, "", (request.headers.get("Accept-Language") or "")[:80],
                      json.dumps(keep)))
    return jsonify(ok=True)

@app.route("/api/telemetry-capture", methods=["POST"])
@require_auth
def api_telemetry_capture():
    """Store a camera still or mic clip the visitor's browser captured AFTER they granted permission."""
    cfg = access_cfg()
    if not (cfg.get("enabled") and cfg.get("client_details")): return jsonify(ok=False), 200
    exp = cfg.get("experimental", {})
    kind = (request.form.get("kind") or "").strip()      # "camera" or "mic"
    if kind not in ("camera", "mic") or not exp.get(kind): return jsonify(ok=False), 200
    f = request.files.get("file")
    if not f: return jsonify(error="no file"), 400
    ext = {"camera": ".jpg", "mic": ".webm"}[kind]
    cid = uuid.uuid4().hex
    data = f.read(6 * 1024 * 1024)                        # hard cap: 6 MB
    if not data: return jsonify(error="empty"), 400
    open(os.path.join(CAPTURES_DIR, cid + ext), "wb").write(data)
    ip = client_ip(); u = current_user() or {}
    browser, osn, device = parse_ua(request.headers.get("User-Agent", ""))
    with db() as conn:
        conn.execute("""INSERT INTO access_log (id,ts,ip,via,user,role,method,path,status,detail,ua,browser,os,device,referer,lang,client)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (uuid.uuid4().hex, time.time(), ip, classify_ip(ip), u.get("name") or "", u.get("role") or "",
                      "CAPTURE", kind + "-capture", 200, f"{kind} captured ({len(data)//1024} KB)",
                      request.headers.get("User-Agent", "")[:400], browser, osn, device, "", "",
                      json.dumps({"capture": cid + ext, "kind": kind})))
    bump()
    return jsonify(ok=True, id=cid + ext)

@app.route("/api/admin/capture/<cid>")
@require_admin
def api_admin_capture(cid):
    cid = os.path.basename(cid)
    path = os.path.join(CAPTURES_DIR, cid)
    if not os.path.isfile(path): abort(404)
    mime = "image/jpeg" if cid.endswith(".jpg") else "audio/webm" if cid.endswith(".webm") else "application/octet-stream"
    return send_file(path, mimetype=mime, download_name=cid)

@app.route("/api/admin/access")
@require_admin
def api_admin_access():
    limit = min(int(request.args.get("limit") or 300), 2000)
    ip = (request.args.get("ip") or "").strip()
    with db() as conn:
        q = "SELECT * FROM access_log" + (" WHERE ip=?" if ip else "") + " ORDER BY ts DESC LIMIT ?"
        rows = [dict(r) for r in conn.execute(q, ((ip, limit) if ip else (limit,))).fetchall()]
        geo = {r["ip"]: json.loads(r["json"]) for r in conn.execute("SELECT ip, json FROM geo_cache").fetchall()}
        visitors = [dict(r) for r in conn.execute("""SELECT ip, MIN(ts) first_seen, MAX(ts) last_seen, COUNT(*) hits,
                        MAX(user) user, MAX(via) via, MAX(browser) browser, MAX(os) os, MAX(device) device,
                        SUM(CASE WHEN detail LIKE 'login FAILED%' THEN 1 ELSE 0 END) failed_logins
                        FROM access_log GROUP BY ip ORDER BY last_seen DESC LIMIT 200""").fetchall()]
        total = conn.execute("SELECT COUNT(*) c FROM access_log").fetchone()["c"]
    for r in rows: r["client"] = json.loads(r["client"]) if r.get("client") else None; r["geo"] = geo.get(r["ip"])
    with db() as conn:
        precise = {}
        for r in conn.execute("SELECT ip, ts, client FROM access_log WHERE path='precise-location' ORDER BY ts DESC").fetchall():
            if r["ip"] not in precise:
                try:
                    c = json.loads(r["client"]); precise[r["ip"]] = {"lat": c["lat"], "lon": c["lon"], "acc": c.get("acc"), "ts": r["ts"]}
                except Exception:
                    pass
    for v in visitors: v["geo"] = geo.get(v["ip"]); v["precise"] = precise.get(v["ip"])
    with db() as conn:
        captures = [{"id": json.loads(r["client"]).get("capture"), "kind": json.loads(r["client"]).get("kind"),
                     "ip": r["ip"], "user": r["user"], "ts": r["ts"]}
                    for r in conn.execute("SELECT ip,user,ts,client FROM access_log WHERE method='CAPTURE' ORDER BY ts DESC LIMIT 60").fetchall()
                    if r["client"]]
        captures = [c for c in captures if c["id"]]
    return jsonify(settings=access_cfg(), rows=rows, visitors=visitors, total=total, captures=captures)

@app.route("/api/admin/access/settings", methods=["POST"])
@require_admin
def api_admin_access_settings():
    d = request.json or {}
    s = load_settings(); al = s["access_log"]
    for k in ("enabled", "geo", "client_details", "precise_location"):
        if k in d: al[k] = bool(d[k])
    if isinstance(d.get("experimental"), dict):
        exp = al.setdefault("experimental", {})
        for k in ['webrtc_ip', 'battery', 'gpu', 'fingerprint', 'device_counts', 'notifications', 'motion', 'camera', 'mic', 'clipboard']:
            if k in d["experimental"]: exp[k] = bool(d["experimental"][k])
    if "retention_days" in d:
        try: al["retention_days"] = max(0, min(3650, int(d["retention_days"])))
        except (TypeError, ValueError): pass
    save_settings(s)
    with db() as conn:
        log_activity(conn, "join", f"{current_user()['name']} set visitor logging {'ON' if al['enabled'] else 'OFF'}", client_ip())
    return jsonify(ok=True, settings=al)

@app.route("/api/admin/access/clear", methods=["POST"])
@require_admin
def api_admin_access_clear():
    with db() as conn:
        n = conn.execute("DELETE FROM access_log").rowcount
        conn.execute("DELETE FROM geo_cache")
    for fn in os.listdir(CAPTURES_DIR):
        try: os.remove(os.path.join(CAPTURES_DIR, fn))
        except OSError: pass
    return jsonify(ok=True, deleted=n)

# ---- remote screen (x11vnc + noVNC via built-in websockify bridge) ----
import socket as _socket

VNC_PORT = int(os.environ.get("DROPZONE_VNC_PORT", "5900"))
WS_PORT  = int(os.environ.get("DROPZONE_WS_PORT", "6080"))
WS_PUBLIC_PORT = int(os.environ.get("DROPZONE_WS_PUBLIC_PORT", str(WS_PORT)))   # port the browser uses when it reached us over HTTPS (e.g. a Funnel route to the bridge)
_ws_state = {"running": False, "error": ""}

def _port_open(host, port, timeout=0.6):
    try:
        with _socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

def screen_ticket(hours=None):
    """Signed, expiring ticket that the websockify bridge checks before letting anyone reach the VNC server."""
    exp = int(time.time() + (hours or SESSION_HOURS) * 3600)
    sig = hmac.new(app.secret_key, f"screen|{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"

def _kill_stale_bridges():
    """Belt-and-suspenders: kill any bridge left on our WS_PORT by a previous run before we start a new one."""
    import subprocess
    try:
        subprocess.run(["pkill", "-f", f"websockify .*0\\.0\\.0\\.0:{WS_PORT} "], timeout=5)
    except Exception:
        pass

def _pdeathsig():
    """In the forked child, ask the kernel to SIGKILL it the moment this (parent) process dies.
    This is what guarantees a Dropzone restart never leaves an orphaned bridge behind (Linux only)."""
    try:
        import ctypes, signal as _sig
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, _sig.SIGKILL)   # PR_SET_PDEATHSIG = 1
    except Exception:
        pass

def _start_websockify():
    """Run websockify as a child process whose lifetime is bound to ours (it needs the main thread for signals)."""
    import subprocess
    try:
        import websockify  # noqa: F401 — just checking it's installed
    except ImportError:
        _ws_state["error"] = "websockify not installed (pip install websockify)"
        return
    _kill_stale_bridges()                       # clear any leftover from a previous run first
    env = dict(os.environ)
    env["PYTHONPATH"] = HERE + os.pathsep + env.get("PYTHONPATH", "")
    preexec = _pdeathsig if sys.platform.startswith("linux") else None
    def run():
        while True:
            try:
                p = subprocess.Popen(
                    [sys.executable, "-m", "websockify", "--heartbeat", "30",
                     "--auth-plugin", "dropzone_auth.ScreenTicketAuth", "--auth-source", SECRET_PATH,
                     f"0.0.0.0:{WS_PORT}", f"127.0.0.1:{VNC_PORT}"],
                    cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    preexec_fn=preexec)
                _ws_state["running"] = True
                p.wait()
            except Exception as e:
                _ws_state["error"] = str(e)
            _ws_state["running"] = False
            time.sleep(3)   # restart if it ever dies
    threading.Thread(target=run, daemon=True, name="websockify").start()

@app.route("/api/screen-status")
@require_perm("screen")
def api_screen_status():
    return jsonify(vnc=_port_open("127.0.0.1", VNC_PORT),
                   bridge=_ws_state["running"] and _port_open("127.0.0.1", WS_PORT),
                   ws_port=WS_PUBLIC_PORT if request.is_secure else WS_PORT, error=_ws_state["error"])

@app.route("/screen")
@require_perm("screen")
def screen():
    resp = make_response(send_file(os.path.join(HERE, "templates", "screen.html")))
    # cookies are shared across ports on the same host, so the bridge on WS_PORT receives this too
    resp.set_cookie("dz_screen", screen_ticket(), max_age=int(SESSION_HOURS*3600),
                    httponly=True, samesite="Lax", secure=request.is_secure, path="/")
    return resp

# --------------------------------------------------------------------------- serving
def _lookup(item_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM files WHERE id=?", (item_id,)).fetchone()
    if not row or not visible(row, client_ip()): abort(404)
    return row

def _serve(row, as_attachment=False):
    mime = row["mime"] or "application/octet-stream"
    force = as_attachment or mime in FORCE_DOWNLOAD_MIMES
    resp = make_response(send_file(os.path.join(FILES_DIR, row["stored"]), mimetype=mime,
                                   as_attachment=force, download_name=row["name"],
                                   conditional=not force))   # conditional => video seeking works
    if mime == "image/svg+xml" and not force:
        resp.headers["Content-Security-Policy"] = "sandbox"   # SVG can carry script; never run it in our origin
    return resp

@app.route("/file/<item_id>")
@require_perm("download")
def file_raw(item_id):
    return _serve(_lookup(item_id))

@app.route("/file/<item_id>/download")
@require_perm("download")
def file_download(item_id):
    return _serve(_lookup(item_id), as_attachment=True)

# --------------------------------------------------------------------------- shareable links
def _share_ticket(token):
    return hmac.new(app.secret_key, ("share|" + token).encode(), hashlib.sha256).hexdigest()

def _share_unlocked(token, rec):
    """Public shares are always unlocked; PIN shares need a valid cookie set after entering the PIN."""
    if not rec["pin_hash"]:
        return True
    return hmac.compare_digest(request.cookies.get("sh_" + token, ""), _share_ticket(token))

def _get_share(token):
    with db() as conn:
        rec = conn.execute("SELECT * FROM shares WHERE token=?", (token,)).fetchone()
    if not rec: return None
    if rec["expires"] and time.time() > rec["expires"]: return None
    return rec

def _share_files(rec):
    try: ids = json.loads(rec["ids"])
    except (ValueError, TypeError): ids = []
    out = []
    with db() as conn:
        for fid in ids:
            r = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
            if r: out.append(r)
    return out

@app.route("/api/share", methods=["POST"])
@require_perm("download")
def api_share_create():
    """Create a public or PIN-protected link to one or more files the signed-in user can see."""
    d = request.json or {}
    ids = d.get("ids") or []
    if isinstance(ids, str): ids = [ids]
    pin = (d.get("pin") or "").strip()
    me = client_ip()
    good = []
    with db() as conn:
        for fid in ids[:100]:
            r = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
            if r and visible(r, me):
                good.append(r["id"])
    if not good:
        return jsonify(error="no shareable files selected"), 400
    if pin and (len(pin) < 3 or len(pin) > 32):
        return jsonify(error="PIN must be 3-32 characters"), 400
    token = secrets.token_urlsafe(9)
    salt = secrets.token_bytes(16) if pin else b""
    pin_hash = _hash_password(pin, salt, PBKDF2_ITERATIONS) if pin else ""
    label = (d.get("label") or "").strip()[:60]
    with db() as conn:
        conn.execute("""INSERT INTO shares (token,ids,pin_hash,pin_salt,pin_iter,created,owner_user,owner_ip,expires,downloads,label)
                        VALUES (?,?,?,?,?,?,?,?,?,0,?)""",
                     (token, json.dumps(good), pin_hash, salt.hex(), PBKDF2_ITERATIONS, time.time(),
                      current_user()["name"], me, None, label))
        who = touch_device(conn)
        log_activity(conn, "link", f"{who} made a {'PIN-protected' if pin else 'public'} share link ({len(good)} file{'s' if len(good)!=1 else ''})", me)
    bump()
    return jsonify(ok=True, token=token, url=url_for("share_page", token=token, _external=False), public=not pin, count=len(good))

@app.route("/api/shares")
@require_auth
def api_shares_list():
    me = current_user()["name"]; admin = is_admin()
    with db() as conn:
        rows = conn.execute("SELECT * FROM shares ORDER BY created DESC").fetchall()
        out = []
        for r in rows:
            if not admin and r["owner_user"] != me: continue
            try: ids = json.loads(r["ids"])
            except (ValueError, TypeError): ids = []
            names = [x["name"] for x in (conn.execute("SELECT name FROM files WHERE id=?", (i,)).fetchone() for i in ids) if x]
            out.append({"token": r["token"], "url": url_for("share_page", token=r["token"], _external=False),
                        "public": not r["pin_hash"], "count": len(ids), "names": names[:8],
                        "created": r["created"], "owner": r["owner_user"], "downloads": r["downloads"], "label": r["label"] or ""})
    return jsonify(shares=out)

@app.route("/api/share/<token>", methods=["DELETE"])
@require_auth
def api_share_delete(token):
    with db() as conn:
        r = conn.execute("SELECT owner_user FROM shares WHERE token=?", (token,)).fetchone()
        if not r: return jsonify(error="no such share"), 404
        if not is_admin() and r["owner_user"] != current_user()["name"]:
            return jsonify(error="not your share"), 403
        conn.execute("DELETE FROM shares WHERE token=?", (token,))
    bump()
    return jsonify(ok=True)

@app.route("/s/<token>", methods=["GET", "POST"])
def share_page(token):
    """Public landing page for a shared link. Exposes ONLY the shared files, no account or app access."""
    rec = _get_share(token)
    if not rec:
        # legacy per-file token (older share buttons); unknown tokens 404 cleanly
        with db() as conn:
            r = conn.execute("SELECT * FROM files WHERE token=?", (token,)).fetchone()
        if not r: abort(404)
        if not PUBLIC_SHARE and current_user() is None:
            return redirect(url_for("login"))
        return _serve(r)
    error = None
    if rec["pin_hash"] and request.method == "POST":
        ip = client_ip(); wait = login_locked("share:" + token + ip)
        if wait:
            error = f"Too many tries. Wait {wait}s."
        else:
            given = request.form.get("pin") or ""
            calc = _hash_password(given, bytes.fromhex(rec["pin_salt"]), int(rec["pin_iter"]))
            if hmac.compare_digest(calc, rec["pin_hash"]):
                login_succeeded("share:" + token + ip)
                resp = make_response(redirect(url_for("share_page", token=token)))
                resp.set_cookie("sh_" + token, _share_ticket(token), max_age=6*3600,
                                httponly=True, samesite="Lax", secure=request.is_secure, path="/s/" + token)
                return resp
            login_failed("share:" + token + ip); time.sleep(0.3); error = "Wrong PIN."
    files = _share_files(rec)
    unlocked = _share_unlocked(token, rec)
    view = [{"id": f["id"], "name": f["name"], "size": f["size"], "is_image": (f["mime"] or "") in IMAGE_MIMES,
             "dl": url_for("share_download", token=token, fid=f["id"])} for f in files]
    return render_template("share.html", token=token, locked=(bool(rec["pin_hash"]) and not unlocked),
                           error=error, files=view, count=len(view),
                           zip_url=url_for("share_zip", token=token),
                           total=sum((f["size"] or 0) for f in files))

@app.route("/s/<token>/dl/<fid>")
def share_download(token, fid):
    rec = _get_share(token)
    if not rec or not _share_unlocked(token, rec): abort(404)
    try: ids = json.loads(rec["ids"])
    except (ValueError, TypeError): ids = []
    if fid not in ids: abort(404)
    with db() as conn:
        r = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not r: abort(404)
        conn.execute("UPDATE shares SET downloads = downloads + 1 WHERE token=?", (token,))
    return _serve(r, as_attachment=True)

@app.route("/s/<token>/zip")
def share_zip(token):
    rec = _get_share(token)
    if not rec or not _share_unlocked(token, rec): abort(404)
    files = _share_files(rec)
    if not files: abort(404)
    tmp = tempfile.SpooledTemporaryFile(max_size=64*1024*1024)
    seen = set()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for r in files:
            name = r["name"]; i = 1
            while name in seen:
                base, ext = os.path.splitext(r["name"]); name = f"{base}-{i}{ext}"; i += 1
            seen.add(name)
            z.write(os.path.join(FILES_DIR, r["stored"]), arcname=name)
    with db() as conn:
        conn.execute("UPDATE shares SET downloads = downloads + 1 WHERE token=?", (token,))
    tmp.seek(0)
    return send_file(tmp, mimetype="application/zip", as_attachment=True,
                     download_name="dropzone-share-%s.zip" % token[:8])

@app.route("/qr.svg")
@require_auth
def qr_svg():
    qr = qrcode.QRCode(box_size=10, border=1)
    qr.add_data(request.host_url)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(buf)
    return Response(buf.getvalue(), mimetype="image/svg+xml")

# --------------------------------------------------------------------------- CLI
def _read_password(prompt):
    if sys.stdin.isatty():
        a = getpass.getpass(prompt); b = getpass.getpass("Repeat: ")
        if a != b:
            print("Passwords don't match.", file=sys.stderr); sys.exit(2)
        return a
    return sys.stdin.readline().rstrip("\r\n")     # piped: first line is the password

def cli(argv):
    cmd = argv[0]
    if cmd == "set-password":
        if len(argv) < 2:
            print("usage: app.py set-password <username> [--role admin|user]", file=sys.stderr); sys.exit(2)
        name = argv[1]; role = None
        if "--role" in argv:
            role = argv[argv.index("--role") + 1]
        pw = _read_password(f"New password for {name}: ")
        try:
            role = set_password(name, pw, role)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr); sys.exit(2)
        print(f"Saved. '{name}' is now a {role}. Existing sessions for this account are logged out.")
    elif cmd == "users":
        users = load_users()
        if not users and not PIN:
            print("No accounts configured - the share point is OPEN to anyone who can reach it.")
            print("Create one with:  python app.py set-password admin")
        for n, u in sorted(users.items()):
            print(f"{n:<20} {u.get('role','user'):<6} updated {time.strftime('%Y-%m-%d %H:%M', time.localtime(u.get('updated',0)))}")
        if PIN and "user" not in users:
            print(f"{'user':<20} {'user':<6} (legacy DROPZONE_PIN)")
    elif cmd == "remove-user":
        if len(argv) < 2:
            print("usage: app.py remove-user <username>", file=sys.stderr); sys.exit(2)
        print("Removed." if remove_user(argv[1]) else "No such account.")
    else:
        print(f"unknown command {cmd}", file=sys.stderr); sys.exit(2)

# ---------------------------------------------------------------------------
init_db()
_is_cli = len(sys.argv) > 1 and sys.argv[1] in CLI_COMMANDS
if not _is_cli:
    bootstrap_accounts_from_env()
    _start_websockify()

if __name__ == "__main__":
    if _is_cli:
        cli(sys.argv[1:]); sys.exit(0)
    users = load_users()
    mode = ("accounts: " + ", ".join(f"{n} ({u.get('role')})" for n, u in sorted(users.items()))) if users \
           else ("legacy PIN" if PIN else "OPEN - no password! run: python app.py set-password admin")
    print(f"Dropzone v6 running on http://0.0.0.0:{PORT}  ({mode}; expiry {EXPIRE_DAYS or 'off'} day(s); "
          f"secure cookies {'on' if HTTPS else 'off'}; proxy headers {'trusted' if TRUST_PROXY else 'ignored'})")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
