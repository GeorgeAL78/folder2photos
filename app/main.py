"""Folder2Photos - watch a folder and upload new photos/videos to Google Photos (via rclone).

Every file is uploaded once. Uploaded files are remembered in a local SQLite
database, so deleting a photo in Google Photos never causes a re-upload.
"""
import base64
import configparser
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import tempfile
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
LOG_FILE = os.path.join(CONFIG_DIR, "folder2photos.log")

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
    "parallel_uploads": 4,
    "date_albums": True,
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
        con.execute("CREATE TABLE IF NOT EXISTS seen (path TEXT PRIMARY KEY, first_seen REAL)")
        con.execute("CREATE TABLE IF NOT EXISTS albums (folder TEXT PRIMARY KEY, title TEXT, album_id TEXT)")


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
    """Start of the 'only files dated on or after' day. Missing/invalid: no file qualifies by date."""
    try:
        return datetime.strptime(s["since"], "%Y-%m-%d").timestamp()
    except (ValueError, TypeError, KeyError):
        return float("inf")


MAX_AUTO_ATTEMPTS = 3        # failed uploads are retried this many times, then wait for "Retry all failed"
_dircache = {}                # folder -> (folder mtime, [(name, size, mtime)], [subfolders])
_scan_lock = threading.Lock()


def list_media(s):
    """(relpath, size, mtime) of every matching file in the source folder.

    A folder is read again only when its own modification time changed, so a check
    of a big, unchanged library touches the folders and not every file in them.
    """
    src = s["source"]
    allowed = exts(s)
    out, stack, alive = [], [src], set()
    with _scan_lock:
        while stack:
            d = stack.pop()
            try:
                dmtime = os.stat(d).st_mtime_ns
            except OSError:
                continue
            alive.add(d)
            cached = _dircache.get(d)
            if cached and cached[0] == dmtime:
                files, subdirs = cached[1], cached[2]
            else:
                files, subdirs = [], []
                try:
                    entries = list(os.scandir(d))
                except OSError:
                    continue
                for e in entries:
                    if e.name.startswith("."):
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name not in SKIP_DIRS:
                                subdirs.append(e.path)
                        elif e.is_file():
                            st = e.stat()
                            files.append((e.name, st.st_size, st.st_mtime))
                    except OSError:
                        pass
                _dircache[d] = (dmtime, files, subdirs)
            stack.extend(subdirs)
            for name, size, mtime in files:
                if os.path.splitext(name)[1].lower() in allowed:
                    rel = os.path.relpath(os.path.join(d, name), src).replace(os.sep, "/")
                    out.append((rel, size, mtime))
        for d in list(_dircache):
            if d not in alive:
                del _dircache[d]
    out.sort()
    return out


def first_seen(files):
    """{relpath: time the file was first noticed}; records files seen for the first time."""
    now = time.time()
    with db() as con:
        known = {r["path"]: r["first_seen"] for r in con.execute("SELECT path, first_seen FROM seen")}
        new = [(rel, now) for rel, _, _ in files if rel not in known]
        if new:
            con.executemany("INSERT OR IGNORE INTO seen(path, first_seen) VALUES(?,?)", new)
            known.update(new)
    return known


def baseline():
    """When the app first looked at the folder; files that appear after this are new."""
    s = load_settings()
    if not s.get("baseline_ts"):
        s["baseline_ts"] = time.time()
        save_settings(s)
    return float(s["baseline_ts"])


def scan(s, include_old=False):
    """Yield (relpath, size, mtime) for files that are new: dated on/after the start date,
    or added to the folder after the app started watching it."""
    files = list_media(s)
    if include_old:
        yield from files
        return
    seen = first_seen(files)   # record first, so on the very first look everything
    base = baseline()          # already in the folder counts as "was there before"
    cutoff = since_ts(s)
    for rel, size, mtime in files:
        if mtime >= cutoff or seen.get(rel, base) > base:
            yield rel, size, mtime


def known_paths(statuses=("uploaded", "skipped", "duplicate")):
    with db() as con:
        q = "SELECT path FROM files WHERE status IN (%s)" % ",".join("?" * len(statuses))
        return {r["path"] for r in con.execute(q, statuses)}


