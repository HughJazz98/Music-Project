#!/usr/bin/env python3
"""
pandora_receiver_v13.py = v12 + a new player page, working station switching, producer credits and a scrobble hook.

    python pandora_receiver_v13.py

Like v12 this file does not copy the older receivers, it imports them: keep pandora_receiver_v11.py, pandora_receiver_v12.py,
spotify_player_v11.py, spotify_playlists_v12.py, lastfm_v12.py and mirror_library_v11.py in the same folder. Also needs
player_v13.html (the page) next to it, and the new userscript pandora_userscript_v10.user.js in place of v9.
Open http://127.0.0.1:8765/player

What is new
  Player page   player_v13.html is read from disk on every page load, so you can edit the design and just refresh the browser.
  Stations      the station's address (from stations_seen.json) is sent along with its name, so the userscript can go there
                even when Pandora's page does not list the station.
  Producer      looked up on MusicBrainz in the background (recording credits first, then the album's) and kept in
                producer_cache_v1.json. Shows "not credited" when MusicBrainz has no producer for the song.
  Scrobbles     POST http://127.0.0.1:8765/scrobble is the place for a scrobble detector to report to (see SCROBBLE below).
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

import lastfm_v12 as lfm
import mirror_library_v11 as lib
import pandora_receiver_v11 as base
import pandora_receiver_v12 as v12
import spotify_playlists_v12 as pls
import spotify_player_v11 as sp

PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "player_v13.html")
PRODUCER_CACHE_FILE = "producer_cache_v1.json"
SCROBBLE_LOG = "scrobble_events.jsonl"


def _say(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------- producer credit (MusicBrainz) ----------
try:
    with open(PRODUCER_CACHE_FILE, encoding="utf-8") as f:
        _pcache = json.load(f)
except Exception:
    _pcache = {}
_prod = {}                      # pandoraTrackId -> {"state": "pending" | "done" | "error", "names": [...], "from": ...}
_prod_lock = threading.Lock()


def _producer_names(info):
    """Producers named in the relationships of a MusicBrainz recording/release (executive producers are left out)."""
    names = []
    for rel in info.get("relations", []) or []:
        if rel.get("type") != "producer":
            continue
        if "executive" in [a.lower() for a in rel.get("attributes", []) or []]:
            continue
        n = (rel.get("artist") or {}).get("name")
        if n and n not in names:
            names.append(n)
    return names


def find_producers(title, artist, album):
    """([names], 'recording' | 'release' | None). A few MusicBrainz calls; the same song is often listed several times and only
    some of the entries carry credits, so up to three recordings are tried before the album's own release credits."""
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
        names = _producer_names(base.mb_get(f"recording/{r['id']}", {"inc": "artist-rels", "fmt": "json"}))
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
        names = _producer_names(base.mb_get(f"release/{rid}", {"inc": "artist-rels", "fmt": "json"}))
        if names:
            return names, "release"
    return [], None


def _producer_job(tid):
    with base.lock:
        t = dict(base.tracks.get(tid) or {})
    title, artist, album = t.get("title"), t.get("artist"), t.get("album")
    if not (title and artist):
        return
    # let the date/genre lookup (v11) go first: both use the same one-request-a-second MusicBrainz line
    end = time.time() + 20
    while time.time() < end:
        with base.lock:
            if (base.tracks.get(tid) or {}).get("enriched"):
                break
        time.sleep(0.4)
    key = f"{base.clean(title)}|{artist}|{base.clean(album)}".lower()
    if key in _pcache:
        with _prod_lock:
            _prod[tid] = {"state": "done", "names": _pcache[key]["names"], "from": _pcache[key].get("from")}
        return
    try:
        names, src = find_producers(title, artist, album)
    except Exception as e:
        _say(f"PRODUCER lookup failed for {title} - {artist}: {e}")
        with _prod_lock:
            _prod[tid] = {"state": "error", "names": [], "from": None}
        return
    _pcache[key] = {"names": names, "from": src}
    try:
        with open(PRODUCER_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_pcache, f, indent=1, ensure_ascii=False)
    except OSError:
        pass
    with _prod_lock:
        _prod[tid] = {"state": "done", "names": names, "from": src}
    _say(f"PRODUCER {title} - {artist}: {', '.join(names) if names else '(none credited on MusicBrainz)'}")


