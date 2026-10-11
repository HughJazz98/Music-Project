#!/usr/bin/env python3
"""
pandora_receiver_v16.py = v15 + cover-colour theme support + "mixed by" credit.

    python pandora_receiver_v16.py

Imports the older receivers (v11 to v15): keep them, wiki_v14.py and the other helper files in the same folder.
Needs player_v16.html. Use it with pandora_userscript_v12.user.js. Open http://127.0.0.1:8765/player

/art?u=...   hands the player page a copy of the cover image from this same address. Without it the browser will not let the
             page read the image's pixels, and the theme/accent colours could not be worked out. Only Spotify / Apple image
             hosts are allowed, https only. Images are kept in memory.
Mixed by     MusicBrainz "mix" credits: the song's recordings first, then the album's release. Often nobody is credited; the
             page then says so. Results are remembered in mix_cache_v1.json.
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer

import lastfm_v12 as lfm
import mirror_library_v11 as lib
import pandora_receiver_v11 as base
import pandora_receiver_v12 as v12
import pandora_receiver_v13 as v13
import pandora_receiver_v15 as v15
import spotify_playlists_v12 as pls
import spotify_player_v11 as sp

PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "player_v16.html")
MIX_CACHE_FILE = "mix_cache_v1.json"
ART_HOSTS = (".scdn.co", ".spotifycdn.com", ".mzstatic.com")
ART_MAX = 8 * 1024 * 1024


def _say(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------- cover images, passed through ----------
_art = {}                         # url -> (content type, bytes)
_art_lock = threading.Lock()


def art_allowed(url):
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host.endswith(s) for s in ART_HOSTS)


def fetch_art(url):
    """(content type, bytes) or None when the address is not an allowed image host or the download fails."""
    if not art_allowed(url):
        return None
    with _art_lock:
        if url in _art:
            return _art[url]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PandoraMirror/0.16 (personal use)"})
        with urllib.request.urlopen(req, timeout=12) as r:
            ctype = r.headers.get("Content-Type", "image/jpeg")
            data = r.read(ART_MAX + 1)
    except Exception as e:
        _say(f"ART fetch failed: {e}")
        return None
    if len(data) > ART_MAX or not ctype.startswith("image/"):
        return None
    with _art_lock:
        if len(_art) > 200:
            _art.pop(next(iter(_art)))
        _art[url] = (ctype, data)
    return ctype, data


# ---------- mixing credit (MusicBrainz) ----------
try:
    with open(MIX_CACHE_FILE, encoding="utf-8") as f:
        _mcache = json.load(f)
except Exception:
    _mcache = {}
_mix = {}                         # pandoraTrackId -> {"state": "pending" | "done" | "error", "names": [...], "from": ...}
_mix_lock = threading.Lock()


def _mixer_names(info):
    names = []
    for rel in info.get("relations", []) or []:
        if rel.get("type") != "mix":
            continue
        n = (rel.get("artist") or {}).get("name")
        if n and n not in names:
            names.append(n)
    return names


def find_mixers(title, artist, album):
    """([names], 'recording' | 'release' | None), mirroring v13's producer search."""
    q = f'recording:"{base.clean(title)}" AND artist:"{artist}"'
    data = base.mb_get("recording", {"query": q, "fmt": "json", "limit": 25})
    want_t, want_a = sp.norm(title), sp.norm(artist)
    cands = []
    for r in data.get("recordings", []):
        credit = " ".join(c.get("name", "") for c in r.get("artist-credit", []) if isinstance(c, dict))
        if sp.norm(r.get("title", "")) == want_t and want_a in sp.norm(credit):
            cands.append(r)
    cands.sort(key=lambda r: r.get("first-release-date") or "9999")
    for r in cands[:3]:
        names = _mixer_names(base.mb_get(f"recording/{r['id']}", {"inc": "artist-rels", "fmt": "json"}))
        if names:
            return names, "recording"
    want_album = sp.norm(base.clean(album)) if album else None
    rid = None
    if want_album:
        for r in cands:
            for rel in r.get("releases", []):
                if sp.norm(base.clean(rel.get("title", ""))) == want_album:
                    rid = rel.get("id")
                    break
            if rid:
                break
    if rid:
        names = _mixer_names(base.mb_get(f"release/{rid}", {"inc": "artist-rels", "fmt": "json"}))
        if names:
            return names, "release"
    return [], None


