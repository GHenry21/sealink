"""SeaLink - tray app that creates Seafile share links for new files/folders.

Listens to the Seafile notification server (websocket), reads the changes of
each commit, creates (or reuses) the share link of new files/folders and
copies it to the clipboard. Credentials are read from SeaDrive (accounts.db,
read-only).
"""
import ctypes
import fnmatch
import json
import logging
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import winreg

import pystray
import websocket
from PIL import Image, ImageDraw

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
HISTORY_PATH = os.path.join(APP_DIR, "history.json")
LOG_PATH = os.path.join(APP_DIR, "sealink.log")
SEADRIVE_DB = os.path.expanduser(r"~\seadrive\accounts.db")
SEADRIVE_SETTINGS = r"Software\SeaDrive\Seafile Drive Client\Settings"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DEFAULT_CONFIG = {
    # empty = derived from the SeaDrive server URL (see notification_url())
    "notification_url": "",
    "auto_copy": True,
    "notify": True,
    "menu_items": 5,
    "history_size": 20,
    # after a new folder got its link, files arriving inside it during this
    # many minutes don't get links of their own
    "folder_quiet_minutes": 10,
    "ignore_patterns": [
        "*._0?_", "*.[0-9]*.[0-9]*.m4v", "*.[0-9]*.[0-9]*.aac", "*.[0-9]*.[0-9]*.wav",
        "*.tmp", "*.temp", "*.part", "*.crdownload", "~*", ".~*", "*.lock",
        "*.prproj", "*.prproj~*", "*.pek", "*.cfa", "*.ims", "*.xmp",
        "Thumbs.db", "desktop.ini", ".DS_Store",
    ],
    "ignore_dirs": ["Adobe Premiere Pro Auto-Save", "CacheClip", ".gallery", "@Recycle"],
    # if not empty, only paths starting with "<library>/<prefix>"
    "include_paths": [],
    "exclude_paths": [],
}

GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

logging.basicConfig(filename=LOG_PATH, level=logging.INFO, encoding="utf-8",
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sealink")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG_PATH, {}))
    if not os.path.exists(CONFIG_PATH):
        save_json(CONFIG_PATH, cfg)
    return cfg


# --- clipboard (Win32) ------------------------------------------------------

def copy_to_clipboard(text):
    k32, u32 = ctypes.windll.kernel32, ctypes.windll.user32
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    data = text.encode("utf-16-le") + b"\0\0"
    for _ in range(10):
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise OSError("clipboard busy")
    try:
        u32.EmptyClipboard()
        h = k32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        ctypes.memmove(k32.GlobalLock(h), data, len(data))
        k32.GlobalUnlock(h)
        u32.SetClipboardData(13, h)  # CF_UNICODETEXT
    finally:
        u32.CloseClipboard()


# --- Seafile API ------------------------------------------------------------

class Seafile:
    def __init__(self):
        db = sqlite3.connect(f"file:{SEADRIVE_DB}?mode=ro", uri=True)
        self.url, self.user, self.token = db.execute(
            "select url, username, token from Accounts order by lastVisited desc").fetchone()
        row = db.execute("select syncrootpath from SyncRootInfo where url=? and username=?",
                         (self.url, self.user)).fetchone()
        db.close()
        self.sync_root = row[0] if row else None
        if self.sync_root and not os.path.isabs(self.sync_root):
            # accounts.db only has the folder name, the root is in the registry
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SEADRIVE_SETTINGS) as k:
                    root = winreg.QueryValueEx(k, "seadriveRoot")[0]
                self.sync_root = os.path.normpath(os.path.join(root, self.sync_root))
            except OSError:
                pass
        self.url = self.url.rstrip("/")

    def notification_url(self):
        # same defaults as the official docker setup: behind a proxy on
        # https://host/notification, or published directly on port 8083
        u = urllib.parse.urlsplit(self.url)
        if u.scheme == "https":
            return f"wss://{u.netloc}/notification/"
        return f"ws://{u.hostname}:8083/"

    def call(self, path, method="GET", body=None):
        data = urllib.parse.urlencode(body).encode() if body else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": "Token " + self.token,
                                              "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as f:
            return json.load(f)

    def exists(self, repo_id, path, is_dir):
        q = urllib.parse.quote(path)
        try:
            if is_dir:
                self.call(f"/api/v2.1/repos/{repo_id}/dir/detail/?path={q}")
            else:
                self.call(f"/api2/repos/{repo_id}/file/detail/?p={q}")
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise

    def share_link(self, repo_id, path):
        q = urllib.parse.quote(path)
        for link in self.call(f"/api/v2.1/share-links/?repo_id={repo_id}&path={q}"):
            if link.get("path", "").rstrip("/") == path.rstrip("/") and not link.get("is_expired"):
                return link["link"]
        return self.call("/api/v2.1/share-links/", "POST",
                         {"repo_id": repo_id, "path": path})["link"]


