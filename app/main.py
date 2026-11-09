"""Folder to Google Photos - watch a folder and upload new photos/videos to Google Photos (via rclone).

Every file is uploaded once. Uploaded files are remembered in a local SQLite
database, so deleting a photo in Google Photos never causes a re-upload.
"""
import base64
import configparser
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, Response, jsonify, request, send_from_directory

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
DEFAULT_SOURCE = os.environ.get("SOURCE_DIR", "/photos")
RCLONE = os.environ.get("RCLONE_BIN", "rclone")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")

SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
DB_FILE = os.path.join(CONFIG_DIR, "uploads.db")
RCLONE_CONF = os.path.join(CONFIG_DIR, "rclone.conf")
LOG_FILE = os.path.join(CONFIG_DIR, "folder-to-google-photos.log")

VERSION = os.environ.get("APP_VERSION", "dev").lstrip("v")
REDIRECT_URI = "http://127.0.0.1:53682/"
SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
    "https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata",
]
SKIP_DIRS = {"@eaDir", ".Trash", ".Trashes", "#recycle", ".thumbnails", ".stfolder"}

DEFAULTS = {
    "source": DEFAULT_SOURCE,
    "remote": "gphotos",
    "subfolder_albums": True,
    "extensions": "jpg,jpeg,heic,heif,png,mp4,mov",
    "since": datetime.now().strftime("%Y-%m-%d"),  # first start: only new files
    "interval_minutes": 15,
    "min_age_minutes": 10,
    "auto": True,
    "client_id": "",
    "client_secret": "",
}

app = Flask(__name__, static_folder="static")
lock = threading.RLock()
wake = threading.Event()
logs = deque(maxlen=2000)
state = {
    "running": False,
    "stop": False,
    "current": None,
    "done": 0,
    "total": 0,
    "last_run": None,
    "last_result": None,
    "next_run": None,
    "google": {"ok": None, "message": "not checked", "checked": None},
}
oauth_state = {"value": None}


