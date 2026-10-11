#!/usr/bin/env python3
"""
pandora_receiver_v19 = v18 + Spotify volume (for the player page's hotkeys).

    python pandora_receiver_v19.py

Keep pandora_receiver_v18.py (and v16, v11 to v15, wiki_v14.py, the helper files) in the same folder.
Needs player_v19.html. Use it with pandora_userscript_v13.user.js. Open http://127.0.0.1:8765/player

Volume   the page sends {cmd:"volume", level:0-100} or {cmd:"mute"}; the receiver sets Spotify's volume on the device it plays on
         (needs Spotify Premium, and a device that allows remote volume: some phones and speakers do not).
"""
import io
import json
import os
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer

import pandora_receiver_v11 as base
import pandora_receiver_v13 as v13
import pandora_receiver_v18 as v18
import spotify_player_v11 as sp

PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "player_v19.html")

_vol = {"percent": None, "muted": False, "before_mute": None, "message": None, "read_at": 0.0}
_vol_lock = threading.Lock()
_target = {"percent": None, "busy": False}


def _read_device_volume():
    """Spotify's own idea of the volume (None when the device does not report one)."""
    st = sp._state()
    dev = st.get("device") or {}
    return dev.get("volume_percent")


def _refresh_if_old():
    if time.time() - _vol["read_at"] < 20:
        return
    _vol["read_at"] = time.time()

    def job():
        try:
            p = _read_device_volume()
        except Exception:
            return
        with _vol_lock:
            if p is not None and not _target["busy"]:
                _vol["percent"] = int(p)
    threading.Thread(target=job, daemon=True).start()


def _apply():
    """Send the newest wanted volume to Spotify; key-repeat only ever costs the last value."""
    while True:
        with _vol_lock:
            want = _target["percent"]
            _target["percent"] = None
            if want is None:
                _target["busy"] = False
                return
        try:
            dev = sp._choose_device()
            sp.api("PUT", "/me/player/volume", {"volume_percent": int(want), "device_id": dev["id"]})
            with _vol_lock:
                _vol["message"] = None
        except Exception as e:
            text = str(e)
            with _vol_lock:
                _vol["message"] = ("Spotify needs Premium to change volume from here." if "PREMIUM" in text.upper()
                                   else "Spotify would not change the volume on this device (" + text[:120] + ").")
            with _vol_lock:
                _target["busy"] = False
            return
        time.sleep(0.12)


def set_volume(level=None, mute=None):
    """Optimistic: the page sees the new value at once; Spotify is told in the background."""
    with _vol_lock:
        cur = _vol["percent"] if _vol["percent"] is not None else 50
        if mute:                                   # toggle mute
            if _vol["muted"]:
                level, _vol["muted"] = _vol["before_mute"] if _vol["before_mute"] is not None else 50, False
            else:
                _vol["before_mute"], _vol["muted"], level = cur, True, 0
        elif level is not None:
            _vol["muted"] = False
        if level is None:
            return
        level = max(0, min(100, int(level)))
        _vol["percent"] = level
        _target["percent"] = level
        start = not _target["busy"]
        _target["busy"] = True
    if start:
        threading.Thread(target=_apply, daemon=True).start()


_v18_snapshot = v18.snapshot7


def snapshot8():
    out = _v18_snapshot()
    _refresh_if_old()
    with _vol_lock:
        out["volume"] = {"percent": _vol["percent"], "muted": _vol["muted"], "message": _vol["message"]}
    return out


class Handler(v18.Handler):
    def do_POST(self):
        if urllib.parse.urlparse(self.path).path == "/cmd":
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
            if isinstance(body, dict) and body.get("cmd") in ("volume", "mute"):
                if body["cmd"] == "mute":
                    set_volume(mute=True)
                else:
                    try:
                        set_volume(level=float(body.get("level")))
                    except (TypeError, ValueError):
                        pass
                return self._reply(base.snapshot())
            self.rfile = io.BytesIO(raw)
        super().do_POST()


def main():
    if not os.path.exists(PAGE_FILE):
        raise SystemExit(f"player_v19.html is missing (looked for {PAGE_FILE}). Put it in the same folder as this file.")
    v18.PAGE_FILE = PAGE_FILE
    base.snapshot = snapshot8
    base.handle = v18.handle7
    v13.PAGE_FILE = PAGE_FILE
    v18.v16.v12._load_new()
    if not base.SPOTIFY_ON:
        print("Spotify not logged in yet, so only logging. To enable playback run:  python spotify_player_v11.py login", flush=True)
    v18.v16.lib.start(base.SPOTIFY_ON)
    print("Playlist changes from the page:", "allowed" if v18.v16.pls.can_write() else "NOT yet allowed (run: python spotify_playlists_v12.py login)", flush=True)
    print("Last.fm:", "set up" if v18.v16.lfm.configured() else "not set up (username / API key missing)", "|",
          "can love tracks" if v18.v16.lfm.can_love() else "cannot love yet (run: python lastfm_v12.py auth)", "|",
          "scrobbles from /scrobble are SENT" if v13.scrobble_enabled() else "scrobbles from /scrobble are only logged", flush=True)
    print(f"Receiver v19 listening on http://{base.HOST}:{base.PORT}  (events -> {base.EVENTS_FILE})", flush=True)
    print(f"Player page: http://{base.HOST}:{base.PORT}/player", flush=True)
    ThreadingHTTPServer((base.HOST, base.PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