# ---------- scrobbles ----------
# SCROBBLE: where a scrobble detector plugs in. Whatever watches the playback source (the Spotify web player, a desktop app,
# ...) sends one POST per play, e.g. from a userscript or a script:
#
#   POST http://127.0.0.1:8765/scrobble
#   {"type": "scrobble", "artist": "...", "title": "...", "album": "...", "timestamp": 1760100000, "durationSec": 215}
#   {"type": "nowplaying", "artist": "...", "title": "..."}              (optional: Last.fm's "listening now" line)
#
# "timestamp" is when the song STARTED (unix seconds); it defaults to now. Every request is written to scrobble_events.jsonl.
# Nothing is sent to Last.fm until you put   "scrobble_enabled": true   in the "lastfm" part of mirror_config.json
# (and have run  python lastfm_v12.py auth  once). Leave it off while Pano Scrobbler is also scrobbling, or every play counts twice.
_scrobbled = set()
last_scrobble = {"t": None, "ok": None, "message": None, "sent": False}


def scrobble_enabled():
    return (lfm._full_config().get("lastfm") or {}).get("scrobble_enabled") is True


def do_scrobble(body):
    kind = body.get("type") or "scrobble"
    artist, title = str(body.get("artist") or "").strip(), str(body.get("title") or "").strip()
    album = str(body.get("album") or "").strip()
    if kind not in ("scrobble", "nowplaying") or not artist or not title:
        return {"ok": False, "sent": False, "message": "needs type (scrobble or nowplaying), artist and title"}
    try:
        ts = int(body.get("timestamp") or time.time())
    except (TypeError, ValueError):
        return {"ok": False, "sent": False, "message": "timestamp must be unix seconds"}
    with open(SCROBBLE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": datetime.now().isoformat(timespec="seconds"), "type": kind, "artist": artist, "title": title,
                            "album": album, "timestamp": ts, "durationSec": body.get("durationSec")}, ensure_ascii=False) + "\n")
    out = {"ok": True, "sent": False, "message": "Logged only (scrobble_enabled is not true in mirror_config.json)"}
    if scrobble_enabled():
        if not lfm.can_love():
            out = {"ok": False, "sent": False, "message": "Not logged in to Last.fm for writing yet. Run: python lastfm_v12.py auth"}
        elif kind == "scrobble" and (ts < time.time() - 14 * 86400 or ts > time.time() + 300):
            out = {"ok": False, "sent": False, "message": "Last.fm only takes plays from the last 14 days"}
        elif kind == "scrobble" and (artist.lower(), title.lower(), ts) in _scrobbled:
            out = {"ok": True, "sent": False, "message": "Already sent this one"}
        else:
            params = {"method": "track.scrobble" if kind == "scrobble" else "track.updateNowPlaying",
                      "sk": lfm._s("session_key")}
            if kind == "scrobble":
                params.update({"artist[0]": artist, "track[0]": title, "timestamp[0]": ts})
                if album:
                    params["album[0]"] = album
            else:
                params.update({"artist": artist, "track": title})
                if album:
                    params["album"] = album
                if body.get("durationSec"):
                    params["duration"] = int(body["durationSec"])
            try:
                res = lfm._call(params, sign=True, post=True)
                if kind == "scrobble":
                    _scrobbled.add((artist.lower(), title.lower(), ts))
                    attr = (res.get("scrobbles") or {}).get("@attr") or {}
                    if str(attr.get("ignored", "0")) not in ("0", ""):
                        out = {"ok": False, "sent": True, "message": "Last.fm ignored the scrobble: " + json.dumps(res.get("scrobbles"))[:300]}
                    else:
                        with lfm._lock:
                            lfm._cache.pop(lfm._ckey(artist, title), None)       # so the page asks again for the new count
                        out = {"ok": True, "sent": True, "message": f'Scrobbled "{title}"'}
                else:
                    out = {"ok": True, "sent": True, "message": f'Now playing sent: "{title}"'}
            except lfm.LastfmError as e:
                out = {"ok": False, "sent": False, "message": f"Last.fm said: {e.message}"}
            except Exception as e:
                out = {"ok": False, "sent": False, "message": f"Could not reach Last.fm ({e})"}
    last_scrobble.update(t=time.time(), ok=out["ok"], message=out["message"], sent=out["sent"])
    _say(f"SCROBBLE {kind} {title} - {artist}: {out['message']}")
    return out


