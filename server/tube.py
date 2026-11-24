#!/usr/bin/env python3
"""Tube: a personal video site. Your subscriptions plus YouTube searches for your interests.

Stdlib only (http.server + sqlite3). Runs on the always-on homelab.
Config: tube.json next to this file, or the path in TUBE_CONFIG.

  data/state.db     channels, interests, their videos, and what you liked / saved / watched

  GET    /api/home                         shelves for the home page
  GET    /api/subs?channel=&offset=        newest videos from your channels
  GET    /api/channels                     your subscriptions
  POST   /api/channels {"url": ...}        subscribe (channel URL, @handle or UC... id)
  DELETE /api/channels/<id>                unsubscribe
  GET    /api/interests                    your interests
  POST   /api/interests {"name": ...}      add an interest and search YouTube for it
  DELETE /api/interests/<id>               remove an interest
  GET    /api/foryou?interest=&offset=     the interests' search results, blended
  GET    /api/search?q=                    your channels plus a live YouTube search
  GET    /api/video/<vid>                  one video with related videos
  GET    /api/list?kind=later|liked|history
  POST   /api/state {"vid": ..., liked, later, watched, progress, duration, hidden}
  POST   /api/download {"vid": ...}        queue a download into the Plex folder (needs download_dir)
  GET    /api/download/<vid>               its status: queued, running, done or failed
  GET    /api/description/<vid>            the uploader's description, fetched once then cached
  GET    /health                           liveness, no token

Every /api route needs the app token (Authorization: Bearer <token>).
"""
import contextlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/manifest+json",
        ".png": "image/png", ".svg": "image/svg+xml"}