def given_up_paths():
    with db() as con:
        return {r["path"] for r in con.execute(
            "SELECT path FROM files WHERE status='failed' AND attempts>=?", (MAX_AUTO_ATTEMPTS,))}


def pending(s, limit=None, include_held=False):
    """New files that are not uploaded yet.

    A file is ready once it has not changed on disk for min_age_minutes, so half-copied
    files are never uploaded. "Changed" uses the later of mtime and ctime: a copier can
    restore the photo's original date (mtime), but ctime always shows when the copy wrote it.
    With include_held, files still inside that wait are returned too, with "ready_at".
    """
    done = known_paths() | given_up_paths()
    min_age = int(s["min_age_minutes"]) * 60
    out = []
    for rel, _size, _mtime in scan(s):
        if rel in done:
            continue
        try:  # fresh look: the cached listing may be from while the file was still being copied
            st = os.stat(os.path.join(s["source"], rel))
        except OSError:
            continue
        ready_ts = max(st.st_mtime, st.st_ctime) + min_age
        held = time.time() < ready_ts
        if held and not include_held:
            continue
        out.append({"path": rel, "size": st.st_size, "mtime": st.st_mtime,
                    "ready_at": datetime.fromtimestamp(ready_ts).isoformat(timespec="seconds") if held else None})
        if limit and len(out) >= limit:
            break
    return out


DATE_FOLDER = re.compile(r"^(\d{1,2})[-._ ](\d{1,2})[-._ ](\d{4})$")   # month-day-year, e.g. 11-11-2026


def _date_title(part):
    m = DATE_FOLDER.match(part)
    if m:
        month, day, year = int(m.group(1)), int(m.group(2)), m.group(3)
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year}-{month:02d}-{day:02d}"
    return part


def album_title(s, folder):
    """Album name for a folder ('' = no album): the folder name, with month-day-year
    date folders (as made by camera import tools) written as year-month-day."""
    folder = folder.strip("/")
    if not s["subfolder_albums"] or not folder:
        return ""
    if s.get("date_albums", True):
        folder = "/".join(_date_title(p) for p in folder.split("/"))
    return folder


def album_for(s, rel):
    """Album a file goes to: the one remembered for its folder, else the name from album_title()."""
    folder = os.path.dirname(rel).strip("/")
    if not s["subfolder_albums"] or not folder:
        return ""
    with db() as con:
        r = con.execute("SELECT title FROM albums WHERE folder=?", (folder,)).fetchone()
    return r["title"] if r else album_title(s, folder)


ID_SUFFIX = re.compile(r" \{[^{}]+\}$")   # rclone adds " {albumID}" when two albums share a name


def list_albums(s):
    """[(rclone path, album id)] of the albums this app created."""
    code, out = rclone(["lsjson", "-R", "--dirs-only", f"{s['remote']}:album"], timeout=300)
    if code != 0:
        raise RuntimeError(f"Could not list albums: {last_error_line(out)}")
    return [(a["Path"], a["ID"]) for a in json.loads(out[out.index("["):]) if a.get("ID")]


def resolve_album(s, folder, albums):
    """The rclone album path to upload a folder to, so a folder always uses ONE album.

    Order: the album remembered for this folder (by ID, so same-name albums don't
    matter); an existing album with the new name; an existing album with the folder's
    own name (made before date naming, kept as it is); otherwise a new album.
    Returns (rclone path, title).
    """
    folder = folder.strip("/")
    by_id = {aid: path for path, aid in albums}
    with db() as con:
        r = con.execute("SELECT title, album_id FROM albums WHERE folder=?", (folder,)).fetchone()
    if r and r["album_id"] in by_id:
        return by_id[r["album_id"]], r["title"]
    for title in dict.fromkeys([album_title(s, folder), folder]):
        same = sorted((path, aid) for path, aid in albums if ID_SUFFIX.sub("", path) == title)
        if same:
            path, aid = same[0]
            remember_album(folder, title, aid)
            return path, title
    title = album_title(s, folder)
    return title, title


