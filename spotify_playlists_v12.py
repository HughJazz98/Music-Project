#!/usr/bin/env python3
"""
Add the current track to, or remove it from, your watched Spotify playlists (v12).
Sits on top of spotify_player_v11.py and mirror_library_v11.py and changes neither. Standard library only.

    python spotify_playlists_v12.py login     one-time: log in to Spotify again, this time also allowing playlist changes
    python spotify_playlists_v12.py status    shows whether the saved login may change playlists, and which playlists are watched

How it works
  The player page's "Spotify Playlists" section lists every playlist in mirror_config.json with a tick if the current
  track is already on it. Clicking an unticked playlist adds the track; clicking a ticked one removes it.
  - Adding uses the Spotify song the mirror matched to the Pandora track (the "Playback on Spotify" line).
  - Removing takes out every copy of that song on that playlist. If the playlist holds a different release of the same
    song (found by title + artist, which is how the tick was decided), that copy is removed too, so clicking a ticked
    playlist always leaves the song off it.
  - Spotify only lets this app change playlists you own or collaborate on, the same ones it can read.
  - The tick changes at once; the once-a-minute playlist watcher then re-reads the playlist and confirms it.
"""
import sys
import threading
from datetime import datetime

import mirror_library_v11 as lib
import spotify_player_v11 as sp

WRITE_SCOPES = ("playlist-modify-private", "playlist-modify-public")
_op_lock = threading.Lock()          # one add/remove at a time, so two quick clicks cannot interleave


def _say(msg):
    print(f"[{datetime.now():%H:%M:%S}] PLAYLISTS {msg}", flush=True)


def can_write():
    """Does the saved Spotify login include permission to change playlists?"""
    return all(sp.has_scope(s) for s in WRITE_SCOPES)


def login():
    """Like spotify_player's login, with the two playlist-changing permissions added."""
    missing = [s for s in WRITE_SCOPES if s not in sp.SCOPES.split()]
    if missing:
        sp.SCOPES = sp.SCOPES + " " + " ".join(missing)
    sp.login()


def _sid(uri):
    return (uri or "").split(":")[-1] or None


def membership(uri, title, artist):
    """[{id, name, has}] for every watched playlist: is this track on it? Same test the watcher uses (Spotify id, else title + artist)."""
    sid = _sid(uri)
    k = lib.key(title, artist) if title and artist else None
    with lib._lock:
        return [{"id": pid, "name": p.get("name"), "has": bool((sid and sid in p["ids"]) or (k and k in p["keys"]))}
                for pid, p in lib.playlists.items()]


def _find_uris(pid, sid, k):
    """Every copy of the song on the playlist (same Spotify id, or same title + artist), read fresh from Spotify."""
    uris, offset = [], 0
    while True:
        page = sp.api("GET", f"/playlists/{pid}/items", {"limit": 50, "offset": offset})
        for entry in page.get("items", []):
            t = (entry or {}).get("item") or (entry or {}).get("track")    # Spotify renamed track -> item in 2026
            if not t or not t.get("id") or t.get("type", "track") != "track":
                continue
            names = [a.get("name") for a in t.get("artists", [])]
            if t["id"] == sid or (k and any(lib.key(t.get("name"), n) == k for n in names)):
                uri = t.get("uri") or "spotify:track:" + t["id"]
                if uri not in uris:
                    uris.append(uri)
        offset += 50
        if not page.get("next"):
            return uris


def _local(pid, add, sid, k, gone_ids=()):
    """Update what the watcher remembers right away, so the tick flips without waiting for its next pass."""
    with lib._lock:
        p = lib.playlists.get(pid)
        if not p:
            return
        if add:
            if sid:
                p["ids"].add(sid)
            if k:
                p["keys"].add(k)
        else:
            for i in set(gone_ids) | ({sid} if sid else set()):
                p["ids"].discard(i)
            if k:
                p["keys"].discard(k)


def toggle(pid, uri, title, artist):
    """Add the track to playlist `pid`, or remove it if it is already there. Returns {ok, action, message}."""
    if not can_write():
        return {"ok": False, "message": "Spotify has not allowed playlist changes yet. In a terminal in this folder run: python spotify_playlists_v12.py login"}
    sid = _sid(uri)
    k = lib.key(title, artist) if title and artist else None
    with _op_lock:
        row = next((m for m in membership(uri, title, artist) if m["id"] == pid), None)
        if not row:
            return {"ok": False, "message": "That playlist is not one of the watched playlists."}
        name = row["name"]
        try:
            if row["has"]:
                uris = _find_uris(pid, sid, k) or ([uri] if uri else [])
                if not uris:
                    return {"ok": False, "message": f'Could not find the song on "{name}" to remove it.'}
                for i in range(0, len(uris), 100):
                    sp.api("DELETE", f"/playlists/{pid}/items", body={"items": [{"uri": u} for u in uris[i:i + 100]]})
                _local(pid, False, sid, k, gone_ids=[_sid(u) for u in uris])
                _say(f'REMOVED from "{name}": {title} - {artist} ({len(uris)} copy/copies)')
                return {"ok": True, "action": "removed", "name": name, "message": f'Removed from "{name}"'}
            if not uri:
                return {"ok": False, "message": "No Spotify match for this track yet, so there is nothing to add."}
            sp.api("POST", f"/playlists/{pid}/items", body={"uris": [uri]})
            _local(pid, True, sid, k)
            _say(f'ADDED to "{name}": {title} - {artist}')
            return {"ok": True, "action": "added", "name": name, "message": f'Added to "{name}"'}
        except sp.ApiError as e:
            _say(f'ERROR changing "{name}": {e}')
            if e.status in (401, 403):
                return {"ok": False, "message": f'Spotify refused to change "{name}" ({e.status}). It only allows playlists you own or collaborate on. '
                                                 "If you have not logged in since v12, run: python spotify_playlists_v12.py login"}
            return {"ok": False, "message": f'Spotify error {e.status} while changing "{name}"'}
        except Exception as e:
            _say(f'ERROR changing "{name}": {e}')
            return {"ok": False, "message": f'Could not change "{name}": {e}'}


def _main(argv):
    if not argv or argv[0] not in ("login", "status"):
        print(__doc__)
        return
    if argv[0] == "login":
        login()
        print("Done. Playlist changes are now allowed:", "yes" if can_write() else "NO (Spotify did not grant it)")
        return
    print("Logged in to Spotify:", "yes" if sp.logged_in() else "no")
    print("Allowed to change playlists:", "yes" if can_write() else "no  (run: python spotify_playlists_v12.py login)")
    print("Watched playlists are listed by the receiver at start-up (mirror_config.json).")


if __name__ == "__main__":
    _main(sys.argv[1:])
