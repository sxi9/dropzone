#!/usr/bin/env python3
"""
Dropzone v5 - a private, self-hosted share point for your home network.

Features: file & text sharing, live device detection, activity feed, chat,
pinned items, per-item share links, bulk delete / zip download, send-to-device,
burn-after-read notes, per-item expiry, server-side URL fetch, storage stats,
device renaming, and instant push updates (SSE with polling fallback).

Environment variables (all optional):
    DROPZONE_PORT        port to listen on            (default 8000)
    DROPZONE_DATA        data directory               (default ./data)
    DROPZONE_PIN         optional shared PIN          (default: none = open)
    DROPZONE_MAX_MB      max upload size in MB        (default 2048)
    DROPZONE_EXPIRE_DAYS auto-delete items older than (default 0 = never)
"""

import io
import os
import re
import json
import time
import uuid
import queue
import shutil
import sqlite3
import zipfile
import tempfile
import threading
import mimetypes
import ipaddress
import urllib.request
from functools import wraps

from flask import (
    Flask, request, session, redirect, url_for,
    render_template, jsonify, send_file, abort, Response,
)
import qrcode
import qrcode.image.svg

# --------------------------------------------------------------------------- config
HERE        = os.path.dirname(os.path.abspath(__file__))
PORT        = int(os.environ.get("DROPZONE_PORT", "8000"))
DATA_DIR    = os.environ.get("DROPZONE_DATA", os.path.join(HERE, "data"))
PIN         = os.environ.get("DROPZONE_PIN", "").strip()
MAX_MB      = int(os.environ.get("DROPZONE_MAX_MB", "2048"))
EXPIRE_DAYS = float(os.environ.get("DROPZONE_EXPIRE_DAYS", "0"))

FILES_DIR   = os.path.join(DATA_DIR, "files")
DB_PATH     = os.path.join(DATA_DIR, "dropzone.db")
SECRET_PATH = os.path.join(DATA_DIR, "secret.key")
INDEX_HTML  = os.path.join(HERE, "templates", "index.html")

DEVICE_LIST_WINDOW   = 300
DEVICE_ACTIVE_WINDOW = 60
URL_FETCH_CAP        = MAX_MB * 1024 * 1024

