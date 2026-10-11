#!/usr/bin/env python3
"""
pandora_receiver_v14.py = v13 + Wikipedia fallback for release date and producer, MM/DD/YYYY, the full-screen player page, and a history list.

    python pandora_receiver_v14.py

Imports the older receivers (v11, v12, v13), so keep all of those files in the same folder, plus wiki_v14.py and player_v14.html.
Use it with pandora_userscript_v11.user.js. Open http://127.0.0.1:8765/player

Release date  MusicBrainz first. When MusicBrainz has no date, or only a year, Wikipedia is asked (the song's own page for a
              single, the album's page for an album). The Track details row says where the date came from.
Producer      the song's own MusicBrainz credit if there is one, otherwise the producer(s) in the info box of the ALBUM's
              Wikipedia page. For a single that is the album it is on (found from the single's Wikipedia page, or from
              MusicBrainz's list of albums the song appears on).
Both lookups run in the background and are kept in wiki_cache_v1.json, so each song is looked up once.
"""
import json
import os
import re
import threading
import time
from datetime import datetime
from http.server import ThreadingHTTPServer

import lastfm_v12 as lfm
import mirror_library_v11 as lib
import pandora_receiver_v11 as base
import pandora_receiver_v12 as v12
import pandora_receiver_v13 as v13
import spotify_playlists_v12 as pls
import spotify_player_v11 as sp
import wiki_v14 as wiki

PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "player_v14.html")
WIKI_CACHE_FILE = "wiki_cache_v1.json"
HISTORY_LIMIT = 50