PAGE = 48
SHELF = 12
REFRESH_WINDOW = 30  # newest videos listed per channel on a routine refresh
DAY = 86400
VID_RE = re.compile(r"[\w-]{11}")
INTEREST_RESULTS = 30  # search results kept per interest
SEARCH_RESULTS = 20
MIN_SECONDS = 90        # shorter results are Shorts or teasers
RSS_NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
          "media": "http://search.yahoo.com/mrss/"}

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, added_at INTEGER NOT NULL,
  refreshed_at INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS sub_videos (
  vid TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
  title TEXT NOT NULL, duration REAL, views INTEGER,
  published INTEGER,              -- exact upload time when the channel feed gave one
  sort_key REAL NOT NULL,         -- upload time, or an order-preserving estimate
  date_approx INTEGER NOT NULL    -- 1 when sort_key is an estimate
);
CREATE INDEX IF NOT EXISTS sub_videos_sort ON sub_videos(sort_key DESC);
CREATE INDEX IF NOT EXISTS sub_videos_channel ON sub_videos(channel_id, sort_key DESC);
CREATE TABLE IF NOT EXISTS state (
  vid TEXT PRIMARY KEY, liked INTEGER NOT NULL DEFAULT 0, later INTEGER NOT NULL DEFAULT 0,
  hidden INTEGER NOT NULL DEFAULT 0, watched_at INTEGER, progress REAL NOT NULL DEFAULT 0,
  duration REAL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS interests (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, added_at INTEGER NOT NULL,
  refreshed_at INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS found_videos (  -- videos YouTube search returned, for an interest or the search box
  vid TEXT PRIMARY KEY, title TEXT NOT NULL, channel TEXT, channel_id TEXT, duration REAL, views INTEGER,
  found_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS interest_videos (
  interest_id INTEGER NOT NULL REFERENCES interests(id) ON DELETE CASCADE,
  vid TEXT NOT NULL, rank INTEGER NOT NULL,  -- position in YouTube's own ordering
  PRIMARY KEY (interest_id, vid)
);
CREATE TABLE IF NOT EXISTS descriptions (
  vid TEXT PRIMARY KEY, text TEXT NOT NULL, upload_date TEXT, views INTEGER, fetched_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS downloads (
  vid TEXT PRIMARY KEY, title TEXT, status TEXT NOT NULL,  -- queued, running, done, failed
  error TEXT, path TEXT, queued_at INTEGER NOT NULL, finished_at INTEGER
);
"""

FOUND_SELECT = """SELECT v.vid, v.title, v.channel, v.channel_id, v.duration, v.views,
  s.liked, s.later, s.watched_at, s.progress, s.duration AS seen_duration
  FROM found_videos v LEFT JOIN state s ON s.vid = v.vid"""
SUB_SELECT = """SELECT v.vid, v.title, c.name AS channel, v.channel_id, v.duration, v.views, v.published,
  v.date_approx, s.liked, s.later, s.watched_at, s.progress, s.duration AS seen_duration
  FROM sub_videos v JOIN channels c ON c.id = v.channel_id LEFT JOIN state s ON s.vid = v.vid"""
KNOWN_TITLE = "SELECT title FROM sub_videos WHERE vid = ? UNION ALL SELECT title FROM found_videos WHERE vid = ?"


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def channel_url(text):
    """Normalise what the user typed into a channel's /videos tab URL (regular uploads, no Shorts)."""
    text = (text or "").strip()
    if re.fullmatch(r"UC[\w-]{22}", text):
        return f"https://www.youtube.com/channel/{text}/videos"
    if re.fullmatch(r"@[\w.-]{1,60}", text):
        return f"https://www.youtube.com/{text}/videos"
    m = re.fullmatch(r"https?://(?:www\.|m\.)?youtube\.com/"
                     r"(channel/UC[\w-]{22}|@[\w.%-]{1,90}|c/[\w.%-]{1,90}|user/[\w.%-]{1,90})(?:[/?#].*)?", text)
    if m:
        return f"https://www.youtube.com/{m.group(1)}/videos"
    raise HttpError(400, "Give a YouTube channel URL, an @handle or a UC... channel id")


def ytdlp_list(binary, url, limit=None):
    """List a channel's uploads, newest first: {"id", "name", "entries": [{vid, title, duration}]}."""
    cmd = [binary, "--flat-playlist", "--no-warnings", "-J"]
    if limit:
        cmd += ["--playlist-end", str(limit)]
    out = subprocess.run(cmd + ["--", url], capture_output=True, text=True, timeout=600)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "yt-dlp failed").strip()[-300:])
    data = json.loads(out.stdout)
    entries = [{"vid": e["id"], "title": e.get("title") or "", "duration": e.get("duration")}
               for e in data.get("entries") or [] if e and VID_RE.fullmatch(e.get("id") or "")]
    return {"id": data.get("channel_id"), "name": data.get("channel") or data.get("uploader"), "entries": entries}


def ytdlp_search(binary, query, limit):
    """YouTube's own search results for a phrase, in its order, without Shorts and live streams."""
    out = subprocess.run([binary, "--flat-playlist", "--no-warnings", "-J", "--", f"ytsearch{int(limit)}:{query}"],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "yt-dlp failed").strip()[-300:])
    found = []
    for e in json.loads(out.stdout).get("entries") or []:
        if not e or not VID_RE.fullmatch(e.get("id") or "") or "/shorts/" in (e.get("url") or ""):
            continue
        if not isinstance(e.get("duration"), (int, float)) or e["duration"] < MIN_SECONDS:
            continue
        views = e.get("view_count")
        found.append({"vid": e["id"], "title": e.get("title") or "", "channel": e.get("channel") or e.get("uploader"),
                      "channel_id": e.get("channel_id"), "duration": e["duration"],
                      "views": views if isinstance(views, int) else None})
    return found


def ytdlp_download(binary, vid, dest, tmp, height=720):
    """Download one video the way the Pinchflat "plex" profile does: H.264/AAC mp4 at up to `height`,
    metadata and cover embedded, a -thumb.jpg beside it. Returns the final file path."""
    name = "%(channel)s %(title)s"
    cmd = [binary, "--no-playlist", "--no-warnings", "--no-progress",
           "-S", f"res:{int(height)},vcodec:h264,acodec:m4a", "--remux-video", "mp4",
           "--embed-metadata", "--embed-thumbnail", "--write-thumbnail", "--convert-thumbnails", "jpg",
           "-P", dest, "-P", "temp:" + tmp,  # partial files stay out of the Plex folder
           "-o", name + ".%(ext)s", "-o", "thumbnail:" + name + "-thumb.%(ext)s",
           "--print", "after_move:filepath", "--", f"https://www.youtube.com/watch?v={vid}"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=3 * 3600)
    lines = [line for line in out.stdout.splitlines() if line.strip()]
    if out.returncode != 0 or not lines:
        raise RuntimeError((out.stderr or "yt-dlp failed").strip()[-300:])
    return lines[-1]


def ytdlp_describe(binary, vid):
    """The uploader's description plus upload date (YYYYMMDD) and views. No download."""
    out = subprocess.run([binary, "--skip-download", "--no-playlist", "--no-warnings",
                          "--print", "%(.{description,upload_date,view_count})j",
                          "--", f"https://www.youtube.com/watch?v={vid}"], capture_output=True, text=True, timeout=120)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError((out.stderr or "yt-dlp failed").strip()[-300:])
    return json.loads(out.stdout.strip().splitlines()[-1])


def fetch_rss(channel_id):
    """Exact upload times and view counts for a channel's ~15 newest uploads: {vid: (ts, views)}."""
    req = urllib.request.Request(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        root = ET.fromstring(resp.read(2_000_000))
    out = {}
    for entry in root.findall("a:entry", RSS_NS):
        vid = entry.findtext("yt:videoId", namespaces=RSS_NS)
        published = entry.findtext("a:published", namespaces=RSS_NS)
        if not vid or not published:
            continue
        stats = entry.find("media:group/media:community/media:statistics", RSS_NS)
        views = int(stats.get("views")) if stats is not None and (stats.get("views") or "").isdigit() else None
        out[vid] = (int(datetime.fromisoformat(published).timestamp()), views)
    return out


def flags(row):
    return {"liked": bool(row["liked"]), "later": bool(row["later"]), "watched": row["watched_at"] is not None,
            "progress": row["progress"] or 0, "seen_duration": row["seen_duration"]}


def found_card(row):
    return {"vid": row["vid"], "source": "yt", "title": row["title"], "channel": row["channel"],
            "channel_id": None, "duration": row["duration"], "views": row["views"], "published": None, **flags(row)}


def sub_card(row):
    return {"vid": row["vid"], "source": "sub", "title": row["title"], "channel": row["channel"],
            "channel_id": row["channel_id"], "duration": row["duration"], "views": row["views"],
            "published": None if row["date_approx"] else row["published"], **flags(row)}


class App:
    def __init__(self, cfg, list_channel=None, rss=None, download=None, describe=None, search=None):
        self.cfg = cfg
        self.state_path = os.path.join(cfg["data_dir"], "state.db")
        self.list_channel = list_channel or (lambda url, limit: ytdlp_list(cfg.get("ytdlp", "yt-dlp"), url, limit))
        self.rss = rss or fetch_rss
        self.refresh_lock = threading.Lock()
        self.download_dir = cfg.get("download_dir")
        self.download = download or (lambda vid: ytdlp_download(
            cfg.get("ytdlp", "yt-dlp"), vid, self.download_dir,
            os.path.join(cfg["data_dir"], "download-tmp"), cfg.get("download_height", 720)))
        self.download_wake = threading.Event()
        self.describe = describe or (lambda vid: ytdlp_describe(cfg.get("ytdlp", "yt-dlp"), vid))
        self.search_youtube = search or (lambda query, limit: ytdlp_search(cfg.get("ytdlp", "yt-dlp"), query, limit))
        with self.connect() as con:
            con.executescript(STATE_SCHEMA)

    @contextlib.contextmanager
    def connect(self):
        """One connection per request."""
        con = sqlite3.connect("file:" + urllib.parse.quote(self.state_path), uri=True, timeout=30)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA foreign_keys = ON")
            with con:
                yield con
        finally:
            con.close()

    # ---- subscriptions ----

    def subscribe(self, text):
        url = channel_url(text)
        try:
            listing = self.list_channel(url, None)
        except Exception as e:
            raise HttpError(502, f"Could not read that channel: {e}")
        if not listing.get("id") or not listing.get("name"):
            raise HttpError(502, "That page did not look like a channel")
        with self.connect() as con:
            con.execute("INSERT INTO channels (id, name, added_at) VALUES (?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
                        (listing["id"], listing["name"], int(time.time())))
        self.store_listing(listing)
        return {"id": listing["id"], "name": listing["name"], "videos": len(listing["entries"])}

    def refresh_channel(self, channel_id, full=False):
        url = f"https://www.youtube.com/channel/{channel_id}/videos"
        try:
            listing = self.list_channel(url, None if full else REFRESH_WINDOW)
            listing["id"] = channel_id
            self.store_listing(listing)
            error = None
        except Exception as e:
            error = str(e)[-300:]
        with self.connect() as con:
            con.execute("UPDATE channels SET refreshed_at = ?, error = ? WHERE id = ?",
                        (int(time.time()), error, channel_id))
        return error

    def store_listing(self, listing):
        """Upsert a newest-first listing. The feed gives exact dates for recent uploads; older ones
        get an estimate that keeps the channel's own order."""
        channel_id = listing["id"]
        try:
            dated = self.rss(channel_id)
        except Exception:
            dated = {}
        with self.connect() as con:
            if not con.execute("SELECT 1 FROM channels WHERE id = ?", (channel_id,)).fetchone():
                return
            existing = {r["vid"]: r for r in con.execute(
                "SELECT vid, sort_key, date_approx, published, views FROM sub_videos WHERE channel_id = ?",
                (channel_id,))}
            last_key = time.time()
            for e in listing["entries"]:
                old = existing.get(e["vid"])
                published, views = old["published"] if old else None, old["views"] if old else None
                if e["vid"] in dated:
                    published, views = dated[e["vid"]]
                    sort_key, approx = float(published), 0
                elif old:
                    sort_key, approx = old["sort_key"], old["date_approx"]
                else:
                    sort_key, approx = last_key - DAY, 1
                last_key = sort_key
                con.execute(
                    "INSERT INTO sub_videos (vid, channel_id, title, duration, views, published, sort_key, date_approx) "
                    "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(vid) DO UPDATE SET title = excluded.title, "
                    "duration = COALESCE(excluded.duration, duration), views = excluded.views, "
                    "published = excluded.published, sort_key = excluded.sort_key, date_approx = excluded.date_approx",
                    (e["vid"], channel_id, e["title"], e["duration"], views, published, sort_key, approx))

    def refresh_all(self):
        if not self.refresh_lock.acquire(blocking=False):
            return
        try:
            with self.connect() as con:
                rows = con.execute("SELECT c.id, (SELECT COUNT(*) FROM sub_videos v WHERE v.channel_id = c.id) AS n "
                                   "FROM channels c").fetchall()
            for row in rows:
                self.refresh_channel(row["id"], full=row["n"] == 0)
        finally:
            self.refresh_lock.release()

    def seed(self, path):
        """First start: subscribe to the channels in channels.seed.json (ids only, filled by the refresher)."""
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            seeds = json.load(f)
        with self.connect() as con:
            if con.execute("SELECT 1 FROM channels LIMIT 1").fetchone():
                return
            con.executemany("INSERT INTO channels (id, name, added_at) VALUES (?,?,?)",
                            [(c["id"], c["name"], int(time.time())) for c in seeds])

    def refresher(self):
        while True:
            for job in (self.refresh_all, self.refresh_interests):
                try:
                    job()
                except Exception as e:
                    print(f"refresh failed: {e}", file=sys.stderr, flush=True)
            time.sleep(max(5, int(self.cfg.get("refresh_minutes", 60))) * 60)

    # ---- interests ----

    def store_found(self, con, entries):
        con.executemany(
            "INSERT INTO found_videos (vid, title, channel, channel_id, duration, views, found_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(vid) DO UPDATE SET title = excluded.title, views = COALESCE(excluded.views, views)",
            [(e["vid"], e["title"], e["channel"], e["channel_id"], e["duration"], e["views"], int(time.time()))
             for e in entries])

    def refresh_interest(self, interest_id):
        """Search YouTube for the interest and replace its results. Old results survive a failed search."""
        with self.connect() as con:
            row = con.execute("SELECT name FROM interests WHERE id = ?", (interest_id,)).fetchone()
        if not row:
            return None
        try:
            entries, error = self.search_youtube(row["name"], INTEREST_RESULTS), None
        except Exception as e:
            entries, error = None, str(e)[-300:] or "Search failed"
        with self.connect() as con:
            if entries is not None:
                self.store_found(con, entries)
                con.execute("DELETE FROM interest_videos WHERE interest_id = ?", (interest_id,))
                con.executemany("INSERT OR IGNORE INTO interest_videos (interest_id, vid, rank) VALUES (?,?,?)",
                                [(interest_id, e["vid"], i) for i, e in enumerate(entries)])
            con.execute("UPDATE interests SET refreshed_at = ?, error = ? WHERE id = ?",
                        (int(time.time()), error, interest_id))
        return error

    def refresh_interests(self):
        stale = time.time() - float(self.cfg.get("interest_refresh_hours", 24)) * 3600
        with self.connect() as con:
            ids = [r["id"] for r in con.execute(
                "SELECT id FROM interests WHERE refreshed_at IS NULL OR refreshed_at < ?", (stale,))]
        for interest_id in ids:
            self.refresh_interest(interest_id)

    def add_interest(self, name):
        name = " ".join(name.split()) if isinstance(name, str) else ""
        if not 2 <= len(name) <= 60:
            raise HttpError(400, "Name the interest in 2 to 60 characters")
        with self.connect() as con:
            if con.execute("SELECT 1 FROM interests WHERE name = ?", (name,)).fetchone():
                raise HttpError(400, "You already have that interest")
            interest_id = con.execute("INSERT INTO interests (name, added_at) VALUES (?, ?)",
                                      (name, int(time.time()))).lastrowid
        error = self.refresh_interest(interest_id)
        return {"id": interest_id, "name": name, "error": error}

    def remove_interest(self, interest_id):
        with self.connect() as con:
            if con.execute("DELETE FROM interests WHERE id = ?", (interest_id,)).rowcount == 0:
                raise HttpError(404, "Unknown interest")
        return {"ok": True}

    def interests(self):
        with self.connect() as con:
            return {"interests": [dict(r) for r in con.execute(
                "SELECT i.id, i.name, i.refreshed_at, i.error, COUNT(v.vid) AS videos "
                "FROM interests i LEFT JOIN interest_videos v ON v.interest_id = i.id GROUP BY i.id "
                "ORDER BY i.name COLLATE NOCASE")]}

    def for_you_vids(self, con, interest=None):
        """Unwatched results of every interest, taken in turns: each interest's 1st, then each one's 2nd..."""
        where, args = "", []
        if interest:
            where, args = " AND iv.interest_id = ?", [interest]
        return list(dict.fromkeys(r["vid"] for r in con.execute(
            "SELECT iv.vid FROM interest_videos iv LEFT JOIN state s ON s.vid = iv.vid "
            f"WHERE s.watched_at IS NULL AND COALESCE(s.hidden, 0) = 0{where} ORDER BY iv.rank, iv.interest_id", args)))

    def for_you(self, q):
        offset = int_arg(q, "offset")
        with self.connect() as con:
            vids = self.for_you_vids(con, q.get("interest"))
            return page(self.cards(con, vids[offset:offset + PAGE]), offset, more=len(vids) > offset + PAGE)

    # ---- downloads to the Plex folder ----

    def download_status(self, con, vid):
        out = {"enabled": bool(self.download_dir), "status": None, "error": None}
        row = con.execute("SELECT status, error, path FROM downloads WHERE vid = ?", (vid,)).fetchone()
        if row and not (row["status"] == "done" and not os.path.exists(row["path"] or "")):
            out.update(status=row["status"], error=row["error"])  # a file deleted from Plex can be fetched again
        if out["status"] == "queued":  # one download at a time: say how many are in front
            out["ahead"] = con.execute(
                "SELECT COUNT(*) FROM downloads WHERE status = 'running' OR (status = 'queued' AND queued_at < "
                "(SELECT queued_at FROM downloads WHERE vid = ?))", (vid,)).fetchone()[0]
        return out

    def downloaded_file(self, vid):
        """Path of a finished download, for "Save to device". None unless it is a file in the download folder."""
        if not self.download_dir or not VID_RE.fullmatch(vid or ""):
            return None
        with self.connect() as con:
            row = con.execute("SELECT path FROM downloads WHERE vid = ? AND status = 'done'", (vid,)).fetchone()
        full = os.path.realpath(row["path"]) if row and row["path"] else ""
        inside = full.startswith(os.path.realpath(self.download_dir) + os.sep)
        return full if inside and os.path.isfile(full) else None

    def queue_download(self, body):
        vid = body.get("vid")
        if not self.download_dir:
            raise HttpError(400, "Downloads are not set up: add download_dir to tube.json")
        if not isinstance(vid, str) or not VID_RE.fullmatch(vid):
            raise HttpError(400, "vid required")
        with self.connect() as con:
            known = con.execute(KNOWN_TITLE, (vid, vid)).fetchone()
            if not known:
                raise HttpError(404, "Unknown video")
            if self.download_status(con, vid)["status"] not in ("queued", "running", "done"):
                con.execute("INSERT INTO downloads (vid, title, status, queued_at) VALUES (?,?,'queued',?) "
                            "ON CONFLICT(vid) DO UPDATE SET status = 'queued', error = NULL, path = NULL, "
                            "queued_at = excluded.queued_at, finished_at = NULL", (vid, known["title"], int(time.time())))
            out = self.download_status(con, vid)
        self.download_wake.set()
        return out

    def run_next_download(self):
        """Download the oldest queued video. One at a time: the homelab is an old machine. False when idle."""
        with self.connect() as con:
            row = con.execute("SELECT vid FROM downloads WHERE status = 'queued' ORDER BY queued_at LIMIT 1").fetchone()
            if not row:
                return False
            con.execute("UPDATE downloads SET status = 'running' WHERE vid = ?", (row["vid"],))
        path = error = None
        try:
            free_gb = shutil.disk_usage(self.download_dir).free / 1e9
            if free_gb < float(self.cfg.get("download_min_free_gb", 10)):
                raise RuntimeError(f"Only {free_gb:.0f} GB free on the homelab, download skipped")
            path = self.download(row["vid"])
        except Exception as e:
            error = str(e)[-300:] or "Download failed"
            print(f"download {row['vid']} failed: {error}", file=sys.stderr, flush=True)
        with self.connect() as con:
            con.execute("UPDATE downloads SET status = ?, error = ?, path = ?, finished_at = ? WHERE vid = ?",
                        ("failed" if error else "done", error, path, int(time.time()), row["vid"]))
        return True

    def downloader(self):
        with self.connect() as con:  # a restart interrupts the running download: start it again
            con.execute("UPDATE downloads SET status = 'queued' WHERE status = 'running'")
        while True:
            try:
                while self.run_next_download():
                    pass
            except Exception as e:
                print(f"downloader failed: {e}", file=sys.stderr, flush=True)
            self.download_wake.wait(300)
            self.download_wake.clear()

    # ---- reads ----

    def cards(self, con, vids):
        """Cards for mixed vids, in the given order. A subscribed video wins over its search twin."""
        vids = list(dict.fromkeys(vids))
        if not vids:
            return []
        marks = ",".join("?" * len(vids))
        found = {r["vid"]: found_card(r) for r in con.execute(f"{FOUND_SELECT} WHERE v.vid IN ({marks})", vids)}
        found.update({r["vid"]: sub_card(r) for r in con.execute(f"{SUB_SELECT} WHERE v.vid IN ({marks})", vids)})
        return [found[v] for v in vids if v in found]

    def home(self):
        shelves = []

        def add(key, title, videos, link=None):
            if videos:
                shelves.append({"id": key, "title": title, "link": link, "videos": videos})

        with self.connect() as con:
            add("continue", "Continue watching", self.cards(con, [r["vid"] for r in con.execute(
                "SELECT vid FROM state WHERE progress > 30 AND watched_at IS NULL AND hidden = 0 "
                "ORDER BY updated_at DESC LIMIT 6")]))
            add("subs", "New from your channels", [sub_card(r) for r in con.execute(
                f"{SUB_SELECT} WHERE v.date_approx = 0 AND s.watched_at IS NULL AND COALESCE(s.hidden, 0) = 0 "
                "ORDER BY v.sort_key DESC LIMIT ?", (SHELF,))], "#/subs")
            add("foryou", "For you", self.cards(con, self.for_you_vids(con)[:2 * SHELF]), "#/foryou")
            add("later", "Watch later", self.cards(con, [r["vid"] for r in con.execute(
                "SELECT vid FROM state WHERE later = 1 ORDER BY updated_at DESC LIMIT ?", (SHELF,))]), "#/later")
        return {"shelves": shelves}

    def subs(self, q):
        offset, channel = int_arg(q, "offset"), q.get("channel")
        with self.connect() as con:
            if channel:
                rows = con.execute(f"{SUB_SELECT} WHERE v.channel_id = ? ORDER BY v.sort_key DESC LIMIT ? OFFSET ?",
                                   (channel, PAGE + 1, offset)).fetchall()
            else:
                rows = con.execute(f"{SUB_SELECT} WHERE v.date_approx = 0 ORDER BY v.sort_key DESC LIMIT ? OFFSET ?",
                                   (PAGE + 1, offset)).fetchall()
        return page([sub_card(r) for r in rows], offset)

    def channels(self):
        with self.connect() as con:
            return {"channels": [dict(r) for r in con.execute(
                "SELECT c.id, c.name, c.refreshed_at, c.error, COUNT(v.vid) AS videos "
                "FROM channels c LEFT JOIN sub_videos v ON v.channel_id = c.id GROUP BY c.id "
                "ORDER BY c.name COLLATE NOCASE")]}

    def search(self, q):
        """Your channels by title, then a live YouTube search. The results are kept so they can be played."""
        text = " ".join((q.get("q") or "").split())[:100]
        if not text:
            return {"subs": [], "youtube": [], "error": None}
        like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        try:
            entries, error = self.search_youtube(text, SEARCH_RESULTS), None
        except Exception as e:
            entries, error = [], str(e)[-300:] or "Search failed"
        with self.connect() as con:
            subs = [sub_card(r) for r in con.execute(
                f"{SUB_SELECT} WHERE v.title LIKE ? ESCAPE '\\' ORDER BY v.sort_key DESC LIMIT 24", (like,))]
            self.store_found(con, entries)
            return {"subs": subs, "youtube": self.cards(con, [e["vid"] for e in entries]), "error": error}

    def video(self, vid):
        with self.connect() as con:
            sub = con.execute(f"{SUB_SELECT} WHERE v.vid = ?", (vid,)).fetchone()
            found = con.execute(f"{FOUND_SELECT} WHERE v.vid = ?", (vid,)).fetchone()
            if not sub and not found:
                raise HttpError(404, "Unknown video")
            out = {"video": sub_card(sub) if sub else found_card(found), "more": []}
            if sub:
                out["more"] = [sub_card(r) for r in con.execute(
                    f"{SUB_SELECT} WHERE v.channel_id = ? AND v.vid != ? ORDER BY v.sort_key DESC LIMIT ?",
                    (sub["channel_id"], vid, SHELF))]
            out["related"] = self.cards(con, [r["vid"] for r in con.execute(  # same interest
                "SELECT b.vid FROM interest_videos a JOIN interest_videos b ON b.interest_id = a.interest_id "
                "WHERE a.vid = ? AND b.vid != ? ORDER BY b.rank LIMIT ?", (vid, vid, SHELF))])
            out["download"] = self.download_status(con, vid)
        return out

    def description(self, vid):
        """Fetched from YouTube on first view (a few seconds), then served from state.db."""
        sql = "SELECT text, upload_date, views FROM descriptions WHERE vid = ?"
        with self.connect() as con:
            row = con.execute(sql, (vid,)).fetchone()
            if not row and not con.execute(KNOWN_TITLE, (vid, vid)).fetchone():
                raise HttpError(404, "Unknown video")
        if not row:
            try:
                info = self.describe(vid)
            except Exception as e:
                raise HttpError(502, f"Could not read the description: {e}")
            views = info.get("view_count")
            with self.connect() as con:
                con.execute("INSERT OR REPLACE INTO descriptions VALUES (?,?,?,?,?)",
                            (vid, str(info.get("description") or "")[:20000], info.get("upload_date"),
                             views if isinstance(views, int) else None, int(time.time())))
                row = con.execute(sql, (vid,)).fetchone()
        return {"text": row["text"], "upload_date": row["upload_date"], "views": row["views"]}

    def listing(self, q):
        kind, offset = q.get("kind"), int_arg(q, "offset")
        where = {"later": "later = 1", "liked": "liked = 1",
                 "history": "(watched_at IS NOT NULL OR progress > 0)"}.get(kind)
        if not where:
            raise HttpError(400, "kind must be later, liked or history")
        with self.connect() as con:
            vids = [r["vid"] for r in con.execute(
                f"SELECT vid FROM state WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?", (PAGE + 1, offset))]
            return page(self.cards(con, vids[:PAGE]), offset, more=len(vids) > PAGE)

    # ---- writes ----

    def set_state(self, body):
        vid = body.get("vid")
        if not isinstance(vid, str) or not VID_RE.fullmatch(vid):
            raise HttpError(400, "vid required")
        now = int(time.time())
        sets, args = ["updated_at = ?"], [now]
        for key in ("liked", "later", "hidden"):
            if key in body:
                sets.append(f"{key} = ?")
                args.append(1 if body[key] else 0)
        for key in ("progress", "duration"):
            if isinstance(body.get(key), (int, float)) and body[key] >= 0:
                sets.append(f"{key} = ?")
                args.append(float(body[key]))
        if "watched" in body:
            sets.append("watched_at = ?")
            args.append(now if body["watched"] else None)
            if not body["watched"] and "progress" not in body:
                sets.append("progress = 0")
        with self.connect() as con:
            con.execute("INSERT OR IGNORE INTO state (vid, updated_at) VALUES (?, ?)", (vid, now))
            con.execute(f"UPDATE state SET {', '.join(sets)} WHERE vid = ?", args + [vid])
            row = con.execute("SELECT liked, later, watched_at, progress, duration AS seen_duration "
                              "FROM state WHERE vid = ?", (vid,)).fetchone()
        return {"vid": vid, **flags(row)}

    def unsubscribe(self, channel_id):
        with self.connect() as con:
            if con.execute("DELETE FROM channels WHERE id = ?", (channel_id,)).rowcount == 0:
                raise HttpError(404, "Unknown channel")
        return {"ok": True}

    # ---- routing ----

    def authorised(self, token):
        expected = self.cfg.get("token") or ""
        return bool(expected) and hmac.compare_digest((token or "").encode(), expected.encode())

    def api(self, method, path, q, body, token):
        """Returns (status, json-able). Raises nothing: errors become {"error": ...}."""
        try:
            if not self.authorised(token):
                raise HttpError(401, "Token required")
            parts = [p for p in path.split("/") if p][1:]
            route = (method, parts[0] if parts else "", len(parts))
            if route == ("GET", "home", 1):
                return 200, self.home()
            if route == ("GET", "subs", 1):
                return 200, self.subs(q)
            if route == ("GET", "channels", 1):
                return 200, self.channels()
            if route == ("POST", "channels", 1):
                return 200, self.subscribe(body.get("url"))
            if route == ("DELETE", "channels", 2):
                return 200, self.unsubscribe(parts[1])
            if route == ("GET", "interests", 1):
                return 200, self.interests()
            if route == ("POST", "interests", 1):
                return 200, self.add_interest(body.get("name"))
            if route == ("DELETE", "interests", 2):
                return 200, self.remove_interest(parts[1])
            if route == ("GET", "foryou", 1):
                return 200, self.for_you(q)
            if route == ("GET", "search", 1):
                return 200, self.search(q)
            if route == ("GET", "video", 2):
                return 200, self.video(parts[1])
            if route == ("GET", "list", 1):
                return 200, self.listing(q)
            if route == ("POST", "state", 1):
                return 200, self.set_state(body)
            if route == ("GET", "description", 2):
                return 200, self.description(parts[1])
            if route == ("POST", "download", 1):
                return 200, self.queue_download(body)
            if route == ("GET", "download", 2):
                with self.connect() as con:
                    return 200, self.download_status(con, parts[1])
            raise HttpError(404, "Not found")
        except HttpError as e:
            return e.status, {"error": str(e)}


def int_arg(q, key):
    try:
        return max(0, int(q.get(key) or 0))
    except ValueError:
        return 0


def page(cards, offset, more=None):
    if more is None:
        more, cards = len(cards) > PAGE, cards[:PAGE]
    return {"videos": cards, "next": offset + PAGE if more else None}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tube"

        def log_message(self, *args):
            pass

        def send(self, status, body, ctype, cache="no-store"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            self.end_headers()
            self.wfile.write(body)

        def send_download(self, full):
            """A downloaded video as an attachment, whole or the byte range asked for (a paused save resumes)."""
            size = os.path.getsize(full)
            start, end, status = 0, size - 1, 200
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), end) if m.group(2) else end
                else:
                    start = max(0, size - int(m.group(2)))
                status = 206
            self.send_response(416 if start > end else status)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if start > end:
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                return self.end_headers()
            name = os.path.basename(full)
            plain = re.sub(r'[^A-Za-z0-9 ._()-]', "_", name)
            self.send_header("Content-Type", "video/mp4" if name.lower().endswith(".mp4") else "application/octet-stream")
            self.send_header("Content-Disposition",
                             f"attachment; filename=\"{plain}\"; filename*=UTF-8''{urllib.parse.quote(name)}")
            self.send_header("Content-Length", str(end - start + 1))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(full, "rb") as f:
                f.seek(start)
                left = end - start + 1
                try:
                    while left > 0:
                        chunk = f.read(min(262144, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the browser cancelled the save

        def dispatch(self, method):
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/health":
                return self.send(200, b'{"ok":true}', "application/json")
            if url.path.startswith("/file/"):
                # A plain link cannot send headers, so the token may come in the query string here.
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip() \
                    or urllib.parse.parse_qs(url.query).get("token", [""])[0]
                if method != "GET" or not app.authorised(token):
                    return self.send(401, b"Token required", "text/plain")
                full = app.downloaded_file(url.path.split("/", 2)[2])
                return self.send_download(full) if full else self.send(404, b"Not found", "text/plain")
            if url.path.startswith("/api/"):
                body = {}
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    try:
                        body = json.loads(self.rfile.read(min(length, 65536)))
                    except ValueError:
                        body = None
                    if not isinstance(body, dict):
                        return self.send(400, b'{"error":"Bad JSON"}', "application/json")
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                try:
                    status, out = app.api(method, url.path, q, body, token)
                except Exception as e:
                    print(f"{method} {url.path}: {e!r}", file=sys.stderr, flush=True)
                    status, out = 500, {"error": "Server error"}
                return self.send(status, json.dumps(out).encode(), "application/json")
            if method != "GET":
                return self.send(405, b"", "text/plain")
            name = "index.html" if url.path == "/" else url.path.lstrip("/")
            full = os.path.realpath(os.path.join(WEB, name))
            if not full.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(full):
                return self.send(404, b"Not found", "text/plain")
            with open(full, "rb") as f:
                self.send(200, f.read(), MIME.get(os.path.splitext(full)[1], "application/octet-stream"))

        def do_GET(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

        def do_DELETE(self):
            self.dispatch("DELETE")

    return Handler


def load_config():
    path = os.environ.get("TUBE_CONFIG") or os.path.join(HERE, "tube.json")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["data_dir"] = os.path.normpath(os.path.join(os.path.dirname(path), cfg.get("data_dir", "../data")))
    if not cfg.get("token"):
        raise SystemExit("tube.json needs a token")
    return cfg


def main():
    cfg = load_config()
    os.makedirs(cfg["data_dir"], exist_ok=True)
    app = App(cfg)
    app.seed(os.path.join(HERE, "channels.seed.json"))
    threading.Thread(target=app.refresher, daemon=True).start()
    if app.download_dir:
        if not os.path.isdir(app.download_dir):
            raise SystemExit(f"download_dir {app.download_dir} does not exist")
        threading.Thread(target=app.downloader, daemon=True).start()
    server = ThreadingHTTPServer((cfg.get("bind", "0.0.0.0"), int(cfg.get("port", 8791))), make_handler(app))
    print(f"tube on :{server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