def remember_album(folder, title, album_id):
    with db() as con:
        con.execute("INSERT INTO albums(folder, title, album_id) VALUES(?,?,?) "
                    "ON CONFLICT(folder) DO UPDATE SET title=excluded.title, album_id=excluded.album_id",
                    (folder.strip("/"), title, album_id))


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
def remote_configured(s):
    """True when rclone.conf has the Google Photos login the uploads use."""
    if not os.path.exists(RCLONE_CONF):
        return False
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(RCLONE_CONF, encoding="utf-8")
    except configparser.Error:
        return False
    return cp.has_section(s["remote"])


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


def upload_group(s, folder, items, on_done, album_path=""):
    """Upload the files of one folder with ONE rclone process.

    One process per album matters: rclone creates the album once and then uploads
    several files into it at the same time (--transfers). Separate processes per file
    each created their own album with the same name.
    Returns {basename: error or None}.
    """
    src = os.path.join(s["source"], folder) if folder else s["source"]
    # 'upload' adds photos to the library without an album
    dest = f"{s['remote']}:album/{album_path}" if album_path else f"{s['remote']}:upload"
    names = [os.path.basename(i["path"]) for i in items]
    results = {n: "not uploaded" for n in names}
    last_general = ""
    fd, list_path = tempfile.mkstemp(prefix="f2p-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(names) + "\n")
    cmd = [RCLONE, "--config", RCLONE_CONF, "copy", src, dest, "--files-from-raw", list_path,
           "--no-check-dest", "--no-traverse", "--transfers", str(max(1, int(s["parallel_uploads"]))),
           "--retries", "3", "--low-level-retries", "10", "--use-json-log", "-v", "--stats", "0"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)
    except OSError as e:
        os.unlink(list_path)
        return {n: f"could not start rclone: {e}" for n in names}

    def stopper():
        while proc.poll() is None:
            if state["stop"]:
                proc.terminate()
                return
            time.sleep(0.5)

    threading.Thread(target=stopper, daemon=True).start()
    for line in proc.stderr:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            last_general = line[-400:]
            continue
        obj, level, msg = d.get("object"), d.get("level"), str(d.get("msg", ""))
        if obj in results and level == "info" and msg.startswith("Copied"):
            if results[obj] is not None:
                results[obj] = None
                on_done(obj)
        elif obj in results and level in ("error", "critical") and results[obj] is not None:
            results[obj] = msg[-400:]
        elif level in ("error", "critical"):
            last_general = msg[-400:]
    proc.wait()
    os.unlink(list_path)
    if state["stop"]:
        return {n: (None if e is None else "stopped") for n, e in results.items()}
    for n, e in results.items():
        if e == "not uploaded":
            results[n] = last_general or f"rclone exited with code {proc.returncode}"
    return results


def run_upload():
    with lock:
        if state["running"]:
            return
        state.update(running=True, stop=False, done=0, total=0, current=None)
    s = load_settings()
    counts = {"uploaded": 0, "failed": 0, "dups": 0}
    result = ""
    try:
        if not os.path.isdir(s["source"]):
            raise RuntimeError(f"Source folder not found: {s['source']}")
        if not remote_configured(s):
            raise RuntimeError("Not connected to Google yet; nothing was uploaded. Connect on the Google tab.")
        todo = pending(s)
        state["total"] = len(todo)
        if not todo:
            log("No new files.")
        else:
            log(f"Found {len(todo)} new file(s) to upload ({s['parallel_uploads']} at a time).")

        # 1. duplicate check, one file at a time, and group the rest by folder (= album)
        groups, digests, seen = {}, {}, {}
        for item in todo:
            if state["stop"]:
                break
            rel = item["path"]
            dup = find_duplicate(rel, item["size"], item["mtime"])
            digest = None
            if not dup:
                try:
                    digest = sha1_of(os.path.join(s["source"], rel))
                except OSError as e:
                    log(f"Cannot read {rel}: {e}", "error")
                    state["done"] += 1
                    continue
                dup = find_duplicate(rel, item["size"], item["mtime"], digest) or seen.get(digest)
            if dup:
                record(rel, item["size"], item["mtime"], album_for(s, rel), "duplicate", sha1=digest, duplicate_of=dup)
                log(f"Skipped {rel}: same photo as {dup}", "warn")
                counts["dups"] += 1
                state["done"] += 1
                continue
            seen[digest] = rel
            digests[rel] = digest
            groups.setdefault(os.path.dirname(rel), []).append(item)

        # 2. one rclone process per folder/album, files in parallel inside it
        albums = []
        if groups and s["subfolder_albums"]:
            try:
                albums = list_albums(s)
            except RuntimeError as e:
                log(f"{e}; albums are matched by name only this run.", "warn")
        for folder, items in groups.items():
            if state["stop"]:
                break
            album_path, album = resolve_album(s, folder, albums) if (s["subfolder_albums"] and folder) else ("", "")
            by_name = {os.path.basename(i["path"]): i for i in items}
            state["current"] = (f"{len(items)} file(s) to album '{album}'" if album
                                else f"{len(items)} file(s) (no album)")

            def on_done(name, by_name=by_name, album=album):
                i = by_name[name]
                record(i["path"], i["size"], i["mtime"], album, "uploaded", sha1=digests[i["path"]])
                log(f"Uploaded {i['path']}" + (f" -> album '{album}'" if album else " (no album)"))
                counts["uploaded"] += 1
                state["done"] += 1

            res = upload_group(s, folder, items, on_done, album_path)
            if album and not ID_SUFFIX.search(album_path) and any(e is None for e in res.values()):
                with db() as con:
                    known = con.execute("SELECT album_id FROM albums WHERE folder=?", (folder,)).fetchone()
                if not known:  # the album was just created: remember it for this folder
                    try:
                        albums = list_albums(s)
                        same = sorted((p, a) for p, a in albums if ID_SUFFIX.sub("", p) == album)
                        if same:
                            remember_album(folder, album, same[0][1])
                    except RuntimeError as e:
                        log(f"Could not look up the new album '{album}': {e}", "warn")
            for name, err in res.items():
                if err is None or err == "stopped":
                    continue
                i = by_name[name]
                record(i["path"], i["size"], i["mtime"], album, "failed", err, sha1=digests[i["path"]])
                with db() as con:
                    tries = con.execute("SELECT attempts FROM files WHERE path=?", (i["path"],)).fetchone()["attempts"]
                note = (f" (tried {tries} times; not retried automatically, use 'Retry all failed')"
                        if tries >= MAX_AUTO_ATTEMPTS else " (will retry on the next run)")
                log(f"Failed {i['path']}: {err}{note}", "error")
                counts["failed"] += 1
                state["done"] += 1
        if state["stop"]:
            log("Stopped by user.", "warn")
        result = f"{counts['uploaded']} uploaded, {counts['dups']} duplicate(s) skipped, {counts['failed']} failed"
    except Exception as e:  # noqa: BLE001
        result = f"Error: {e}"
        log(result, "error")
    finally:
        state.update(running=False, current=None, last_run=now_iso(), last_result=result)
    if any(counts.values()):
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
        "password_set": password_set(),
    }