def _say(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


try:
    with open(WIKI_CACHE_FILE, encoding="utf-8") as f:
        _wcache = json.load(f)
except Exception:
    _wcache = {}
_wiki = {}                  # pandoraTrackId -> {"state": "pending" | "done" | "error", "data": {...}}
_wlock = threading.Lock()


def mb_parent_album(title, artist):
    """The earliest real studio album MusicBrainz lists the song on (for a single whose Wikipedia page names no album)."""
    q = f'recording:"{base.clean(title)}" AND artist:"{artist}"'
    data = base.mb_get("recording", {"query": q, "fmt": "json", "limit": 25})
    want_t, want_a = sp.norm(title), sp.norm(artist)
    best = None
    for r in data.get("recordings", []):
        credit = " ".join(c.get("name", "") for c in r.get("artist-credit", []) if isinstance(c, dict))
        if sp.norm(r.get("title", "")) != want_t or want_a not in sp.norm(credit):
            continue
        for rel in r.get("releases", []):
            g = rel.get("release-group") or {}
            if g.get("primary-type") == "Album" and not g.get("secondary-types") and rel.get("title"):
                d = rel.get("date") or "9999"
                if best is None or d < best[0]:
                    best = (d, rel["title"])
    return best[1] if best else None


def _wiki_job(tid):
    with base.lock:
        t = dict(base.tracks.get(tid) or {})
    title, artist, album = t.get("title"), t.get("artist"), t.get("album")
    if not (title and artist):
        return
    end = time.time() + 20                      # let the MusicBrainz date/genre lookup go first
    while time.time() < end:
        with base.lock:
            if (base.tracks.get(tid) or {}).get("enriched"):
                break
        time.sleep(0.4)
    key = f"{base.clean(title)}|{artist}|{base.clean(album)}".lower()
    data = _wcache.get(key)
    if data is None:
        try:
            data = wiki.lookup(title, artist, album, parent_album=lambda: mb_parent_album(title, artist))
        except Exception as e:
            _say(f"WIKIPEDIA lookup failed for {title} - {artist}: {e}")
            with _wlock:
                _wiki[tid] = {"state": "error", "data": {}}
            return
        _wcache[key] = data
        try:
            with open(WIKI_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(_wcache, f, indent=1, ensure_ascii=False)
        except OSError:
            pass
        _say(f"WIKIPEDIA {title} - {artist}: released {data.get('released') or data.get('albumReleased') or '?'}"
             f" ({data.get('releasedFrom') or 'album date only'}); producers {', '.join(data.get('producers') or []) or '(none found)'}"
             f"; page {data.get('page') or '(none found)'}")
    with _wlock:
        _wiki[tid] = {"state": "done", "data": data}


# ---------- putting MusicBrainz and Wikipedia together ----------
def best_release(mb, w):
    """(date, where it came from). MusicBrainz when it has a full date; otherwise Wikipedia's, unless that is clearly another year's."""
    mb = mb or None
    if mb and len(mb) >= 10:
        return mb, "MusicBrainz"
    if w:
        cand, frm = w.get("released"), w.get("releasedFrom")
        if cand and (frm == "song" or not mb or mb[:4] == cand[:4]) and (not mb or len(cand) > len(mb) or frm == "song"):
            return cand, "Wikipedia, " + ("song page" if frm == "song" else "album page")
        if not cand and not mb and w.get("albumReleased"):
            return w["albumReleased"], "Wikipedia, the album's date (the single's own date was not found)"
    return mb, ("MusicBrainz" if mb else None)


def merge_view(view):
    """Release date / year / producer for one track view, using both sources."""
    if not view:
        return view
    with _wlock:
        w = _wiki.get(view.get("tid")) or {}
    wd = w.get("data") or {}
    rel, frm = best_release(view.get("release"), wd if w.get("state") == "done" else None)
    view["release"], view["releaseFrom"] = rel, frm
    m = re.match(r"^(\d{4})", rel or "")
    view["year"] = m.group(1) if m else None
    wstate = w.get("state", "pending")
    view["wikiPage"] = wiki.page_url(wd["page"]) if wd.get("page") else None
    p = view.get("producer")
    if p is not None:
        if p.get("state") == "done" and p.get("names"):
            p["label"] = "MusicBrainz, credit for this song" if p.get("from") == "recording" else "MusicBrainz, album credit"
        elif wstate == "done" and wd.get("producers"):
            p.update(state="done", names=list(wd["producers"]),
                     label="Wikipedia, " + (f"album: {wd['albumTitle']}" if wd.get("producersFrom") == "album" and wd.get("albumTitle") else "song page"))
        elif p.get("state") == "done" and wstate in ("done", "error"):
            p["label"] = ""
            p["wikiError"] = wstate == "error"
        else:
            p["state"] = "pending" if p.get("state") != "error" or wstate == "pending" else "done"
    return view


_v13_snapshot = v13.snapshot3


def snapshot4():
    out = _v13_snapshot()
    merge_view(out.get("track"))
    cur = out["track"]["tid"] if out.get("track") else None
    tids = [t for t in base.history[-HISTORY_LIMIT - 1:] if t != cur]
    out["history"] = [merge_view(v13._decorate(base._track_view(t))) for t in reversed(tids[-HISTORY_LIMIT:])]
    return out


_v13_handle = v13.handle3


def handle4(ev):
    _v13_handle(ev)
    try:
        if ev.get("event") == "track" and ev.get("pandoraTrackId"):
            tid = ev["pandoraTrackId"]
            with _wlock:
                _wiki[tid] = {"state": "pending", "data": {}}
            threading.Thread(target=_wiki_job, args=(tid,), daemon=True).start()
    except Exception as e:
        print("v14 event error:", e, flush=True)


def main():
    if not os.path.exists(PAGE_FILE):
        raise SystemExit(f"player_v14.html is missing (looked for {PAGE_FILE}). Put it in the same folder as this file.")
    v13.PAGE_FILE = PAGE_FILE
    base.snapshot = snapshot4
    base.handle = handle4
    v12._load_new()
    if not base.SPOTIFY_ON:
        print("Spotify not logged in yet, so only logging. To enable playback run:  python spotify_player_v11.py login", flush=True)
    lib.start(base.SPOTIFY_ON)
    print("Playlist changes from the page:", "allowed" if pls.can_write() else "NOT yet allowed (run: python spotify_playlists_v12.py login)", flush=True)
    print("Last.fm:", "set up" if lfm.configured() else "not set up (username / API key missing)", "|",
          "can love tracks" if lfm.can_love() else "cannot love yet (run: python lastfm_v12.py auth)", "|",
          "scrobbles from /scrobble are SENT" if v13.scrobble_enabled() else "scrobbles from /scrobble are only logged", flush=True)
    print(f"Receiver v14 listening on http://{base.HOST}:{base.PORT}  (events -> {base.EVENTS_FILE})", flush=True)
    print(f"Player page: http://{base.HOST}:{base.PORT}/player", flush=True)
    ThreadingHTTPServer((base.HOST, base.PORT), v13.Handler).serve_forever()


if __name__ == "__main__":
    main()