# ---------------------------------------------------------------- helpers
def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def log(msg, level="info"):
    line = {"t": now_iso(), "level": level, "msg": msg}
    logs.append(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{line['t']} [{level}] {msg}\n")
        if os.path.getsize(LOG_FILE) > 5_000_000:
            os.replace(LOG_FILE, LOG_FILE + ".1")
    except OSError:
        pass


def load_settings():
    s = dict(DEFAULTS)
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update(json.load(f))
    else:
        save_settings(s)
    return s


def save_settings(s):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def db():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                size INTEGER,
                mtime REAL,
                album TEXT,
                status TEXT,           -- uploaded | failed | skipped
                attempts INTEGER DEFAULT 0,
                error TEXT,
                updated TEXT
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_status ON files(status)")
        cols = {r["name"] for r in con.execute("PRAGMA table_info(files)")}
        if "sha1" not in cols:
            con.execute("ALTER TABLE files ADD COLUMN sha1 TEXT")
        if "duplicate_of" not in cols:
            con.execute("ALTER TABLE files ADD COLUMN duplicate_of TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS idx_sha1 ON files(sha1)")


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_duplicate(rel, size, mtime, digest=None):
    """Path of an already uploaded/skipped copy of this file, or None."""
    with db() as con:
        if digest:
            r = con.execute(
                "SELECT path FROM files WHERE sha1=? AND status IN ('uploaded','skipped') AND path<>? LIMIT 1",
                (digest, rel)).fetchone()
        else:
            # cheap check without reading the file: same name, size and date
            name = os.path.basename(rel)
            r = con.execute(
                """SELECT path FROM files WHERE status IN ('uploaded','skipped') AND path<>?
                   AND size=? AND ABS(mtime-?)<2 AND (path=? OR path LIKE ?) LIMIT 1""",
                (rel, size, mtime, name, "%/" + name)).fetchone()
    return r["path"] if r else None


def exts(s):
    return {"." + e.strip().lower().lstrip(".") for e in s["extensions"].split(",") if e.strip()}


def since_ts(s):
    try:
        return datetime.strptime(s["since"], "%Y-%m-%d").timestamp()
    except (ValueError, TypeError):
        return 0


def scan(s, include_old=False):
    """Yield (relpath, size, mtime) for matching files in the source folder."""
    src = s["source"]
    allowed = exts(s)
    cutoff = 0 if include_old else since_ts(s)
    min_age = time.time() - int(s["min_age_minutes"]) * 60
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() not in allowed:
                continue
            full = os.path.join(root, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            if not include_old and st.st_mtime > min_age:
                continue
            rel = os.path.relpath(full, src).replace(os.sep, "/")
            yield rel, st.st_size, st.st_mtime


def known_paths(statuses=("uploaded", "skipped", "duplicate")):
    with db() as con:
        q = "SELECT path FROM files WHERE status IN (%s)" % ",".join("?" * len(statuses))
        return {r["path"] for r in con.execute(q, statuses)}


def pending(s, limit=None):
    done = known_paths()
    out = []
    for rel, size, mtime in scan(s):
        if rel not in done:
            out.append({"path": rel, "size": size, "mtime": mtime})
            if limit and len(out) >= limit:
                break
    return out


def album_for(s, rel):
    """Album named after the file's subfolder; '' (no album) for files in the top folder."""
    if not s["subfolder_albums"]:
        return ""
    return os.path.dirname(rel).strip("/")


def dest_for(s, rel):
    album = album_for(s, rel)
    name = os.path.basename(rel)
    # 'upload/' adds the photo to the library without putting it in an album
    return f"{s['remote']}:album/{album}/{name}" if album else f"{s['remote']}:upload/{name}"


def rclone(args, timeout=None):
    cmd = [RCLONE, "--config", RCLONE_CONF] + args
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def last_error_line(out):
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    for l in reversed(lines):
        if "ERROR" in l or "CRITICAL" in l or "Failed" in l:
            return l[-400:]
    return (lines[-1] if lines else "unknown error")[-400:]


def record(rel, size, mtime, album, status, error=None, sha1=None, duplicate_of=None):
    with db() as con:
        con.execute(
            """INSERT INTO files(path,size,mtime,album,status,attempts,error,updated,sha1,duplicate_of)
               VALUES(?,?,?,?,?,1,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,
               album=excluded.album, status=excluded.status, attempts=files.attempts+1,
               error=excluded.error, updated=excluded.updated, sha1=excluded.sha1,
               duplicate_of=excluded.duplicate_of""",
            (rel, size, mtime, album, status, error, now_iso(), sha1, duplicate_of),
        )


# ---------------------------------------------------------------- upload run
def check_google(s=None):
    s = s or load_settings()
    if not os.path.exists(RCLONE_CONF):
        res = {"ok": False, "message": "Not connected. Open the Google tab to connect."}
    else:
        try:
            code, out = rclone(["lsd", f"{s['remote']}:album"], timeout=60)
            res = {"ok": code == 0, "message": "Connected" if code == 0 else last_error_line(out)}
        except subprocess.TimeoutExpired:
            res = {"ok": False, "message": "Timed out talking to Google"}
    res["checked"] = now_iso()
    state["google"] = res
    return res


def run_upload():
    with lock:
        if state["running"]:
            return
        state.update(running=True, stop=False, done=0, total=0, current=None)
    s = load_settings()
    uploaded = failed = dups = 0
    try:
        if not os.path.isdir(s["source"]):
            raise RuntimeError(f"Source folder not found: {s['source']}")
        todo = pending(s)
        state["total"] = len(todo)
        if not todo:
            log("No new files.")
        else:
            log(f"Found {len(todo)} new file(s) to upload.")
        for item in todo:
            if state["stop"]:
                log("Stopped by user.", "warn")
                break
            rel = item["path"]
            album = album_for(s, rel)
            state["current"] = rel
            src = os.path.join(s["source"], rel)
            dest = dest_for(s, rel)
            # duplicate check: same photo already uploaded under another path?
            dup = find_duplicate(rel, item["size"], item["mtime"])
            digest = None
            if not dup:
                try:
                    digest = sha1_of(src)
                except OSError as e:
                    log(f"Cannot read {rel}: {e}", "error")
                    state["done"] += 1
                    continue
                dup = find_duplicate(rel, item["size"], item["mtime"], digest)
            if dup:
                record(rel, item["size"], item["mtime"], album, "duplicate", sha1=digest, duplicate_of=dup)
                log(f"Skipped {rel}: same photo as {dup}", "warn")
                dups += 1
                state["done"] += 1
                continue
            try:
                code, out = rclone(
                    ["copyto", src, dest, "--no-check-dest", "--retries", "3",
                     "--low-level-retries", "10", "--stats", "0"],
                    timeout=6 * 3600,
                )
            except subprocess.TimeoutExpired:
                code, out = 1, "ERROR: upload timed out"
            if code == 0:
                uploaded += 1
                record(rel, item["size"], item["mtime"], album, "uploaded", sha1=digest)
                log(f"Uploaded {rel}" + (f" -> album '{album}'" if album else " (no album)"))
            else:
                failed += 1
                err = last_error_line(out)
                record(rel, item["size"], item["mtime"], album, "failed", err, sha1=digest)
                log(f"Failed {rel}: {err}", "error")
            state["done"] += 1
        result = f"{uploaded} uploaded, {dups} duplicate(s) skipped, {failed} failed"
    except Exception as e:  # noqa: BLE001
        result = f"Error: {e}"
        log(result, "error")
    finally:
        state.update(running=False, current=None, last_run=now_iso(), last_result=result)
    if uploaded or failed or dups:
        log(f"Run finished: {result}")


def scheduler():
    time.sleep(3)
    check_google()
    while True:
        s = load_settings()
        interval = max(1, int(s["interval_minutes"])) * 60
        if s["auto"]:
            run_upload()
            state["next_run"] = (datetime.now() + timedelta(seconds=interval)).isoformat(timespec="seconds")
        else:
            state["next_run"] = None
        wake.wait(interval)  # settings changes / retries end the wait early
        wake.clear()


# ---------------------------------------------------------------- rclone.conf
def write_remote(name, client_id, client_secret, token):
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    if os.path.exists(RCLONE_CONF):
        cp.read(RCLONE_CONF, encoding="utf-8")
    if not cp.has_section(name):
        cp.add_section(name)
    cp[name]["type"] = "google photos"
    cp[name]["client_id"] = client_id
    cp[name]["client_secret"] = client_secret
    cp[name]["token"] = json.dumps(token, separators=(",", ":"))
    with open(RCLONE_CONF, "w", encoding="utf-8") as f:
        cp.write(f)


def google_remotes(text):
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read_string(text)
    return [sec for sec in cp.sections() if cp[sec].get("type") == "google photos"]


# ---------------------------------------------------------------- actions
# One function per action; the web UI (/api/...), the token API (/api/v1/...)
# and the MCP endpoint (/mcp) all call these.
def op_status():
    with db() as con:
        counts = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM files GROUP BY status")}
    s = load_settings()
    return {
        **{k: state[k] for k in ("running", "current", "done", "total", "last_run", "last_result", "next_run", "google")},
        "counts": counts,
        "auto": s["auto"],
        "source_ok": os.path.isdir(s["source"]),
        "source": s["source"],
        "subfolder_albums": s["subfolder_albums"],
        "version": VERSION,
    }


def op_pending(limit=500):
    limit = max(1, min(int(limit or 500), 5000))
    items = pending(load_settings(), limit=limit + 1)
    return {"items": items[:limit], "more": len(items) > limit}


def op_files(status="uploaded", query="", limit=500):
    if status not in ("uploaded", "failed", "skipped", "duplicate"):
        raise ValueError("status must be uploaded, failed, skipped or duplicate")
    limit = max(1, min(int(limit or 500), 5000))
    sql, args = "SELECT * FROM files WHERE status=?", [status]
    if query:
        sql += " AND path LIKE ?"
        args.append(f"%{query}%")
    sql += " ORDER BY updated DESC LIMIT ?"
    args.append(limit)
    with db() as con:
        return {"items": [dict(r) for r in con.execute(sql, args)]}


def op_forget(paths):
    paths = [p for p in (paths or []) if isinstance(p, str)]
    with db() as con:
        n = sum(con.execute("DELETE FROM files WHERE path=?", (p,)).rowcount for p in paths)
    log(f"Removed {n} file(s) from history; they will upload again on the next run.", "warn")
    return {"ok": True, "removed": n}


def op_retry_failed():
    with db() as con:
        n = con.execute("DELETE FROM files WHERE status='failed'").rowcount
    log(f"{n} failed file(s) queued for retry.")
    if not state["running"]:
        threading.Thread(target=run_upload, daemon=True).start()
    return {"ok": True, "queued": n}


def op_skip_existing():
    """Mark every matching file currently in the folder as skipped (never upload)."""
    s = load_settings()
    done = known_paths(("uploaded", "skipped", "failed", "duplicate"))
    rows = [(rel, size, mtime, "", "skipped", now_iso())
            for rel, size, mtime in scan(s, include_old=True) if rel not in done]
    with db() as con:
        con.executemany(
            "INSERT OR IGNORE INTO files(path,size,mtime,album,status,attempts,updated) VALUES(?,?,?,?,?,0,?)",
            rows,
        )
    log(f"Marked {len(rows)} existing file(s) as skipped.")
    return {"ok": True, "skipped": len(rows)}


def op_run():
    if state["running"]:
        return {"ok": False, "message": "An upload is already running"}
    threading.Thread(target=run_upload, daemon=True).start()
    return {"ok": True, "message": "Upload started"}


def op_stop():
    state["stop"] = True
    return {"ok": True, "message": "Stopping after the current file" if state["running"] else "Nothing is running"}


def op_get_settings():
    s = load_settings()
    s["client_secret"] = "********" if s.get("client_secret") else ""
    s.pop("api_token", None)
    return s


SETTING_KEYS = {
    "source": str, "remote": str, "extensions": str, "since": str,
    "interval_minutes": int, "min_age_minutes": int,
    "subfolder_albums": bool, "auto": bool,
}


def op_set_settings(data):
    s = load_settings()
    data = data or {}
    unknown = [k for k in data if k not in SETTING_KEYS]
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(unknown)}")
    for k, v in data.items():
        typ = SETTING_KEYS[k]
        if typ is str:
            s[k] = str(v).strip()
        elif typ is int:
            s[k] = max(0, int(v))
        else:
            s[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
    if s.get("since"):
        datetime.strptime(s["since"], "%Y-%m-%d")  # raises ValueError if not YYYY-MM-DD
    s["interval_minutes"] = max(1, s["interval_minutes"])
    save_settings(s)
    log("Settings saved.")
    wake.set()
    return {"ok": True, "settings": op_get_settings()}


def op_logs(lines=200):
    lines = max(1, min(int(lines or 200), 2000))
    return {"items": list(logs)[-lines:]}


def op_google_check():
    return check_google()


# ---------------------------------------------------------------- web
def _basic_ok():
    h = request.headers.get("Authorization", "")
    if not h.startswith("Basic "):
        return False
    try:
        _, pw = base64.b64decode(h[6:]).decode().split(":", 1)
        return secrets.compare_digest(pw, WEB_PASSWORD)
    except Exception:  # noqa: BLE001
        return False


def api_token():
    return os.environ.get("API_TOKEN") or load_settings().get("api_token", "")


def _token_ok():
    tok = api_token()
    h = request.headers.get("Authorization", "")
    given = h[7:].strip() if h.startswith("Bearer ") else request.headers.get("X-API-Key", "")
    return bool(tok and given) and secrets.compare_digest(given, tok)


def auth_required(f):
    """Web UI routes: the web password (if one is set), or the API token."""
    @wraps(f)
    def wrapper(*a, **kw):
        if WEB_PASSWORD and not (_basic_ok() or _token_ok()):
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Folder to Google Photos"'})
        return f(*a, **kw)
    return wrapper


def token_required(f):
    """API and MCP routes: always need the API token."""
    @wraps(f)
    def wrapper(*a, **kw):
        if not _token_ok():
            return jsonify(ok=False, message="Missing or wrong API token. Send 'Authorization: Bearer <token>'."), 401
        return f(*a, **kw)
    return wrapper


def call(fn, *a, **kw):
    try:
        return jsonify(fn(*a, **kw))
    except (ValueError, TypeError) as e:
        return jsonify(ok=False, message=str(e)), 400


@app.get("/health")
def health():
    return "ok"


@app.get("/")
@auth_required
def index():
    return send_from_directory(app.static_folder, "index.html")


# --- web UI routes
@app.get("/api/status")
@auth_required
def api_status():
    return call(op_status)


@app.get("/api/pending")
@auth_required
def api_pending():
    return call(op_pending, 500)


@app.get("/api/files")
@auth_required
def api_files():
    return call(lambda: op_files(request.args.get("status", "uploaded"), request.args.get("q", "").strip())["items"])


@app.post("/api/files/forget")
@auth_required
def api_forget():
    return call(op_forget, (request.get_json(silent=True) or {}).get("paths", []))


@app.post("/api/files/retry-failed")
@auth_required
def api_retry_failed():
    return call(op_retry_failed)


@app.post("/api/skip-existing")
@auth_required
def api_skip_existing():
    return call(op_skip_existing)


@app.post("/api/run")
@auth_required
def api_run():
    return call(op_run)


@app.post("/api/stop")
@auth_required
def api_stop():
    return call(op_stop)


@app.get("/api/settings")
@auth_required
def api_get_settings():
    return call(op_get_settings)


@app.post("/api/settings")
@auth_required
def api_set_settings():
    return call(op_set_settings, request.get_json(silent=True) or {})


@app.get("/api/logs")
@auth_required
def api_logs():
    return call(lambda: op_logs(500)["items"])


@app.post("/api/google/check")
@auth_required
def api_google_check():
    return call(op_google_check)


@app.get("/api/token")
@auth_required
def api_get_token():
    return jsonify(token=api_token(), from_env=bool(os.environ.get("API_TOKEN")))


@app.post("/api/token/regenerate")
@auth_required
def api_regen_token():
    if os.environ.get("API_TOKEN"):
        return jsonify(ok=False, message="The token is set by the API_TOKEN variable; change it there."), 400
    s = load_settings()
    s["api_token"] = secrets.token_urlsafe(32)
    save_settings(s)
    log("API token regenerated.", "warn")
    return jsonify(ok=True, token=s["api_token"])


# --- token API (/api/v1), for scripts and AI agents
V1 = [
    # method, path, handler(request), summary
    ("GET", "/api/v1/status", lambda r: op_status(),
     "Current state: running or idle, progress, counts, Google connection, next run."),
    ("GET", "/api/v1/pending", lambda r: op_pending(r.args.get("limit", 500)),
     "Files waiting to be uploaded (query: limit)."),
    ("GET", "/api/v1/files", lambda r: op_files(r.args.get("status", "uploaded"), r.args.get("q", ""), r.args.get("limit", 500)),
     "Upload history (query: status=uploaded|failed|skipped|duplicate, q=search text, limit)."),
    ("POST", "/api/v1/upload", lambda r: op_run(), "Start an upload run now."),
    ("POST", "/api/v1/stop", lambda r: op_stop(), "Stop the running upload after the current file."),
    ("POST", "/api/v1/files/forget", lambda r: op_forget((r.get_json(silent=True) or {}).get("paths")),
     "Remove files from the history so they upload again (body: {\"paths\": [...]})."),
    ("POST", "/api/v1/files/retry-failed", lambda r: op_retry_failed(), "Retry all failed uploads."),
    ("POST", "/api/v1/skip-existing", lambda r: op_skip_existing(),
     "Mark every file currently in the folder as skipped."),
    ("GET", "/api/v1/settings", lambda r: op_get_settings(), "Current settings."),
    ("PATCH", "/api/v1/settings", lambda r: op_set_settings(r.get_json(silent=True) or {}),
     "Change settings (body: any of source, extensions, since, interval_minutes, min_age_minutes, subfolder_albums, auto, remote)."),
    ("GET", "/api/v1/logs", lambda r: op_logs(r.args.get("lines", 200)), "Recent activity (query: lines)."),
    ("POST", "/api/v1/google/check", lambda r: op_google_check(), "Test the Google Photos connection."),
]


def _v1_view(fn):
    @token_required
    def view():
        return call(fn, request)
    return view


for _m, _p, _fn, _doc in V1:
    app.add_url_rule(_p, endpoint="v1" + _p.replace("/", "_") + "_" + _m, view_func=_v1_view(_fn), methods=[_m])


@app.get("/api/v1")
@app.get("/api/v1/openapi.json")
def api_v1_openapi():
    paths = {}
    for m, p, _fn, doc in V1:
        paths.setdefault(p, {})[m.lower()] = {"summary": doc, "responses": {"200": {"description": "JSON"}}}
    return jsonify({
        "openapi": "3.0.3",
        "info": {"title": "Folder to Google Photos API", "version": VERSION,
                 "description": "Control Folder to Google Photos. Send 'Authorization: Bearer <API token>' (shown in the web UI under API). "
                                "An MCP endpoint for AI assistants is at /mcp."},
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
        "security": [{"bearer": []}],
        "paths": paths,
    })


# --- MCP (Model Context Protocol), streamable HTTP transport, stateless JSON replies
MCP_TOOLS = [
    ("get_status", "Show whether Folder to Google Photos is uploading or idle, progress, how many files were uploaded / failed / "
     "skipped / duplicates, the Google Photos connection, and when the next automatic run is.",
     {}, lambda a: op_status()),
    ("list_pending", "List photos and videos in the watched folder that are waiting to be uploaded.",
     {"limit": {"type": "integer", "description": "Max items (default 100)"}},
     lambda a: op_pending(a.get("limit", 100))),
    ("list_history", "List files from the upload history.",
     {"status": {"type": "string", "enum": ["uploaded", "failed", "skipped", "duplicate"], "description": "Default: uploaded"},
      "query": {"type": "string", "description": "Part of the file path to search for"},
      "limit": {"type": "integer", "description": "Max items (default 100)"}},
     lambda a: op_files(a.get("status", "uploaded"), a.get("query", ""), a.get("limit", 100))),
    ("upload_now", "Start uploading new files to Google Photos now.", {}, lambda a: op_run()),
    ("stop_upload", "Stop the running upload after the current file.", {}, lambda a: op_stop()),
    ("retry_failed", "Retry every upload that failed.", {}, lambda a: op_retry_failed()),
    ("upload_again", "Remove files from the history so the next run uploads them again.",
     {"paths": {"type": "array", "items": {"type": "string"}, "description": "File paths as shown in the history"}},
     lambda a: op_forget(a.get("paths"))),
    ("skip_existing_files", "Mark every file currently in the folder as skipped so it is never uploaded.",
     {}, lambda a: op_skip_existing()),
    ("get_settings", "Show the current settings.", {}, lambda a: op_get_settings()),
    ("update_settings", "Change settings. Pass only the fields to change.",
     {"auto": {"type": "boolean", "description": "Upload automatically on a schedule"},
      "interval_minutes": {"type": "integer", "description": "Minutes between automatic runs"},
      "min_age_minutes": {"type": "integer", "description": "Only upload files unchanged for this many minutes"},
      "since": {"type": "string", "description": "Only files dated on or after this day (YYYY-MM-DD)"},
      "extensions": {"type": "string", "description": "Comma separated file types, e.g. jpg,jpeg,heic,mp4"},
      "subfolder_albums": {"type": "boolean", "description": "Put photos from subfolders into albums named after the subfolder"},
      "source": {"type": "string", "description": "Folder to watch (path inside the container)"}},
     lambda a: op_set_settings(a)),
    ("get_log", "Show recent activity.", {"lines": {"type": "integer", "description": "Default 100"}},
     lambda a: op_logs(a.get("lines", 100))),
    ("check_google", "Test the connection to Google Photos.", {}, lambda a: op_google_check()),
]
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def _rpc(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


def _mcp_handle(msg):
    if not isinstance(msg, dict):
        return _rpc(None, error={"code": -32600, "message": "Invalid request"})
    method, id_, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if id_ is None:  # notification, e.g. notifications/initialized
        return None
    if method == "initialize":
        want = params.get("protocolVersion")
        return _rpc(id_, {
            "protocolVersion": want if want in MCP_VERSIONS else MCP_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "folder-to-google-photos", "title": "Folder to Google Photos", "version": VERSION},
            "instructions": "Folder to Google Photos uploads new photos and videos from a folder to Google Photos, each file once. "
                            "Call get_status first. upload_now starts a run; list_history shows what was uploaded.",
        })
    if method == "ping":
        return _rpc(id_, {})
    if method == "tools/list":
        return _rpc(id_, {"tools": [
            {"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": props, "additionalProperties": False}}
            for n, d, props, _ in MCP_TOOLS]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        tool = next((t for t in MCP_TOOLS if t[0] == name), None)
        if not tool:
            return _rpc(id_, error={"code": -32602, "message": f"Unknown tool: {name}"})
        try:
            out = tool[3](args)
            return _rpc(id_, {"content": [{"type": "text", "text": json.dumps(out, default=str, indent=1)}],
                              "structuredContent": out, "isError": False})
        except Exception as e:  # noqa: BLE001
            return _rpc(id_, {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
    return _rpc(id_, error={"code": -32601, "message": f"Method not found: {method}"})


@app.route("/mcp", methods=["POST", "GET", "DELETE"])
@token_required
def mcp():
    if request.method != "POST":
        return Response(status=405, headers={"Allow": "POST"})
    body = request.get_json(silent=True)
    if body is None:
        return jsonify(_rpc(None, error={"code": -32700, "message": "Parse error"})), 400
    if isinstance(body, list):
        out = [r for r in (_mcp_handle(m) for m in body) if r]
        return (jsonify(out), 200) if out else ("", 202)
    res = _mcp_handle(body)
    return (jsonify(res), 200) if res else ("", 202)


@app.post("/api/google/start")
@auth_required
def api_google_start():
    s = load_settings()
    data = request.json or {}
    if data.get("client_id"):
        s["client_id"] = data["client_id"].strip()
    if data.get("client_secret") and data["client_secret"] != "********":
        s["client_secret"] = data["client_secret"].strip()
    if not s["client_id"] or not s["client_secret"]:
        return jsonify(ok=False, message="Enter your Client ID and Client secret first."), 400
    save_settings(s)
    oauth_state["value"] = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode({
        "client_id": s["client_id"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": oauth_state["value"],
    })
    return jsonify(ok=True, url=url)


@app.post("/api/google/finish")
@auth_required
def api_google_finish():
    s = load_settings()
    pasted = (request.json or {}).get("url", "").strip()
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    code = qs.get("code", [pasted if pasted and "://" not in pasted else ""])[0]
    if qs.get("state") and oauth_state["value"] and qs["state"][0] != oauth_state["value"]:
        return jsonify(ok=False, message="This link belongs to an older sign-in. Click 'Sign in with Google' again."), 400
    if not code:
        return jsonify(ok=False, message="No code found in what you pasted."), 400
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": s["client_id"],
        "client_secret": s["client_secret"],
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()
    try:
        with urllib.request.urlopen("https://oauth2.googleapis.com/token", body, timeout=30) as r:
            tok = json.load(r)
    except urllib.error.HTTPError as e:
        return jsonify(ok=False, message=f"Google refused the code: {e.read().decode()[:300]}"), 400
    if "refresh_token" not in tok:
        return jsonify(ok=False, message="Google did not return a refresh token. Try signing in again."), 400
    expiry = datetime.now(timezone.utc) + timedelta(seconds=int(tok.get("expires_in", 3600)))
    write_remote(s["remote"], s["client_id"], s["client_secret"], {
        "access_token": tok["access_token"],
        "token_type": tok.get("token_type", "Bearer"),
        "refresh_token": tok["refresh_token"],
        "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    })
    oauth_state["value"] = None
    log("Connected to Google Photos.")
    return jsonify(check_google(s))


@app.post("/api/google/import")
@auth_required
def api_google_import():
    text = (request.json or {}).get("text", "")
    try:
        names = google_remotes(text)
    except configparser.Error as e:
        return jsonify(ok=False, message=f"Not a valid rclone.conf: {e}"), 400
    if not names:
        return jsonify(ok=False, message="No Google Photos remote found in that file."), 400
    with open(RCLONE_CONF, "w", encoding="utf-8") as f:
        f.write(text)
    s = load_settings()
    s["remote"] = names[0]
    save_settings(s)
    log(f"Imported rclone.conf (using remote '{names[0]}').")
    return jsonify(check_google(s))


def main():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    init_db()
    s = load_settings()
    if not s.get("api_token"):
        s["api_token"] = secrets.token_urlsafe(32)
        save_settings(s)
    log(f"Folder to Google Photos {VERSION} started.")
    threading.Thread(target=scheduler, daemon=True).start()
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threads=8)


if __name__ == "__main__":
    main()
