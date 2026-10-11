#!/usr/bin/env python3
"""
What the mirror knows about YOU (as opposed to the track): your playlists, your Pandora thumbs, your scrobbles.
Used by pandora_receiver_v11.py. Standard library only. Nothing here is on the track -> Spotify path.

PLAYLISTS (real, kept up to date)
    Put the playlists you want watched in mirror_config.json (created on first run):
        {"playlists": ["My Hardcore Playlist", "https://open.spotify.com/playlist/xxxxxxxxxxxxxxxxxxxxxx"]}
    A name (any capitals), a playlist id or a playlist link all work. `python spotify_player_v11.py playlists` lists yours.
    Once a minute ONE Spotify call asks for the snapshot id of every playlist; a playlist's songs are re-read only when its
    snapshot id changed (you added or removed something), so adds show up on the player page within about a minute.
    Spotify only lets this app read playlists you own or collaborate on (February 2026 development-mode rule).
    Needs the playlist-read permissions: log in again once with  python spotify_player_v11.py login

THUMBS (placeholder until you have the data)
    If thumbs_history.json exists next to this file it is read, as a list of
        {"station": "Cruel Hand Radio", "title": "House Arrest", "artist": "Cruel Hand", "vote": "up"}     (vote: "up" or "down")
    The file written by the community console script (liked_songs_grouped_by_station.json, thumbs UP only) is also read if
    present: it is guessed from its description, not tested, so check the "THUMBS loaded N" line it prints.

ACCOUNTS (v10): mirror_config.json can also hold
        "pandora": {"username": "your pandora username"},
        "lastfm":  {"username": "...", "api_key": "...", "api_secret": "..."}
    The page shows what is filled in (never the key or secret). Keep this file private. Nothing is sent to Last.fm yet.

SCROBBLES / LAST.FM (placeholders only: nothing is read, recorded or sent yet)
"""
import json
import os
import re
import threading
import time
from datetime import datetime

import spotify_player_v11 as sp

SP_FILE = os.path.basename(sp.__file__)     # named in messages so they always match the version you are running

CONFIG_FILE = "mirror_config.json"
PLAYLIST_POLL_SEC = 60            # 0 turns the playlist watcher off
THUMBS_FILE = "thumbs_history.json"
THUMBS_FILE_COMMUNITY = "liked_songs_grouped_by_station.json"

_lock = threading.Lock()
playlists = {}                    # playlist id -> {"name", "snapshot", "ids": set of spotify track ids, "keys": set of (title, artist)}
status = {"state": "starting", "message": "", "checked": None}
spotify_user = {"name": None}     # who the Spotify login belongs to (shown on the page)
thumbs = {}                       # (title, artist) -> {"vote": "up"/"down", "stations": [...]}
thumbs_info = {"state": "placeholder", "message": "Thumbs history not imported yet", "count": 0}


def _say(msg):
    print(f"[{datetime.now():%H:%M:%S}] LIBRARY {msg}", flush=True)


def key(title, artist):
    return (sp.norm(title), sp.norm(artist))


# ---------- playlists ----------
TEMPLATE = {"playlists": [], "pandora": {"username": ""}, "lastfm": {"username": "", "api_key": "", "api_secret": ""}}
_cfg_cache = {"mtime": None, "data": {}}


def _cfg():
    """The whole config file (re-read only when it changes). Creates it with empty slots the first time."""
    try:
        mt = os.path.getmtime(CONFIG_FILE)
    except FileNotFoundError:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(TEMPLATE, f, indent=1)
        mt = os.path.getmtime(CONFIG_FILE)
    if mt != _cfg_cache["mtime"]:
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                _cfg_cache.update(mtime=mt, data=json.load(f))
        except Exception as e:
            _say(f"could not read {CONFIG_FILE}: {e}")
            _cfg_cache.update(mtime=mt, data={})
    return _cfg_cache["data"]


def _config():
    return [str(w).strip() for w in (_cfg().get("playlists") or []) if str(w).strip()]


def _my_playlists():
    out, offset = [], 0
    while True:
        page = sp.api("GET", "/me/playlists", {"limit": 50, "offset": offset})
        out += page.get("items", [])
        offset += 50
        if not page.get("next"):
            return [p for p in out if p]


def _resolve(wanted, mine):
    """Match what is written in the config (name, id or link) to playlists on your account."""
    found, missing = [], []
    for w in wanted:
        m = re.search(r"playlist[/:]([A-Za-z0-9]{22})", w)
        pid = m.group(1) if m else w
        hit = next((p for p in mine if p.get("id") == pid), None) or next((p for p in mine if (p.get("name") or "").lower() == w.lower()), None)
        (found if hit else missing).append(hit or w)
    return found, missing


