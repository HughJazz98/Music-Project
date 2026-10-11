#!/usr/bin/env python3
"""
Last.fm side of the player page (v12): YOUR scrobble count for the playing track, whether it is already one of your
loved tracks, a Love button, and a link to the artist's Last.fm page. Standard library only.

Reads the "lastfm" part of mirror_config.json:  {"username": "...", "api_key": "...", "api_secret": "...", "session_key": "..."}
The first three are already there; "session_key" is added by the one-time step below. Keys are never sent to the browser.

    python lastfm_v12.py auth                    one-time: lets this app love tracks on your account (Last.fm opens in your browser)
    python lastfm_v12.py test "Artist" "Title"   prints what Last.fm says about that track for your account

Notes
  - Reading the count and the loved state needs only the username and API key. Loving needs the one-time "auth" step.
  - Last.fm counts a play only after it is scrobbled, so the number for the song playing right now catches up a little late.
  - Lookups happen in the background and are remembered for two minutes; the player page never waits on Last.fm.
  - This file only reads and loves. It does not scrobble, so it cannot double-count plays that Pano Scrobbler already sends.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime

CONFIG_FILE = "mirror_config.json"
API = os.environ.get("LASTFM_API_BASE", "https://ws.audioscrobbler.com/2.0/")
SITE = "https://www.last.fm"
CACHE_SEC = 120
UA = "PandoraMirror/0.12 (personal use)"

# Pandora tacks edition tags onto titles ("(Remastered)", "- 2011 Remaster"); Last.fm uses the plain title.
_WORDS = r"(remaster|deluxe|anniversary|edition|version|mono|stereo|expanded|bonus|explicit|clean|reissue)"
_PAREN = re.compile(r"\s*[\(\[][^\)\]]*" + _WORDS + r"[^\)\]]*[\)\]]", re.I)
_DASH = re.compile(r"\s+-\s+[^-]*" + _WORDS + r".*$", re.I)


def clean_title(title):
    return _DASH.sub("", _PAREN.sub("", title or "")).strip()


class LastfmError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


# ---------- config ----------
_cfg_cache = {"mtime": None, "data": {}}


def _full_config():
    try:
        mt = os.path.getmtime(CONFIG_FILE)
    except OSError:
        return {}
    if mt != _cfg_cache["mtime"]:
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        _cfg_cache.update(mtime=mt, data=data if isinstance(data, dict) else {})
    return _cfg_cache["data"]


def _s(name):
    v = (_full_config().get("lastfm") or {}).get(name)
    return v.strip() if isinstance(v, str) else ""


def configured():
    return bool(_s("username") and _s("api_key"))


def can_love():
    return bool(_s("session_key") and _s("api_secret") and _s("api_key"))


def artist_url(artist):
    return f"{SITE}/music/{urllib.parse.quote_plus(artist)}" if artist else None


# ---------- the API ----------
def _signature(params, secret):
    raw = "".join(k + str(params[k]) for k in sorted(params) if k not in ("format", "callback")) + secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _call(params, sign=False, post=False):
    p = {k: v for k, v in params.items() if v is not None}
    p["api_key"] = _s("api_key")
    if sign:
        p["api_sig"] = _signature(p, _s("api_secret"))
    p["format"] = "json"
    data = urllib.parse.urlencode(p).encode("utf-8")
    if post:
        req = urllib.request.Request(API, data=data, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA})
    else:
        req = urllib.request.Request(API + "?" + data.decode("ascii"), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except Exception:
            raise LastfmError(e.code, f"HTTP {e.code}")
    if isinstance(body, dict) and "error" in body:
        raise LastfmError(int(body["error"]), body.get("message") or "Last.fm error")
    return body


def _num(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _fetch(artist, title):
    """What Last.fm says about this track for your account (one network call)."""
    try:
        d = _call({"method": "track.getInfo", "artist": artist, "track": title, "username": _s("username"), "autocorrect": 1})
    except LastfmError as e:
        if e.code == 6:                                   # "Track not found"
            return {"found": False, "userPlays": 0, "loved": False, "globalPlays": None, "trackUrl": None, "artistUrl": None,
                    "trackName": None, "artistName": None}
        raise
    t = d.get("track") or {}
    a = t.get("artist") if isinstance(t.get("artist"), dict) else {}
    return {"found": True,
            "userPlays": _num(t.get("userplaycount")),
            "loved": (str(t.get("userloved")) == "1") if "userloved" in t else None,
            "globalPlays": _num(t.get("playcount")),
            "trackUrl": t.get("url"), "artistUrl": a.get("url"),
            "trackName": t.get("name"), "artistName": a.get("name")}


# ---------- lookups that never block the page ----------
_lock = threading.Lock()
_cache = {}      # (artist, title) lowercased -> {"t": when it was last tried, "data": ..., "error": ..., "busy": bool}


def _ckey(artist, title):
    return (artist.strip().lower(), clean_title(title).strip().lower())


def _refresh(k, artist, title):
    try:
        data, err = _fetch(artist, title), None
    except LastfmError as e:
        data, err = None, f"Last.fm said: {e.message}"
    except Exception as e:
        data, err = None, f"could not reach Last.fm ({e})"
    with _lock:
        e = _cache[k]
        e["busy"], e["t"] = False, time.time()
        if data is not None:
            e["data"], e["error"] = data, None
        else:
            e["error"] = err                              # an older answer, if there is one, is kept


def lookup(artist, title):
    """Whatever is known right now, or None if Last.fm is not set up. Starts a background refresh when the answer is missing or old."""
    if not (configured() and artist and title):
        return None
    k = _ckey(artist, title)
    with _lock:
        e = _cache.setdefault(k, {"t": 0.0, "data": None, "error": None, "busy": False})
        start = not e["busy"] and time.time() - e["t"] > CACHE_SEC
        if start:
            e["busy"] = True
        snap = dict(e)
    if start:
        threading.Thread(target=_refresh, args=(k, artist, clean_title(title)), daemon=True).start()
    return snap


def public_state(artist, title):
    """What the player page shows for the current track. Safe to call every second."""
    base = {"canLove": can_love(), "artistUrl": artist_url(artist)}
    if not configured():
        return dict(base, state="off", message="Not set up yet. Fill in username and API key in the lastfm part of mirror_config.json.")
    e = lookup(artist, title)
    if e is None:
        return dict(base, state="idle")
    out = dict(base, state="loading")
    if e["data"]:
        out.update(e["data"], state="ok")
        out["artistUrl"] = e["data"].get("artistUrl") or base["artistUrl"]
    elif e["error"]:
        out.update(state="error", message=e["error"])
    return out


def love(artist, title):
    """Love the track on your Last.fm account. Returns {ok, message}."""
    if not configured():
        return {"ok": False, "message": "Last.fm is not set up yet."}
    if not can_love():
        return {"ok": False, "message": "This app is not allowed to love tracks yet. In a terminal in this folder run once: python lastfm_v12.py auth"}
    k = _ckey(artist, title)
    with _lock:
        known = (_cache.get(k) or {}).get("data") or {}
    name_a, name_t = known.get("artistName") or artist, known.get("trackName") or clean_title(title)    # Last.fm's own spelling when it told us
    try:
        _call({"method": "track.love", "artist": name_a, "track": name_t, "sk": _s("session_key")}, sign=True, post=True)
    except LastfmError as e:
        if e.code in (4, 9):
            return {"ok": False, "message": "Last.fm no longer accepts the saved login. Run again: python lastfm_v12.py auth"}
        return {"ok": False, "message": f"Last.fm said: {e.message}"}
    except Exception as e:
        return {"ok": False, "message": f"Could not reach Last.fm ({e})"}
    with _lock:
        e = _cache.get(k)
        if e and e["data"]:
            e["data"]["loved"] = True
    return {"ok": True, "message": f'Loved "{name_t}" on Last.fm'}


# ---------- one-time authorisation ----------
def _save_session(key):
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    shutil.copy2(CONFIG_FILE, f"{CONFIG_FILE}.{datetime.now():%Y%m%d-%H%M%S}.bak")
    cfg.setdefault("lastfm", {})["session_key"] = key
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1, ensure_ascii=False)


def auth():
    if not (_s("api_key") and _s("api_secret")):
        sys.exit("The lastfm part of mirror_config.json needs api_key and api_secret first (python setup_mirror_config_v2.py fills them in).")
    try:
        token = _call({"method": "auth.getToken"}, sign=True)["token"]
    except LastfmError as e:
        sys.exit(f"Last.fm refused to start the login: {e.message}")
    url = f"{SITE}/api/auth/?" + urllib.parse.urlencode({"api_key": _s("api_key"), "token": token})
    print("Opening Last.fm in your browser. Log in if asked, then click \"Yes, allow access\".\nIf nothing opens, paste this into your browser:\n\n" + url + "\n", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    for attempt in (1, 2):
        input("When you have clicked Yes, allow access, press Enter here... ")
        try:
            s = _call({"method": "auth.getSession", "token": token}, sign=True)["session"]
            break
        except LastfmError as e:
            if e.code == 14 and attempt == 1:             # the token was not approved yet
                print("Last.fm says the access was not approved yet. Click \"Yes, allow access\" in the browser, then try once more.")
                continue
            sys.exit(f"Last.fm refused: {e.message}")
    _save_session(s["key"])
    print(f"Done. This app may now love tracks on the Last.fm account \"{s.get('name')}\". (Saved to {CONFIG_FILE}; the old file was kept as a .bak copy.)")


def _main(argv):
    if not argv or argv[0] not in ("auth", "test"):
        print(__doc__)
        return
    if argv[0] == "auth":
        auth()
        return
    if len(argv) < 3:
        print('usage: python lastfm_v12.py test "Artist" "Title"')
        return
    if not configured():
        sys.exit("username / api_key are missing in the lastfm part of mirror_config.json")
    try:
        info = _fetch(argv[1], clean_title(argv[2]))
    except LastfmError as e:
        sys.exit(f"Last.fm said: {e.message}")
    print(json.dumps(info, indent=1))
    print("Allowed to love tracks:", "yes" if can_love() else "no (run: python lastfm_v12.py auth)")


if __name__ == "__main__":
    _main(sys.argv[1:])