def op_pending(limit=500):
    limit = max(1, min(int(limit or 500), 5000))
    items = pending(load_settings(), limit=limit + 1, include_held=True)
    shown = items[:limit]
    return {"items": shown, "more": len(items) > limit,
            "held": sum(1 for i in shown if i["ready_at"])}


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
    return {"ok": True, "message": "Stopping after the uploads in progress" if state["running"] else "Nothing is running"}


def op_get_settings():
    s = load_settings()
    s["client_secret"] = "********" if s.get("client_secret") else ""
    for k in ("api_token", "web_password_hash", "baseline_ts"):
        s.pop(k, None)
    return s


SETTING_KEYS = {
    "source": str, "remote": str, "extensions": str, "since": str,
    "interval_minutes": int, "min_age_minutes": int, "parallel_uploads": int,
    "subfolder_albums": bool, "auto": bool, "date_albums": bool,
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
    try:
        datetime.strptime(s.get("since", ""), "%Y-%m-%d")
    except ValueError:
        raise ValueError("The start date ('only files dated on or after') is required, as YYYY-MM-DD.") from None
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_. -]*", s.get("remote", "")):
        raise ValueError("The rclone remote name must not be empty (letters, numbers, _ . - and spaces).")
    if not exts(s):
        raise ValueError("Enter at least one file type.")
    s["interval_minutes"] = max(1, s["interval_minutes"])
    s["parallel_uploads"] = min(16, max(1, int(s.get("parallel_uploads", 4))))
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
def _hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000)
    return f"pbkdf2${salt}${dk.hex()}"