def _fetch_items(pid):
    ids, keys, offset = set(), set(), 0
    while True:
        page = sp.api("GET", f"/playlists/{pid}/items", {"limit": 50, "offset": offset})
        for entry in page.get("items", []):
            t = (entry or {}).get("item") or (entry or {}).get("track")    # Spotify renamed track -> item in 2026
            if not t or not t.get("id") or t.get("type", "track") != "track":
                continue
            ids.add(t["id"])
            for a in t.get("artists", []):
                keys.add(key(t.get("name"), a.get("name")))
        offset += 50
        if not page.get("next"):
            return ids, keys


def refresh():
    """One pass of the watcher. Returns nothing; results are in `playlists` and `status`."""
    if not sp.has_scope("playlist-read-private"):
        status.update(state="needs_login", message=f"Log in to Spotify again so the mirror may read your playlists. In a terminal in this folder run: python {SP_FILE} login")
        return
    wanted = _config()
    try:
        if not spotify_user["name"]:
            me = sp.api("GET", "/me")
            spotify_user["name"] = me.get("display_name") or me.get("id")
        mine = _my_playlists()
        if not wanted:
            if status["state"] != "not_configured":
                _say(f"no playlists chosen yet. Put some of these names in {CONFIG_FILE} under \"playlists\":")
                for p in mine:
                    print(f'     {p.get("name")}', flush=True)
            status.update(state="not_configured", message="No playlists chosen yet (mirror_config.json)")
            with _lock:
                playlists.clear()
            return
        found, missing = _resolve(wanted, mine)
        for w in missing:
            if status.get("missing") != missing:
                _say(f'"{w}" is not one of your playlists (check the spelling, or use its id)')
        status["missing"] = missing
        keep = {p["id"] for p in found}
        with _lock:
            for pid in [k for k in playlists if k not in keep]:
                _say(f'no longer watching "{playlists.pop(pid)["name"]}"')
        for p in found:
            pid, snap = p["id"], p.get("snapshot_id")
            with _lock:
                cur = playlists.get(pid)
            if cur and cur["snapshot"] == snap:
                continue
            ids, keys = _fetch_items(pid)
            with _lock:
                first = pid not in playlists
                playlists[pid] = {"name": p.get("name"), "snapshot": snap, "ids": ids, "keys": keys}
            _say(f'{"loaded" if first else "changed:"} "{p.get("name")}" has {len(ids)} songs')
        status.update(state="ok", message="", checked=time.time())
    except sp.ApiError as e:
        if e.status in (401, 403):
            status.update(state="needs_login", message=f"Spotify refused to show your playlists ({e.status}). Log in again: python {SP_FILE} login. "
                          "Spotify also only shares playlists you own or collaborate on.")
        else:
            status.update(state="error", message=f"Spotify error {e.status} while reading playlists; will retry")
        _say(status["message"])
    except Exception as e:
        status.update(state="error", message=f"could not read playlists ({e}); will retry")
        _say(status["message"])


def _watch():
    last_state = None
    while True:
        try:
            refresh()
        except Exception as e:                  # the watcher must never die
            _say(f"watcher error: {e}")
        if status["state"] != last_state and status["state"] in ("needs_login", "not_configured"):
            _say(status["message"])
        last_state = status["state"]
        time.sleep(PLAYLIST_POLL_SEC)


def containing(spotify_id, title, artist):
    """Names of the watched playlists this track is on (by Spotify id, else by title + artist)."""
    k = key(title, artist)
    with _lock:
        return [p["name"] for p in playlists.values() if (spotify_id and spotify_id in p["ids"]) or k in p["keys"]]


# ---------- thumbs ----------
def _first(d, *names):
    for n in names:
        if isinstance(d.get(n), str) and d[n].strip():
            return d[n].strip()
    return None


def _records(obj, station=None, default_vote=None):
    """Yield (station, title, artist, vote) from a list of records or a {station: [records]} mapping."""
    if isinstance(obj, list):
        for x in obj:
            if isinstance(x, dict):
                title = _first(x, "title", "name", "songName", "trackName", "song")
                artist = _first(x, "artist", "artistName")
                if title and artist:
                    vote = (_first(x, "vote", "thumb", "rating") or default_vote or "").lower()
                    vote = "up" if vote in ("up", "like", "liked", "thumbs_up", "1", "+1") else "down" if vote in ("down", "dislike", "disliked", "thumbs_down", "-1") else None
                    yield (_first(x, "station", "channel", "stationName") or station, title, artist, vote)
            else:
                yield from _records(x, station, default_vote)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (list, dict)):
                yield from _records(v, k if station is None else station, default_vote)