# --- core -------------------------------------------------------------------

class SeaLink:
    def __init__(self):
        self.cfg = load_config()
        self.sf = Seafile()
        hist = load_json(HISTORY_PATH, {})
        self.files = hist.get("files", [])
        self.folders = hist.get("folders", [])
        self.last_commits = hist.get("last_commits", {})
        self.repos = {}
        self.status = "starting…"
        self.paused = False
        self.events = queue.Queue()
        self.ws = None
        self.lock = threading.Lock()
        self.icon = None

    # history ---------------------------------------------------------------
    def save(self):
        with self.lock:
            save_json(HISTORY_PATH, {"files": self.files, "folders": self.folders,
                                     "last_commits": self.last_commits})

    def latest(self):
        items = self.files[:1] + self.folders[:1]
        return max(items, key=lambda e: e["time"]) if items else None

    # repos -----------------------------------------------------------------
    def refresh_repos(self):
        repos = {}
        for r in self.sf.call("/api2/repos/"):
            if r.get("encrypted"):
                continue
            repos[r["id"]] = r
        self.repos = repos
        return repos

    def local_path(self, repo_id, path):
        r = self.repos.get(repo_id)
        if not r or not self.sf.sync_root:
            return self.sf.sync_root
        rel = path.strip("/").replace("/", os.sep)
        if r.get("type") == "grepo":
            base = os.path.join(self.sf.sync_root, "Shared with groups", r.get("group_name", ""))
        elif r.get("owner") == self.sf.user or r.get("type") == "repo":
            base = os.path.join(self.sf.sync_root, "My Libraries")
        else:
            base = os.path.join(self.sf.sync_root, "Shared with me")
        return os.path.join(base, r["name"], rel)

    # filters ---------------------------------------------------------------
    def ignored(self, repo_id, path, is_dir):
        name = path.rstrip("/").rsplit("/", 1)[-1]
        parts = path.strip("/").split("/")
        if any(d in parts for d in self.cfg["ignore_dirs"]):
            return True
        if not is_dir:
            if any(fnmatch.fnmatch(name.lower(), p.lower()) for p in self.cfg["ignore_patterns"]):
                return True
            if GUID_RE.match(name):
                return True
        full = self.repos.get(repo_id, {}).get("name", "") + "/" + path.strip("/")
        if self.cfg["include_paths"] and not any(full.startswith(p.strip("/")) for p in self.cfg["include_paths"]):
            return True
        return any(full.startswith(p.strip("/")) for p in self.cfg["exclude_paths"])

    def in_quiet_folder(self, repo_id, path):
        limit = time.time() - self.cfg["folder_quiet_minutes"] * 60
        return any(f["repo_id"] == repo_id and f["time"] > limit
                   and path.startswith(f["path"].rstrip("/") + "/") for f in self.folders)

    # commit processing -----------------------------------------------------
    def new_items(self, repo_id, commit_id):
        ch = self.sf.call(f"/api2/repo_history_changes/{repo_id}/?commit_id={commit_id}")
        dirs = ["/" + d.strip("/") for d in ch.get("added_dirs", [])]
        files = ["/" + f.strip("/") for f in ch.get("added_files", [])]
        ren = ch.get("renamed_files", [])
        for old, new in zip(ren[0::2], ren[1::2]):
            # a real rename (e.g. temp -> final name), not just a move
            if old.rsplit("/", 1)[-1] != new.rsplit("/", 1)[-1]:
                files.append("/" + new.strip("/"))
        top_dirs = [d for d in dirs if not any(d.startswith(o + "/") for o in dirs if o != d)]
        files = [f for f in files if not any(f.startswith(d + "/") for d in top_dirs)]
        return [(d, True) for d in top_dirs] + [(f, False) for f in files]

    def handle_commit(self, repo_id, commit_id):
        if self.repos.get(repo_id) is None:
            self.refresh_repos()
        found = []
        for path, is_dir in self.new_items(repo_id, commit_id):
            if self.ignored(repo_id, path, is_dir):
                log.info("ignored %s", path)
                continue
            if not is_dir and self.in_quiet_folder(repo_id, path):
                continue
            if not self.sf.exists(repo_id, path, is_dir):
                log.info("gone %s", path)
                continue
            link = self.sf.share_link(repo_id, path)
            entry = {"name": path.rstrip("/").rsplit("/", 1)[-1], "repo_id": repo_id,
                     "path": path, "dir": is_dir, "link": link, "time": time.time(),
                     "local": self.local_path(repo_id, path)}
            lst = self.folders if is_dir else self.files
            with self.lock:
                lst[:] = [e for e in lst if e["link"] != link]
                lst.insert(0, entry)
                del lst[self.cfg["history_size"]:]
            found.append(entry)
            log.info("link %s -> %s", path, link)
        with self.lock:
            self.last_commits[repo_id] = commit_id
        self.save()
        if found and not self.paused:
            last = found[-1]
            if self.cfg["auto_copy"]:
                copy_to_clipboard(last["link"])
            if self.cfg["notify"] and self.icon:
                more = f" (+{len(found) - 1})" if len(found) > 1 else ""
                self.icon.notify(f"{last['name']}{more}", "Link copied" if self.cfg["auto_copy"] else "Link created")
        if self.icon:
            self.icon.update_menu()

    def catch_up(self):
        """Commits that arrived while disconnected (first run: only remember the head)."""
        for rid, r in self.repos.items():
            last = self.last_commits.get(rid)
            if last is None:
                self.last_commits[rid] = r.get("head_commit_id")
                continue
            if last == r.get("head_commit_id"):
                continue
            missed = []
            for c in self.sf.call(f"/api/v2.1/repos/{rid}/history/?page=1&per_page=50")["data"]:
                if c["commit_id"] == last:
                    break
                missed.append(c["commit_id"])
            for cid in reversed(missed):
                self.events.put((rid, cid))
        self.save()

    def worker(self):
        while True:
            rid, cid = self.events.get()
            try:
                self.handle_commit(rid, cid)
            except Exception:
                log.exception("commit error %s %s", rid, cid)
                self.set_status("error (see log)")

    # websocket -------------------------------------------------------------
    def subscribe(self, ws, repo_ids):
        repos = []
        for rid in repo_ids:
            try:
                tok = self.sf.call(f"/api/v2.1/repos/{rid}/repo-notification-jwt-token/")["token"]
                repos.append({"id": rid, "jwt_token": tok})
            except Exception:
                log.exception("jwt %s", rid)
        ws.send(json.dumps({"type": "subscribe", "content": {"repos": repos}}))
        log.info("subscribed to %d libraries", len(repos))

    def on_open(self, ws):
        self.refresh_repos()
        self.subscribe(ws, list(self.repos))
        self.catch_up()
        self.set_status("connected")

    def on_message(self, ws, raw):
        msg = json.loads(raw)
        c = msg.get("content") or {}
        if msg.get("type") == "repo-update":
            self.events.put((c["repo_id"], c["commit_id"]))
        elif msg.get("type") == "jwt-expired":
            self.subscribe(ws, [c["repo_id"]] if c.get("repo_id") else list(self.repos))

    def ws_loop(self):
        url = self.cfg["notification_url"] or self.sf.notification_url()
        log.info("notification server %s", url)
        delay = 5
        while True:
            try:
                self.ws = websocket.WebSocketApp(
                    url, on_open=self.on_open, on_message=self.on_message,
                    on_error=lambda ws, e: log.warning("ws: %s", e))
                started = time.time()
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
                if time.time() - started > 60:
                    delay = 5
            except Exception:
                log.exception("ws loop")
            self.set_status("reconnecting…")
            time.sleep(delay)
            delay = min(delay * 2, 300)

    def resubscribe_loop(self):
        # libraries created in the meantime
        while True:
            time.sleep(1800)
            try:
                old = set(self.repos)
                new = set(self.refresh_repos()) - old
                if new and self.ws and self.ws.sock and self.ws.sock.connected:
                    self.subscribe(self.ws, new)
            except Exception:
                log.exception("refresh repos")

    # tray ------------------------------------------------------------------
    def set_status(self, s):
        self.status = s
        if self.icon:
            self.icon.icon = make_icon(s)
            self.icon.update_menu()

    def copy(self, entry):
        if entry:
            copy_to_clipboard(entry["link"])

    def open_explorer(self, entry):
        p = entry.get("local") or self.sf.sync_root
        if entry.get("dir") or os.path.isdir(p):
            subprocess.Popen(["explorer", p])
        else:
            subprocess.Popen(f'explorer /select,"{p}"')

    def entry_item(self, e):
        return pystray.MenuItem(e["name"], pystray.Menu(
            pystray.MenuItem("Copy link", lambda: self.copy(e), default=True),
            pystray.MenuItem("Open online", lambda: webbrowser.open(e["link"])),
            pystray.MenuItem("Show in Explorer", lambda: self.open_explorer(e)),
        ))

    def menu_items(self):
        n = self.cfg["menu_items"]
        yield pystray.MenuItem("Copy latest link", lambda: self.copy(self.latest()),
                               default=True, visible=False)
        yield pystray.MenuItem("Files:", None, enabled=False)
        yield from (self.entry_item(e) for e in self.files[:n]) if self.files else \
            [pystray.MenuItem("   (none)", None, enabled=False)]
        yield pystray.MenuItem("Folders:", None, enabled=False)
        yield from (self.entry_item(e) for e in self.folders[:n]) if self.folders else \
            [pystray.MenuItem("   (none)", None, enabled=False)]
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem(f"Status: {self.status}", None, enabled=False)
        yield pystray.MenuItem("Pause", self.toggle_pause, checked=lambda i: self.paused)
        yield pystray.MenuItem("Open config", lambda: os.startfile(CONFIG_PATH))
        yield pystray.MenuItem("Start with Windows", toggle_autostart, checked=lambda i: autostart_enabled())
        yield pystray.MenuItem("Quit", lambda: self.icon.stop())

    def toggle_pause(self):
        self.paused = not self.paused

    def run(self):
        self.icon = pystray.Icon("SeaLink", make_icon(self.status), "SeaLink",
                                 pystray.Menu(self.menu_items))
        for target in (self.worker, self.ws_loop, self.resubscribe_loop):
            threading.Thread(target=target, daemon=True).start()
        self.icon.run()


def make_icon(status):
    if status == "connected":
        color = (46, 160, 67)
    elif status.startswith(("starting", "reconnecting")):
        color = (200, 120, 0)
    else:
        color = (200, 40, 40)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # two chain links
    d.rounded_rectangle((4, 18, 38, 46), radius=14, outline=(255, 255, 255), width=7)
    d.rounded_rectangle((26, 18, 60, 46), radius=14, outline=(255, 255, 255), width=7)
    d.ellipse((44, 44, 62, 62), fill=color)
    return img


def autostart_cmd():
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "SeaLink")
            return True
    except OSError:
        return False


def toggle_autostart():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if autostart_enabled():
            winreg.DeleteValue(k, "SeaLink")
        else:
            winreg.SetValueEx(k, "SeaLink", 0, winreg.REG_SZ, autostart_cmd())


if __name__ == "__main__":
    # single instance
    ctypes.windll.kernel32.CreateMutexW(None, False, "SeaLinkSingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:
        sys.exit(0)
    SeaLink().run()