def password_set():
    return bool(WEB_PASSWORD or load_settings().get("web_password_hash"))


def _pw_ok(pw):
    if WEB_PASSWORD:
        return secrets.compare_digest(pw, WEB_PASSWORD)
    stored = load_settings().get("web_password_hash", "")
    if not stored:
        return False
    try:
        _, salt, _ = stored.split("$")
    except ValueError:
        return False
    return secrets.compare_digest(_hash_pw(pw, salt), stored)


def _basic_ok():
    h = request.headers.get("Authorization", "")
    if not h.startswith("Basic "):
        return False
    try:
        _, pw = base64.b64decode(h[6:]).decode().split(":", 1)
        return _pw_ok(pw)
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
        if password_set() and not (_basic_ok() or _token_ok()):
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Folder2Photos"'})
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


@app.post("/api/password")
@auth_required
def api_set_password():
    if WEB_PASSWORD:
        return jsonify(ok=False, message="The password is set by the WEB_PASSWORD variable; change it there."), 400
    new = str((request.get_json(silent=True) or {}).get("password", ""))
    if new and len(new) < 6:
        return jsonify(ok=False, message="Use at least 6 characters."), 400
    s = load_settings()
    if new:
        s["web_password_hash"] = _hash_pw(new)
        log("Web password set.", "warn")
    else:
        s.pop("web_password_hash", None)
        log("Web password removed.", "warn")
    save_settings(s)
    return jsonify(ok=True, password_set=password_set())


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
     "Change settings (body: any of source, extensions, since, interval_minutes, min_age_minutes, subfolder_albums, auto, parallel_uploads, remote)."),
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
        "info": {"title": "Folder2Photos API", "version": VERSION,
                 "description": "Control Folder2Photos. Send 'Authorization: Bearer <API token>' (shown in the web UI under API). "
                                "An MCP endpoint for AI assistants is at /mcp."},
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
        "security": [{"bearer": []}],
        "paths": paths,
    })


# --- MCP (Model Context Protocol), streamable HTTP transport, stateless JSON replies
MCP_TOOLS = [
    ("get_status", "Show whether Folder2Photos is uploading or idle, progress, how many files were uploaded / failed / "
     "skipped / duplicates, the Google Photos connection, and when the next automatic run is.",
     {}, lambda a: op_status()),
    ("list_pending", "List photos and videos in the watched folder that are waiting to be uploaded. Items with "
     "ready_at are still settling (recently copied) and are uploaded by the first run after that time.",
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
      "parallel_uploads": {"type": "integer", "description": "How many files to upload at the same time (1-16)"},
      "since": {"type": "string", "description": "Only files dated on or after this day (YYYY-MM-DD)"},
      "extensions": {"type": "string", "description": "Comma separated file types, e.g. jpg,jpeg,heic,mp4"},
      "subfolder_albums": {"type": "boolean", "description": "Put photos from subfolders into albums named after the subfolder"},
      "date_albums": {"type": "boolean", "description": "Name albums for date folders like 11-11-2026 (month-day-year) as 2026-11-11"},
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
            "serverInfo": {"name": "folder2photos", "title": "Folder2Photos", "version": VERSION},
            "instructions": "Folder2Photos uploads new photos and videos from a folder to Google Photos, each file once. "
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
    log(f"Folder2Photos {VERSION} started.")
    threading.Thread(target=scheduler, daemon=True).start()
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threads=8)


if __name__ == "__main__":
    main()