def load_thumbs():
    """Read the thumbs files if they exist; otherwise leave the placeholder in place."""
    found = [(f, v) for f, v in ((THUMBS_FILE, None), (THUMBS_FILE_COMMUNITY, "up")) if os.path.exists(f)]
    if not found:
        return
    out, skipped = {}, 0
    for fname, default_vote in found:
        try:
            with open(fname, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            _say(f"could not read {fname}: {e}")
            continue
        for station, title, artist, vote in _records(data, None, default_vote):
            if vote not in ("up", "down"):
                skipped += 1
                continue
            e = out.setdefault(key(title, artist), {"vote": vote, "stations": []})
            e["vote"] = vote
            if station and station not in e["stations"]:
                e["stations"].append(station)
    with _lock:
        thumbs.clear()
        thumbs.update(out)
    ups = sum(1 for v in out.values() if v["vote"] == "up")
    thumbs_info.update(state="loaded", message="", count=len(out))
    _say(f"THUMBS loaded {len(out)} songs ({ups} up, {len(out) - ups} down) from {', '.join(f for f, _ in found)}"
         + (f"; {skipped} entries had no readable vote and were skipped" if skipped else ""))


def thumb_for(title, artist):
    with _lock:
        return dict(thumbs.get(key(title, artist)) or {}) or None


# ---------- what the page shows ----------
def summary():
    with _lock:
        pls = [{"name": p["name"], "count": len(p["ids"])} for p in playlists.values()]
    return {
        "playlists": {"state": status["state"], "message": status["message"], "watching": pls},
        "thumbs": dict(thumbs_info),
        "scrobbles": {"state": "placeholder", "message": "Scrobble history not imported yet"},
        "lastfm": {"state": "placeholder", "message": "Last.fm account not connected; scrobbles are not being recorded yet"},
    }


def accounts(pandora_seen_ago):
    """Account status for the page. pandora_seen_ago: seconds since the Pandora tab's userscript last contacted us (None = never).
    Keys, secrets and tokens are never included."""
    cfg = _cfg()
    pu = ((cfg.get("pandora") or {}).get("username") or "").strip()
    lf = cfg.get("lastfm") or {}
    lu = (lf.get("username") or "").strip()
    have_key, have_secret = bool((lf.get("api_key") or "").strip()), bool((lf.get("api_secret") or "").strip())
    if not sp.logged_in():
        spot = {"state": "off", "message": f"Not logged in. In a terminal in this folder run: python {SP_FILE} login"}
    elif not sp.has_scope("playlist-read-private"):
        spot = {"state": "partial", "message": f"Logged in, but without permission to read playlists. Log in again: python {SP_FILE} login"}
    else:
        spot = {"state": "on", "message": "Connected" + (f" as {spotify_user['name']}" if spotify_user["name"] else "")}
    if pandora_seen_ago is None:
        pand = {"state": "off", "message": "No Pandora tab has reported in. Open pandora.com with the userscript enabled."}
    elif pandora_seen_ago > 5:
        pand = {"state": "partial", "message": f"The Pandora tab went quiet {int(pandora_seen_ago)} s ago (closed, asleep or the userscript is off)."}
    else:
        pand = {"state": "on", "message": "Pandora tab connected" + (f" (account: {pu})" if pu else "")}
    pand["username"] = pu or None
    pand["thumbsUrl"] = f"https://www.pandora.com/profile/thumbs/{pu}" if pu else None
    if not lu and not have_key:
        last = {"state": "off", "message": f"Not set up. Fill the \"lastfm\" part of {CONFIG_FILE} (username, API key and secret). Scrobbling itself is not active yet."}
    else:
        bits = [f"username {lu}" if lu else "no username", "API key saved" if have_key else "no API key", "secret saved" if have_secret else "no secret"]
        last = {"state": "partial", "message": "Saved: " + ", ".join(bits) + ". Not connected yet; scrobbling is not active."}
    return {"spotify": spot, "pandora": pand, "lastfm": last}


def start(spotify=True):
    """Called once by the receiver. Reads the thumbs files and, if Spotify is logged in, starts the playlist watcher."""
    load_thumbs()
    if not spotify:
        status.update(state="needs_login", message=f"Not logged in to Spotify yet. In a terminal in this folder run: python {SP_FILE} login")
    elif PLAYLIST_POLL_SEC > 0:
        threading.Thread(target=_watch, daemon=True).start()