os.makedirs(FILES_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024

if os.path.exists(SECRET_PATH):
    app.secret_key = open(SECRET_PATH, "rb").read()
else:
    app.secret_key = os.urandom(32)
    open(SECRET_PATH, "wb").write(app.secret_key)

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
                                            pinned INTEGER DEFAULT 0, token TEXT, owner_ip TEXT, target_ip TEXT, expires REAL, label TEXT);
        CREATE TABLE IF NOT EXISTS notes   (id TEXT PRIMARY KEY, content TEXT, created REAL,
                                            pinned INTEGER DEFAULT 0, owner_ip TEXT, target_ip TEXT, expires REAL, burn INTEGER DEFAULT 0, label TEXT);
        CREATE TABLE IF NOT EXISTS devices (ip TEXT PRIMARY KEY, name TEXT, custom_name TEXT, type TEXT, first_seen REAL, last_seen REAL);
        CREATE TABLE IF NOT EXISTS activity(id TEXT PRIMARY KEY, icon TEXT, text TEXT, ip TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, ip TEXT, dev TEXT, text TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS meta    (k TEXT PRIMARY KEY, v TEXT);
        """)
        # migrations for anyone upgrading from v3
        for table, cols in (("files", ["pinned INTEGER DEFAULT 0", "token TEXT", "owner_ip TEXT", "target_ip TEXT", "expires REAL", "label TEXT"]),
                            ("notes", ["pinned INTEGER DEFAULT 0", "owner_ip TEXT", "target_ip TEXT", "expires REAL", "burn INTEGER DEFAULT 0", "label TEXT"]),
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

# --------------------------------------------------------------------------- helpers
IMAGE_MIMES = {"image/png","image/jpeg","image/gif","image/webp","image/svg+xml","image/bmp"}
VIDEO_PREFIX, AUDIO_PREFIX = "video/", "audio/"
TEXT_PREVIEW = {"text/plain","text/csv","text/markdown","application/json","text/html","application/javascript"}

def client_ip():
    fwd = request.headers.get("X-Forwarded-For")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "?"

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
def authed():
    return not PIN or session.get("ok") is True

def require_auth(fn):
    @wraps(fn)
    def w(*a, **k):
        if not authed():
            return (jsonify(error="locked"), 401) if request.path.startswith("/api/") else redirect(url_for("login"))
        return fn(*a, **k)
    return w

# --------------------------------------------------------------------------- pages
@app.route("/")
@require_auth
def index():
    return send_file(INDEX_HTML)

@app.route("/login", methods=["GET","POST"])
def login():
    if not PIN: return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if request.form.get("pin","") == PIN:
            session["ok"] = True
            return redirect(url_for("index"))
        error = "That PIN didn't match. Try again."
    return render_template("login.html", error=error)

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login") if PIN else url_for("index"))

# --------------------------------------------------------------------------- state
@app.route("/api/state")
@require_auth
def api_state():
    me = client_ip(); now = time.time()
    with db() as conn:
        purge_expired(conn)
        touch_device(conn)

        items = []
        for r in conn.execute("SELECT * FROM files ORDER BY pinned DESC, created DESC").fetchall():
            if not visible(r, me): continue
            mime = r["mime"] or ""
            items.append({"kind":"file","id":r["id"],"name":r["name"],"size":r["size"],"mime":mime,
                          "is_image":mime in IMAGE_MIMES,"created":r["created"],
                          "pinned":bool(r["pinned"]), "label":r["label"] or "",
                          "preview":preview_kind(mime, r["name"]),
                          "target":r["target_ip"] or "", "mine_target":bool(r["target_ip"]) and r["target_ip"]==me,
                          "share_url":url_for("shared", token=r["token"], _external=False) if r["token"] else "",
                          "raw_url":url_for("file_raw", item_id=r["id"]),
                          "download_url":url_for("file_download", item_id=r["id"])})
        for r in conn.execute("SELECT * FROM notes ORDER BY pinned DESC, created DESC").fetchall():
            if not visible(r, me): continue
            burn = bool(r["burn"])
            items.append({"kind":"note","id":r["id"],
                          "content": None if burn else r["content"],
                          "burn":burn, "created":r["created"], "pinned":bool(r["pinned"]), "label":r["label"] or "",
                          "target":r["target_ip"] or "", "mine_target":bool(r["target_ip"]) and r["target_ip"]==me})
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

        used = conn.execute("SELECT COALESCE(SUM(size),0) s FROM files").fetchone()["s"]
        largest = [{"id":r["id"],"name":r["name"],"size":r["size"]}
                   for r in conn.execute("SELECT id,name,size FROM files ORDER BY size DESC LIMIT 10").fetchall()]
    du = shutil.disk_usage(DATA_DIR)
    return jsonify(items=items, devices=devices, activity=activity, chat=chat,
                   me=me, remote=is_remote(me),
                   stats={"used":used, "free":du.free, "total":du.total, "largest":largest})

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
def _store_file(conn, filename, stream_or_bytes, mime, owner, target):
    ext = os.path.splitext(filename)[1][:12]
    stored = uuid.uuid4().hex + ext
    path = os.path.join(FILES_DIR, stored)
    if isinstance(stream_or_bytes, bytes):
        open(path, "wb").write(stream_or_bytes)
    else:
        stream_or_bytes.save(path)
    size = os.path.getsize(path)
    mime = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    conn.execute("""INSERT INTO files (id,name,stored,size,mime,created,pinned,token,owner_ip,target_ip,expires)
                    VALUES (?,?,?,?,?,?,0,?,?,?,NULL)""",
                 (uuid.uuid4().hex, filename, stored, size, mime, time.time(),
                  uuid.uuid4().hex[:16], owner, target or None))
    return size

@app.route("/api/upload", methods=["POST"])
@require_auth
def api_upload():
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="No files received."), 400
    ip = client_ip()
    target = (request.form.get("target") or "").strip()
    saved = 0
    with db() as conn:
        who = touch_device(conn)
        tgt_name = display_name(conn, target) if target else ""
        for f in files:
            if not f or not f.filename: continue
            _store_file(conn, f.filename, f, f.mimetype, ip, target)
            label = f"{who} shared {f.filename}" + (f" → {tgt_name}" if target else "")
            log_activity(conn, "up", label, ip)
            saved += 1
    if saved: bump()
    return jsonify(saved=saved)

@app.route("/api/note", methods=["POST"])
@require_auth
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
        conn.execute("""INSERT INTO notes (id,content,created,pinned,owner_ip,target_ip,expires,burn)
                        VALUES (?,?,?,0,?,?,?,?)""",
                     (uuid.uuid4().hex, content, time.time(), ip, target or None,
                      (time.time()+hours*3600) if hours>0 else None, 1 if burn else 0))
        label = f"{who} shared " + ("a secret note" if burn else "text")
        if target: label += f" → {display_name(conn, target)}"
        log_activity(conn, "link", label, ip)
    bump()
    return jsonify(ok=True)

@app.route("/api/fetch-url", methods=["POST"])
@require_auth
def api_fetch_url():
    d = request.json if request.is_json else request.form
    url = (d.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return jsonify(error="Not a valid http(s) URL."), 400
    ip = client_ip()
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Dropzone/4"})
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
        _store_file(conn, name, data, mime, ip, None)
        log_activity(conn, "down", f"{who} pulled {name} from the web", ip)
    bump()
    return jsonify(ok=True, name=name)

@app.route("/api/chat", methods=["POST"])
@require_auth
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

@app.route("/api/item/<kind>/<item_id>", methods=["DELETE"])
@require_auth
def api_delete(kind, item_id):
    ip = client_ip()
    with db() as conn:
        who = touch_device(conn)
        if kind == "file":
            row = conn.execute("SELECT name, stored FROM files WHERE id=?", (item_id,)).fetchone()
            if row:
                try: os.remove(os.path.join(FILES_DIR, row["stored"]))
                except OSError: pass
                conn.execute("DELETE FROM files WHERE id=?", (item_id,))
                log_activity(conn, "trash", f"{who} removed {row['name']}", ip)
        elif kind == "note":
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
    ip = client_ip(); n = 0
    with db() as conn:
        who = touch_device(conn)
        for it in ids:
            if it.get("kind") == "file":
                row = conn.execute("SELECT stored FROM files WHERE id=?", (it.get("id"),)).fetchone()
                if row:
                    try: os.remove(os.path.join(FILES_DIR, row["stored"]))
                    except OSError: pass
                    conn.execute("DELETE FROM files WHERE id=?", (it.get("id"),)); n += 1
            elif it.get("kind") == "note":
                n += conn.execute("DELETE FROM notes WHERE id=?", (it.get("id"),)).rowcount
        if n: log_activity(conn, "trash", f"{who} removed {n} items", ip)
    if n: bump()
    return jsonify(deleted=n)

@app.route("/api/zip")
@require_auth
def api_zip():
    ids = [x for x in (request.args.get("ids") or "").split(",") if x]
    if not ids: abort(400)
    tmp = tempfile.SpooledTemporaryFile(max_size=64*1024*1024)
    with db() as conn, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        seen = set()
        for fid in ids:
            r = conn.execute("SELECT name, stored FROM files WHERE id=?", (fid,)).fetchone()
            if not r: continue
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
    with db() as conn:
        who = touch_device(conn)
        old = display_name(conn, ip)
        conn.execute("UPDATE devices SET custom_name=? WHERE ip=?", (name or None, ip))
        if name and name != old:
            log_activity(conn, "join", f"{old} is now “{name}”", me)
    bump()
    return jsonify(ok=True)

@app.route("/api/preview-text/<item_id>")
@require_auth
def api_preview_text(item_id):
    with db() as conn:
        r = conn.execute("SELECT stored, mime, size FROM files WHERE id=?", (item_id,)).fetchone()
    if not r: abort(404)
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
@require_auth
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

# ---- remote screen (x11vnc + noVNC via built-in websockify bridge) ----
import socket as _socket

VNC_PORT = int(os.environ.get("DROPZONE_VNC_PORT", "5900"))
WS_PORT  = int(os.environ.get("DROPZONE_WS_PORT", "6080"))
_ws_state = {"running": False, "error": ""}

def _port_open(host, port, timeout=0.6):
    try:
        with _socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

def _start_websockify():
    """Run websockify as a child process (it needs the main thread for signals)."""
    import subprocess, sys
    try:
        import websockify  # noqa: F401 — just checking it's installed
    except ImportError:
        _ws_state["error"] = "websockify not installed (pip install websockify)"
        return
    def run():
        while True:
            try:
                p = subprocess.Popen(
                    [sys.executable, "-m", "websockify", "--heartbeat", "30",
                     f"0.0.0.0:{WS_PORT}", f"127.0.0.1:{VNC_PORT}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                _ws_state["running"] = True
                p.wait()
            except Exception as e:
                _ws_state["error"] = str(e)
            _ws_state["running"] = False
            time.sleep(3)   # restart if it ever dies
    threading.Thread(target=run, daemon=True, name="websockify").start()

@app.route("/api/screen-status")
@require_auth
def api_screen_status():
    return jsonify(vnc=_port_open("127.0.0.1", VNC_PORT),
                   bridge=_ws_state["running"] and _port_open("127.0.0.1", WS_PORT),
                   ws_port=WS_PORT, error=_ws_state["error"])

@app.route("/screen")
@require_auth
def screen():
    return send_file(os.path.join(HERE, "templates", "screen.html"))

# --------------------------------------------------------------------------- serving
def _lookup(item_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM files WHERE id=?", (item_id,)).fetchone()
    if not row: abort(404)
    return row

@app.route("/file/<item_id>")
@require_auth
def file_raw(item_id):
    r = _lookup(item_id)
    return send_file(os.path.join(FILES_DIR, r["stored"]), mimetype=r["mime"],
                     download_name=r["name"], conditional=True)   # conditional => video seeking works

@app.route("/file/<item_id>/download")
@require_auth
def file_download(item_id):
    r = _lookup(item_id)
    return send_file(os.path.join(FILES_DIR, r["stored"]), mimetype=r["mime"],
                     as_attachment=True, download_name=r["name"])

@app.route("/s/<token>")
def shared(token):
    """Per-item share link — works without the PIN so it can be handed to anyone on the network."""
    with db() as conn:
        r = conn.execute("SELECT * FROM files WHERE token=?", (token,)).fetchone()
    if not r: abort(404)
    return send_file(os.path.join(FILES_DIR, r["stored"]), mimetype=r["mime"],
                     download_name=r["name"], conditional=True)

@app.route("/qr.svg")
@require_auth
def qr_svg():
    qr = qrcode.QRCode(box_size=10, border=1)
    qr.add_data(request.host_url)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(buf)
    return Response(buf.getvalue(), mimetype="image/svg+xml")

# ---------------------------------------------------------------------------
init_db()
_start_websockify()

if __name__ == "__main__":
    print(f"Dropzone v5 running on http://0.0.0.0:{PORT}  (PIN {'set' if PIN else 'off'}, "
          f"expiry {EXPIRE_DAYS or 'off'} day(s))")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