# ---------- what /state carries ----------
def _year(release):
    m = re.match(r"^(\d{4})", release or "")
    return m.group(1) if m else None


def _decorate(view):
    if view:
        view["year"] = _year(view.get("release"))
    return view


_v12_snapshot = v12.snapshot2


def snapshot3():
    out = _v12_snapshot()
    t = _decorate(out.get("track"))
    if t:
        with _prod_lock:
            p = _prod.get(t.get("tid"))
        t["producer"] = dict(p) if p else {"state": "pending", "names": [], "from": None}
    for h in out.get("history") or []:
        _decorate(h)
    out["scrobbler"] = {"enabled": scrobble_enabled(), "canSend": lfm.can_love(), "last": dict(last_scrobble)}
    return out


_v12_handle = v12.handle2


def handle3(ev):
    _v12_handle(ev)
    try:
        if ev.get("event") == "track" and ev.get("pandoraTrackId"):
            tid = ev["pandoraTrackId"]
            with _prod_lock:
                _prod[tid] = {"state": "pending", "names": [], "from": None}
            threading.Thread(target=_producer_job, args=(tid,), daemon=True).start()
    except Exception as e:
        print("v13 event error:", e, flush=True)


# ---------- clicks from the player page ----------
def do_station(name):
    name = str(name or "").strip()
    if not name:
        return
    href = base.stations_known.get(name)
    if not href:                                    # names compared loosely, as the page may send a slightly different spelling
        low = name.lower()
        href = next((h for n, h in base.stations_known.items() if n.lower() == low), None)
    arg = name + ("@@" + href if href else "")
    base.send_pandora("station:" + arg)
    with base.state_lock:                           # the userscript may try several ways in a row: keep Pandora's flicker from pausing Spotify
        base.state["settle_until"] = max(base.state["settle_until"], time.time() + 14.0)


class Handler(v12.Handler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/player":
            try:
                with open(PAGE_FILE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(500, f"player_v13.html not found next to the receiver ({PAGE_FILE})".encode(), "text/plain")
        super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/cmd", "/scrobble"):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
            body = body if isinstance(body, dict) else {}
            if path == "/scrobble":
                return self._reply(do_scrobble(body))
            if body.get("cmd") == "station":
                do_station(body.get("name"))
                return self._reply(base.snapshot())
            self.rfile = io.BytesIO(raw)
        super().do_POST()


def main():
    if not os.path.exists(PAGE_FILE):
        raise SystemExit(f"player_v13.html is missing (looked for {PAGE_FILE}). Put it in the same folder as this file.")
    base.snapshot = snapshot3
    base.handle = handle3
    v12._load_new()
    if not base.SPOTIFY_ON:
        print("Spotify not logged in yet, so only logging. To enable playback run:  python spotify_player_v11.py login", flush=True)
    lib.start(base.SPOTIFY_ON)
    print("Playlist changes from the page:", "allowed" if pls.can_write() else "NOT yet allowed (run: python spotify_playlists_v12.py login)", flush=True)
    print("Last.fm:", "set up" if lfm.configured() else "not set up (username / API key missing)", "|",
          "can love tracks" if lfm.can_love() else "cannot love yet (run: python lastfm_v12.py auth)", "|",
          "scrobbles from /scrobble are SENT" if scrobble_enabled() else "scrobbles from /scrobble are only logged", flush=True)
    print(f"Receiver v13 listening on http://{base.HOST}:{base.PORT}  (events -> {base.EVENTS_FILE})", flush=True)
    print(f"Player page: http://{base.HOST}:{base.PORT}/player", flush=True)
    ThreadingHTTPServer((base.HOST, base.PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
