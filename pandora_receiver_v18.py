#!/usr/bin/env python3
"""
pandora_receiver_v18.py = v16 + station tuning modes + cover detection.

    python pandora_receiver_v18.py

Keep pandora_receiver_v16.py (and v11 to v15, wiki_v14.py, the helper files) in the same folder.
Needs player_v18.html. Use it with pandora_userscript_v13.user.js. Open http://127.0.0.1:8765/player

Station tuning   the userscript reads Pandora's tuning options (Crowd Faves, Deep Cuts, ...) from the Now Playing page and
                 reports them; the player page lists them and a click is passed back to the userscript ("mode:<name>").
Cover detection  MusicBrainz: the song's recording belongs to a "work" (the composition). If the earliest recording of that work
                 is by someone else, and this artist is not a credited writer of it, the song is shown as a cover with the
                 original's artist and year. Remembered in cover_cache_v1.json. It is a best guess: live takes, remixes and
                 demos are ignored, but MusicBrainz may not know every work.
"""
import io
import json
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import ThreadingHTTPServer

import pandora_receiver_v11 as base
import pandora_receiver_v13 as v13
import pandora_receiver_v16 as v16
import spotify_player_v11 as sp

PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "player_v18.html")
COVER_CACHE_FILE = "cover_cache_v1.json"


