#!/usr/bin/env python3
"""
pandora_receiver_v15.py = v14 + saved similar artists.

    python pandora_receiver_v15.py

Imports the older receivers (v11 to v14): keep them, wiki_v14.py and the other helper files in the same folder.
Needs player_v15.html. Use it with pandora_userscript_v12.user.js. Open http://127.0.0.1:8765/player

Similar artists   every list Pandora's page gives is remembered per artist in similar_cache_v1.json. When a song starts, its
                  artist's saved list shows at once (Pandora's own list only appears about 3 s into a song), and it also fills in
                  when Pandora's page cannot be read for a song. A fresh list from Pandora replaces the saved one.
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
import pandora_receiver_v14 as v14
import spotify_playlists_v12 as pls

PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "player_v15.html")
SIMILAR_FILE = "similar_cache_v1.json"

try:
    with open(SIMILAR_FILE, encoding="utf-8") as f:
        _sim = json.load(f)
except Exception:
    _sim = {}
_sim_lock = threading.Lock()


def _skey(artist):
    return re.sub(r"\s+", " ", (artist or "").strip().lower())


def remember_similar(tid):
    """Save the similar-artists list Pandora gave for this track's artist (replacing an older list)."""
    with base.lock:
        t = dict(base.tracks.get(tid) or {})
    names, key = [n for n in (t.get("similar") or []) if n], _skey(t.get("artist"))
    if not names or not key:
        return
    with _sim_lock:
        if (_sim.get(key) or {}).get("names") == names:
            return
        _sim[key] = {"artist": t.get("artist"), "names": names, "t": datetime.now().isoformat(timespec="seconds")}
        try:
            with open(SIMILAR_FILE, "w", encoding="utf-8") as f:
                json.dump(_sim, f, indent=1, ensure_ascii=False)
        except OSError:
            pass


def with_similar(view):
    if view:
        if view.get("similar"):
            view["similarFrom"] = "Pandora"
        else:
            saved = (_sim.get(_skey(view.get("artist"))) or {}).get("names")
            view["similar"], view["similarFrom"] = (list(saved), "saved") if saved else ([], None)
    return view


_v14_snapshot = v14.snapshot4


def snapshot5():
    out = _v14_snapshot()
    cur = (out.get("track") or {}).get("tid")
    if cur:
        remember_similar(cur)
    with_similar(out.get("track"))
    for h in out.get("history") or []:
        with_similar(h)
    return out


_v14_handle = v14.handle4


def handle5(ev):
    _v14_handle(ev)
    try:
        if ev.get("event") == "details" and ev.get("pandoraTrackId"):
            remember_similar(ev["pandoraTrackId"])
    except Exception as e:
        print("v15 event error:", e, flush=True)


def main():
    if not os.path.exists(PAGE_FILE):
        raise SystemExit(f"player_v15.html is missing (looked for {PAGE_FILE}). Put it in the same folder as this file.")
    v13.PAGE_FILE = PAGE_FILE
    base.snapshot = snapshot5
    base.handle = handle5
    v12._load_new()
    if not base.SPOTIFY_ON:
        print("Spotify not logged in yet, so only logging. To enable playback run:  python spotify_player_v11.py login", flush=True)
    lib.start(base.SPOTIFY_ON)
    print("Playlist changes from the page:", "allowed" if pls.can_write() else "NOT yet allowed (run: python spotify_playlists_v12.py login)", flush=True)
    print("Last.fm:", "set up" if lfm.configured() else "not set up (username / API key missing)", "|",
          "can love tracks" if lfm.can_love() else "cannot love yet (run: python lastfm_v12.py auth)", "|",
          "scrobbles from /scrobble are SENT" if v13.scrobble_enabled() else "scrobbles from /scrobble are only logged", flush=True)
    print(f"Similar artists saved so far: {len(_sim)} artists ({SIMILAR_FILE})", flush=True)
    print(f"Receiver v15 listening on http://{base.HOST}:{base.PORT}  (events -> {base.EVENTS_FILE})", flush=True)
    print(f"Player page: http://{base.HOST}:{base.PORT}/player", flush=True)
    ThreadingHTTPServer((base.HOST, base.PORT), v13.Handler).serve_forever()


if __name__ == "__main__":
    main()
