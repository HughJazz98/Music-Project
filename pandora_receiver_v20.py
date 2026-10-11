#!/usr/bin/env python3
"""
pandora_receiver_v20 = v19 + tempo / key / time signature (GetSongBPM).

    python pandora_receiver_v20.py

Keep pandora_receiver_v19.py, v18, v16 (and v11 to v15, wiki_v14.py, the helper files) in the same folder.
Needs player_v20.html. Use it with pandora_userscript_v14.user.js. Open http://127.0.0.1:8765/player

Tempo   GetSongBPM (https://getsongbpm.com/api) has a free API: register at that page (it asks for a link back to getsongbpm.com,
        which the player page's footer has), then add to mirror_config.json:   "getsongbpm": {"api_key": "YOUR KEY"}
        Without a key the page says so and nothing else is affected. Answers are remembered in bpm_cache_v1.json.
        (Spotify's own tempo data is no longer open to new apps, which is why it is not used.)
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer

import pandora_receiver_v11 as base
import pandora_receiver_v13 as v13
import pandora_receiver_v18 as v18
import pandora_receiver_v19 as v19
import spotify_player_v11 as sp

PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "player_v20.html")
BPM_CACHE_FILE = "bpm_cache_v1.json"
API = "https://api.getsong.co/"


def _say(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


try:
    with open(BPM_CACHE_FILE, encoding="utf-8") as f:
        _bcache = json.load(f)
except Exception:
    _bcache = {}
_tempo = {}                      # pandoraTrackId -> {"state": "pending"|"done"|"notfound"|"nokey"|"error", ...}
_tempo_lock = threading.Lock()
_last_call = [0.0]


def api_key():
    try:
        with open("mirror_config.json", encoding="utf-8") as f:
            return ((json.load(f).get("getsongbpm") or {}).get("api_key") or "").strip() or None
    except Exception:
        return None


def _get(params):
    wait = 0.5 - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    url = API + "search/?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "PandoraMirror/0.20 (personal use)", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.load(r)
    finally:
        _last_call[0] = time.time()


def _items(data):
    s = (data or {}).get("search")
    if isinstance(s, dict):
        return [s]
    return [x for x in (s or []) if isinstance(x, dict)] if isinstance(s, list) else []


def find_tempo(title, artist, key):
    want_t, want_a = sp.norm(base.clean(title)), sp.norm(artist)
    tries = [{"api_key": key, "type": "both", "lookup": f"song:{base.clean(title)} artist:{artist}"},
             {"api_key": key, "type": "song", "lookup": base.clean(title)}]
    for params in tries:
        for it in _items(_get(params)):
            a = it.get("artist")
            name = a.get("name") if isinstance(a, dict) else a
            if sp.norm(it.get("title", "")) == want_t and want_a and want_a in sp.norm(name or ""):
                try:
                    bpm = int(float(it.get("tempo")))
                except (TypeError, ValueError):
                    bpm = None
                return {"bpm": bpm, "timeSig": it.get("time_sig") or None, "key": it.get("key_of") or None,
                        "openKey": it.get("open_key") or None, "url": it.get("uri") or None}
    return None


def _tempo_job(tid):
    with base.lock:
        t = dict(base.tracks.get(tid) or {})
    title, artist = t.get("title"), t.get("artist")
    if not (title and artist):
        return
    cache_key = f"{base.clean(title)}|{artist}".lower()
    if cache_key in _bcache:
        with _tempo_lock:
            _tempo[tid] = dict(_bcache[cache_key], state="done" if _bcache[cache_key].get("bpm") else "notfound")
        return
    key = api_key()
    if not key:
        with _tempo_lock:
            _tempo[tid] = {"state": "nokey"}
        return
    try:
        res = find_tempo(title, artist, key)
    except urllib.error.HTTPError as e:
        _say(f"TEMPO lookup failed for {title} - {artist}: HTTP {e.code}")
        with _tempo_lock:
            _tempo[tid] = {"state": "error", "message": "GetSongBPM answered HTTP %s%s" % (e.code, " (check the API key)" if e.code in (401, 403) else "")}
        return
    except Exception as e:
        _say(f"TEMPO lookup failed for {title} - {artist}: {e}")
        with _tempo_lock:
            _tempo[tid] = {"state": "error", "message": "GetSongBPM could not be reached"}
        return
    _bcache[cache_key] = res or {}
    try:
        with open(BPM_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_bcache, f, indent=1, ensure_ascii=False)
    except OSError:
        pass
    with _tempo_lock:
        _tempo[tid] = dict(res, state="done") if res and res.get("bpm") else {"state": "notfound"}
    _say(f"TEMPO {title} - {artist}: " + (f"{res['bpm']} BPM, {res.get('timeSig') or '?'}, key {res.get('key') or '?'}" if res else "(not on GetSongBPM)"))


_v19_snapshot = v19.snapshot8


def snapshot9():
    out = _v19_snapshot()
    t = out.get("track")
    if t:
        with _tempo_lock:
            m = _tempo.get(t.get("tid"))
        t["tempo"] = dict(m) if m else {"state": "pending"}
    return out


_v18_handle = v18.handle7


def handle9(ev):
    _v18_handle(ev)
    try:
        if ev.get("event") == "track" and ev.get("pandoraTrackId"):
            tid = ev["pandoraTrackId"]
            with _tempo_lock:
                _tempo[tid] = {"state": "pending"}
            threading.Thread(target=_tempo_job, args=(tid,), daemon=True).start()
    except Exception as e:
        print("v20 event error:", e, flush=True)


def main():
    if not os.path.exists(PAGE_FILE):
        raise SystemExit(f"player_v20.html is missing (looked for {PAGE_FILE}). Put it in the same folder as this file.")
    base.snapshot = snapshot9
    base.handle = handle9
    v13.PAGE_FILE = PAGE_FILE
    v18.v16.v12._load_new()
    if not base.SPOTIFY_ON:
        print("Spotify not logged in yet, so only logging. To enable playback run:  python spotify_player_v11.py login", flush=True)
    v18.v16.lib.start(base.SPOTIFY_ON)
    print("Playlist changes from the page:", "allowed" if v18.v16.pls.can_write() else "NOT yet allowed (run: python spotify_playlists_v12.py login)", flush=True)
    print("Last.fm:", "set up" if v18.v16.lfm.configured() else "not set up (username / API key missing)", "|",
          "can love tracks" if v18.v16.lfm.can_love() else "cannot love yet (run: python lastfm_v12.py auth)", "|",
          "scrobbles from /scrobble are SENT" if v13.scrobble_enabled() else "scrobbles from /scrobble are only logged", flush=True)
    print("GetSongBPM tempo lookups:", "on" if api_key() else 'off (add "getsongbpm": {"api_key": "..."} to mirror_config.json)', flush=True)
    print(f"Receiver v20 listening on http://{base.HOST}:{base.PORT}  (events -> {base.EVENTS_FILE})", flush=True)
    print(f"Player page: http://{base.HOST}:{base.PORT}/player", flush=True)
    ThreadingHTTPServer((base.HOST, base.PORT), v19.Handler).serve_forever()


if __name__ == "__main__":
    main()