def _say(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------- station tuning modes ----------
_modes = {"station": None, "list": [], "t": 0.0}
_modes_lock = threading.Lock()


def set_modes(ev):
    items = []
    for m in ev.get("modes") or []:
        if isinstance(m, dict) and str(m.get("name") or "").strip():
            items.append({"name": str(m["name"]).strip()[:40], "active": bool(m.get("active"))})
    if not items:
        return
    with _modes_lock:
        _modes.update(station=ev.get("station"), list=items[:12], t=time.time())


def do_mode(name):
    name = str(name or "").strip()
    if not name:
        return
    with _modes_lock:
        known = [m["name"] for m in _modes["list"]]
    match = next((n for n in known if n.lower() == name.lower()), name)
    base.send_pandora("mode:" + match)
    with _modes_lock:                                    # show the choice at once; the page's own report corrects it a moment later
        for m in _modes["list"]:
            m["active"] = m["name"] == match


# ---------- cover detection (MusicBrainz) ----------
try:
    with open(COVER_CACHE_FILE, encoding="utf-8") as f:
        _ccache = json.load(f)
except Exception:
    _ccache = {}
_cover = {}                       # pandoraTrackId -> {"state": ..., "isCover": bool, "original": {...} | None}
_cover_lock = threading.Lock()
_NOT_STUDIO = re.compile(r"\b(live|remix|demo|acoustic|instrumental|karaoke|session|unplugged|mix|edit|version|cover|tribute)\b", re.I)


def _year(date):
    m = re.match(r"^(\d{4})", date or "")
    return int(m.group(1)) if m else None


def _credit(rec):
    return " ".join(c.get("name", "") + (c.get("joinphrase") or "") for c in rec.get("artist-credit", []) if isinstance(c, dict))


def _credit_names(rec):
    return [(c.get("artist") or {}).get("name") or c.get("name", "") for c in rec.get("artist-credit", []) if isinstance(c, dict)]


def find_cover(title, artist, album):
    """{'isCover': bool, 'original': {'artist','title','year'} | None}"""
    q = f'recording:"{base.clean(title)}" AND artist:"{artist}"'
    data = base.mb_get("recording", {"query": q, "fmt": "json", "limit": 25})
    want_t, want_a = sp.norm(title), sp.norm(artist)
    cands = []
    for r in data.get("recordings", []):
        if sp.norm(r.get("title", "")) == want_t and want_a in sp.norm(_credit(r)):
            cands.append(r)
    cands.sort(key=lambda r: r.get("first-release-date") or "9999")
    work = None
    for r in cands[:3]:
        info = base.mb_get(f"recording/{r['id']}", {"inc": "work-rels", "fmt": "json"})
        for rel in info.get("relations", []) or []:
            if rel.get("type") == "performance" and (rel.get("work") or {}).get("id"):
                work = rel["work"]
                break
        if work:
            break
    if not work:
        return {"isCover": False, "original": None}
    mine = _year(cands[0].get("first-release-date")) if cands else None
    wi = base.mb_get(f"work/{work['id']}", {"inc": "artist-rels", "fmt": "json"})
    writers = {sp.norm((rel.get("artist") or {}).get("name", "")) for rel in wi.get("relations", []) or []
               if rel.get("type") in ("writer", "composer", "lyricist", "arranger") and rel.get("artist")}
    if want_a in writers:
        return {"isCover": False, "original": None}       # the writer's own recording is not a cover
    recs = base.mb_get("recording", {"work": work["id"], "inc": "artist-credits", "limit": 100, "fmt": "json"}).get("recordings", [])
    dated = []
    for r in recs:
        y = _year(r.get("first-release-date"))
        if y is None or _NOT_STUDIO.search(r.get("disambiguation") or "") or _NOT_STUDIO.search(r.get("title") or "") and sp.norm(r.get("title", "")) != want_t:
            continue
        dated.append((r.get("first-release-date"), r))
    if not dated:
        return {"isCover": False, "original": None}
    dated.sort(key=lambda x: x[0])
    first = dated[0][1]
    names = [sp.norm(n) for n in _credit_names(first)] + [sp.norm(_credit(first))]
    if any(n and (n == want_a or n in want_a or want_a in n) for n in names) or "various" in sp.norm(_credit(first)) or "unknown" in sp.norm(_credit(first)):
        return {"isCover": False, "original": None}
    oy = _year(first.get("first-release-date"))
    if mine is not None and oy is not None and oy >= mine:
        return {"isCover": False, "original": None}
    return {"isCover": True, "original": {"artist": _credit(first).strip(), "title": first.get("title") or title, "year": oy}}


def _cover_job(tid):
    with base.lock:
        t = dict(base.tracks.get(tid) or {})
    title, artist, album = t.get("title"), t.get("artist"), t.get("album")
    if not (title and artist):
        return
    key = f"{base.clean(title)}|{artist}".lower()
    if key in _ccache:
        with _cover_lock:
            _cover[tid] = dict(_ccache[key], state="done")
        return
    end = time.time() + 60                                 # the producer and mixer lookups go first on the one-request-a-second MusicBrainz line
    while time.time() < end:
        with v16._mix_lock:
            if (v16._mix.get(tid) or {}).get("state") != "pending":
                break
        time.sleep(0.5)
    try:
        res = find_cover(title, artist, album)
    except Exception as e:
        _say(f"COVER lookup failed for {title} - {artist}: {e}")
        with _cover_lock:
            _cover[tid] = {"state": "error", "isCover": False, "original": None}
        return
    _ccache[key] = res
    try:
        with open(COVER_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_ccache, f, indent=1, ensure_ascii=False)
    except OSError:
        pass
    with _cover_lock:
        _cover[tid] = dict(res, state="done")
    o = res["original"]
    _say(f"COVER {title} - {artist}: " + (f"cover of {o['artist']} ({o['year']})" if res["isCover"] else "no cover detected"))


# ---------- wiring ----------
_v16_snapshot = v16.snapshot6


def snapshot7():
    out = _v16_snapshot()
    t = out.get("track")
    if t:
        with _cover_lock:
            c = _cover.get(t.get("tid"))
        t["cover"] = dict(c) if c else {"state": "pending", "isCover": False, "original": None}
    with _modes_lock:
        out["modes"] = {"station": _modes["station"], "list": [dict(m) for m in _modes["list"]]} if _modes["list"] else None
    return out


_v16_handle = v16.handle6


def handle7(ev):
    _v16_handle(ev)
    try:
        if ev.get("event") == "modes":
            set_modes(ev)
        elif ev.get("event") == "track" and ev.get("pandoraTrackId"):
            tid = ev["pandoraTrackId"]
            with _cover_lock:
                _cover[tid] = {"state": "pending", "isCover": False, "original": None}
            threading.Thread(target=_cover_job, args=(tid,), daemon=True).start()
    except Exception as e:
        print("v18 event error:", e, flush=True)


class Handler(v16.Handler):
    def do_POST(self):
        if urllib.parse.urlparse(self.path).path == "/cmd":
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
            if isinstance(body, dict) and body.get("cmd") == "mode":
                do_mode(body.get("name"))
                return self._reply(base.snapshot())
            self.rfile = io.BytesIO(raw)
        super().do_POST()


def main():
    if not os.path.exists(PAGE_FILE):
        raise SystemExit(f"player_v18.html is missing (looked for {PAGE_FILE}). Put it in the same folder as this file.")
    v16.PAGE_FILE = PAGE_FILE
    base.snapshot = snapshot7
    base.handle = handle7
    # v16.main() installs its own snapshot/handle and starts the server with v16's handler, so do the same set-up here instead
    v13.PAGE_FILE = PAGE_FILE
    v16.v12._load_new()
    if not base.SPOTIFY_ON:
        print("Spotify not logged in yet, so only logging. To enable playback run:  python spotify_player_v11.py login", flush=True)
    v16.lib.start(base.SPOTIFY_ON)
    print("Playlist changes from the page:", "allowed" if v16.pls.can_write() else "NOT yet allowed (run: python spotify_playlists_v12.py login)", flush=True)
    print("Last.fm:", "set up" if v16.lfm.configured() else "not set up (username / API key missing)", "|",
          "can love tracks" if v16.lfm.can_love() else "cannot love yet (run: python lastfm_v12.py auth)", "|",
          "scrobbles from /scrobble are SENT" if v13.scrobble_enabled() else "scrobbles from /scrobble are only logged", flush=True)
    print(f"Receiver v18 listening on http://{base.HOST}:{base.PORT}  (events -> {base.EVENTS_FILE})", flush=True)
    print(f"Player page: http://{base.HOST}:{base.PORT}/player", flush=True)
    ThreadingHTTPServer((base.HOST, base.PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
