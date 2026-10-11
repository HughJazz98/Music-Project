#!/usr/bin/env python3
"""
Local receiver for the Pandora userscript (proof of life).

    python pandora_receiver.py

Listens on http://127.0.0.1:8765, prints every track change the moment it arrives, and
once a track has been playing ~3 s (similar artists known) and a MusicBrainz lookup has
finished, writes one line to nowplaying.txt:

    songName - Artist - Album - releaseDate - genre - similar1, similar2, ...

Every raw event is also appended to events.jsonl. Standard library only.
Open http://127.0.0.1:8765/ in a browser to check that it is running.
"""
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import spotify_player as sp
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 8765
EVENTS_FILE = "events.jsonl"
LINES_FILE = "nowplaying.txt"
CACHE_FILE = "enrich_cache_v2.json"   # v2: release date now prefers the earliest release
# MusicBrainz asks API users to identify themselves with a contact. Put yours here.
USER_AGENT = "PandoraMirrorProofOfLife/0.1 ( put-your-email@example.com )"

SPOTIFY_ON = sp.logged_in()
lock = threading.Lock()
tracks = {}  # pandoraTrackId -> info collected so far for the current play of that track

# Pandora serves edition tags like "(Remastered)" or "(15th Anniversary Edition)"; strip them before searching.
EDITION = re.compile(
    r"\s*[\(\[][^\)\]]*(remaster|deluxe|anniversary|edition|version|mono|stereo|expanded|bonus|explicit)[^\)\]]*[\)\]]",
    re.I,
)


def clean(s):
    return EDITION.sub("", s or "").strip()


# ---------- MusicBrainz lookup (release date + genre), cached on disk ----------
try:
    with open(CACHE_FILE, encoding="utf-8") as f:
        cache = json.load(f)
except Exception:
    cache = {}

mb_lock = threading.Lock()
last_mb_call = 0.0


def save_cache():
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, ensure_ascii=False)


def mb_get(path, params):
    """One MusicBrainz request, kept to about one per second as they require."""
    global last_mb_call
    url = "https://musicbrainz.org/ws/2/" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with mb_lock:
        wait = 1.1 - (time.time() - last_mb_call)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.load(r)
        finally:
            last_mb_call = time.time()


def lookup(title, artist):
    key = f"{clean(title)}|{artist}".lower()
    if key in cache:
        return cache[key]
    result = {"releaseDate": None, "genre": None}
    try:
        query = f'recording:"{clean(title)}" AND artist:"{artist}"'
        data = mb_get("recording", {"query": query, "fmt": "json", "limit": 25})
        recs = data.get("recordings", [])
        want_t, want_a = sp.norm(title), sp.norm(artist)
        cands = []
        for r in recs:
            names = " ".join(c.get("name", "") for c in r.get("artist-credit", []) if isinstance(c, dict))
            if sp.norm(r.get("title", "")) == want_t and want_a in sp.norm(names):
                cands.append(r)
        # Reissues and remasters are separate recordings with later dates, so take the earliest across all exact matches.
        dated = [r for r in cands if re.match(r"^\d{4}", r.get("first-release-date") or "")]
        best = min(dated, key=lambda r: r["first-release-date"]) if dated else (cands[0] if cands else None)
        if best:
            result["releaseDate"] = best.get("first-release-date") or None
            counts = {}
            for r in cands:
                for t in r.get("tags", []):
                    counts[t["name"]] = counts.get(t["name"], 0) + t.get("count", 0)
            if counts:
                result["genre"] = max(counts, key=counts.get)
            else:  # fall back to the artist's own genres
                credit = [c for c in best.get("artist-credit", []) if isinstance(c, dict)]
                aid = credit[0].get("artist", {}).get("id") if credit else None
                if aid:
                    a = mb_get(f"artist/{aid}", {"inc": "genres", "fmt": "json"})
                    genres = sorted(a.get("genres", []), key=lambda g: -g.get("count", 0))
                    if genres:
                        result["genre"] = genres[0]["name"]
    except Exception as e:
        result["error"] = str(e)
    if "error" not in result:
        cache[key] = result
        save_cache()
    return result


# ---------- event handling ----------
def finalize(tid):
    """Write the full line once both the page details and the lookup are in."""
    with lock:
        t = tracks.get(tid)
        if not t or t.get("printed") or not (t.get("details") and t.get("enriched")):
            return
        t["printed"] = True
        parts = [
            t.get("title") or "?",
            t.get("artist") or "?",
            t.get("album") or "?",
            t.get("release") or "?",
            t.get("genre") or "?",
            ", ".join(t.get("similar") or []) or "?",
        ]
    line = " - ".join(parts)
    print(f"[{datetime.now():%H:%M:%S}] LINE {line}", flush=True)
    with open(LINES_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def enrich(tid):
    with lock:
        t = tracks.get(tid)
        title, artist = (t.get("title"), t.get("artist")) if t else (None, None)
    res = lookup(title, artist) if title and artist else {}
    with lock:
        if tid in tracks:
            tracks[tid]["release"] = res.get("releaseDate")
            tracks[tid]["genre"] = res.get("genre")
            tracks[tid]["enriched"] = True
    finalize(tid)


def handle(ev):
    now_ms = int(time.time() * 1000)
    ev["receivedMs"] = now_ms
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    kind = ev.get("event")
    stamp = datetime.now().strftime("%H:%M:%S")
    lag = now_ms - ev.get("sentMs", now_ms)

    if kind == "ad":
        print(f"[{stamp}] AD  {ev.get('title')}", flush=True)
        if SPOTIFY_ON:
            threading.Thread(target=sp.on_ad, daemon=True).start()
    elif kind == "track":
        tid = ev.get("pandoraTrackId")
        if not tid:
            return
        with lock:
            tracks[tid] = {
                "title": ev.get("title"),
                "artist": ev.get("artist"),
                "album": ev.get("album"),
                "details": False,
                "enriched": False,
                "printed": False,
            }
        print(f"[{stamp}] NOW  {ev.get('title')} - {ev.get('artist')} - {ev.get('album') or '?'}   (page to receiver: {lag} ms)", flush=True)
        threading.Thread(target=enrich, args=(tid,), daemon=True).start()
        if SPOTIFY_ON:
            threading.Thread(target=sp.play_for_track, args=(ev,), daemon=True).start()
    elif kind == "details":
        tid = ev.get("pandoraTrackId")
        with lock:
            t = tracks.get(tid)
            if not t:
                return
            t["similar"] = ev.get("similar") or []
            t["durationSec"] = ev.get("durationSec")
            if ev.get("album"):
                t["album"] = ev["album"]
            t["details"] = True
        if SPOTIFY_ON:
            sp.note_details(tid, ev.get("durationSec"))
        finalize(tid)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"pandora receiver is running\n")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        self.send_response(204)
        self.end_headers()
        try:
            handle(json.loads(raw))
        except Exception as e:
            print("bad event:", e, flush=True)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    if not SPOTIFY_ON:
        print("Spotify not logged in yet, so only logging. To enable playback run:  python spotify_player.py login", flush=True)
    print(f"Receiver listening on http://{HOST}:{PORT}  (events -> {EVENTS_FILE}, lines -> {LINES_FILE})", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