def _mix_job(tid):
    with base.lock:
        t = dict(base.tracks.get(tid) or {})
    title, artist, album = t.get("title"), t.get("artist"), t.get("album")
    if not (title and artist):
        return
    key = f"{base.clean(title)}|{artist}|{base.clean(album)}".lower()
    if key in _mcache:
        with _mix_lock:
            _mix[tid] = {"state": "done", "names": _mcache[key]["names"], "from": _mcache[key].get("from")}
        return
    # wait for the producer lookup (v13) first: same one-request-a-second MusicBrainz line
    end = time.time() + 40
    while time.time() < end:
        with v13._prod_lock:
            if (v13._prod.get(tid) or {}).get("state") != "pending":
                break
        time.sleep(0.5)
    try:
        names, src = find_mixers(title, artist, album)
    except Exception as e:
        _say(f"MIXER lookup failed for {title} - {artist}: {e}")
        with _mix_lock:
            _mix[tid] = {"state": "error", "names": [], "from": None}
        return
    _mcache[key] = {"names": names, "from": src}
    try:
        with open(MIX_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_mcache, f, indent=1, ensure_ascii=False)
    except OSError:
        pass
    with _mix_lock:
        _mix[tid] = {"state": "done", "names": names, "from": src}
    _say(f"MIXER {title} - {artist}: {', '.join(names) if names else '(none credited on MusicBrainz)'}")


_v15_snapshot = v15.snapshot5


def snapshot6():
    out = _v15_snapshot()
    t = out.get("track")
    if t:
        with _mix_lock:
            m = _mix.get(t.get("tid"))
        t["mixer"] = dict(m) if m else {"state": "pending", "names": [], "from": None}
    return out


_v15_handle = v15.handle5


def handle6(ev):
    _v15_handle(ev)
    try:
        if ev.get("event") == "track" and ev.get("pandoraTrackId"):
            tid = ev["pandoraTrackId"]
            with _mix_lock:
                _mix[tid] = {"state": "pending", "names": [], "from": None}
            threading.Thread(target=_mix_job, args=(tid,), daemon=True).start()
    except Exception as e:
        print("v16 event error:", e, flush=True)


class Handler(v13.Handler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/art":
            url = (urllib.parse.parse_qs(parsed.query).get("u") or [""])[0]
            got = fetch_art(url)
            if not got:
                return self._send(404, b"not available", "text/plain")
            ctype, data = got
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()


def main():
    if not os.path.exists(PAGE_FILE):
        raise SystemExit(f"player_v16.html is missing (looked for {PAGE_FILE}). Put it in the same folder as this file.")
    v13.PAGE_FILE = PAGE_FILE
    base.snapshot = snapshot6
    base.handle = handle6
    v12._load_new()
    if not base.SPOTIFY_ON:
        print("Spotify not logged in yet, so only logging. To enable playback run:  python spotify_player_v11.py login", flush=True)
    lib.start(base.SPOTIFY_ON)
    print("Playlist changes from the page:", "allowed" if pls.can_write() else "NOT yet allowed (run: python spotify_playlists_v12.py login)", flush=True)
    print("Last.fm:", "set up" if lfm.configured() else "not set up (username / API key missing)", "|",
          "can love tracks" if lfm.can_love() else "cannot love yet (run: python lastfm_v12.py auth)", "|",
          "scrobbles from /scrobble are SENT" if v13.scrobble_enabled() else "scrobbles from /scrobble are only logged", flush=True)
    print(f"Similar artists saved so far: {len(v15._sim)} artists ({v15.SIMILAR_FILE})", flush=True)
    print(f"Receiver v16 listening on http://{base.HOST}:{base.PORT}  (events -> {base.EVENTS_FILE})", flush=True)
    print(f"Player page: http://{base.HOST}:{base.PORT}/player", flush=True)
    ThreadingHTTPServer((base.HOST, base.PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
