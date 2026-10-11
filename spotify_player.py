#!/usr/bin/env python3
"""
Spotify side of the Pandora mirror. Standard library only.

Used by pandora_receiver.py (it calls play_for_track / on_ad), and also usable on its own:

    python spotify_player.py login                      one-time browser login (PKCE, no client secret needed)
    python spotify_player.py devices                    list the Spotify devices your account can see
    python spotify_player.py diagnose                   tries each way of starting a track and reports what Spotify says
    python spotify_player.py test "Title" "Artist" ["Album"] [--play]
                                                        search + score candidates, optionally play the winner

Files it creates next to the script:
    spotify_token.json    your login tokens (private, keep it out of anything you share)
    spotify_cache.json    pandoraTrackId -> chosen Spotify track (repeat plays cost no search)
    spotify_log.jsonl     one line per track with timings and the match score
"""
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

# The Client ID is not a secret (it is visible in every login URL). No client secret is used or needed.
CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "46df57b71357413895754a88952a4357")
REDIRECT_URI = "http://127.0.0.1:8888/callback"   # must be listed in the app's Redirect URIs
SCOPES = "user-modify-playback-state user-read-playback-state user-read-private"
API = os.environ.get("SPOTIFY_API_BASE", "https://api.spotify.com/v1")
ACCOUNTS = os.environ.get("SPOTIFY_ACCOUNTS_BASE", "https://accounts.spotify.com")

# Optional: part of the device name to prefer when nothing is playing, e.g. "DESKTOP" or "iPhone".
PREFERRED_DEVICE = os.environ.get("SPOTIFY_DEVICE", "")
PAUSE_ON_AD = True        # pause Spotify when Pandora starts an ad (filler music comes in a later step)
ACCEPT_SCORE = 70         # title + artist match is 80; anything below this is treated as "no match"

TOKEN_FILE = "spotify_token.json"
CACHE_FILE = "spotify_cache.json"
LOG_FILE = "spotify_log.jsonl"

# ---------- text normalising ----------
EDITION_WORDS = r"(remaster|deluxe|anniversary|edition|version|mono|stereo|expanded|bonus|explicit|clean|reissue)"
EDITION_PAREN = re.compile(r"\s*[\(\[][^\)\]]*" + EDITION_WORDS + r"[^\)\]]*[\)\]]", re.I)
EDITION_DASH = re.compile(r"\s+-\s+[^-]*" + EDITION_WORDS + r".*$", re.I)   # "Song - 2011 Remaster"
BAD_WORDS = ("live", "karaoke", "tribute", "cover", "instrumental", "acoustic", "remix", "made famous", "originally performed")


def norm(s):
    """Lowercase, drop edition tags, accents and punctuation: 'Allegiance (Explicit)' -> 'allegiance'."""
    s = EDITION_PAREN.sub("", s or "")
    s = EDITION_DASH.sub("", s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def has_bad(s):
    n = " " + norm(s) + " "
    return {w for w in BAD_WORDS if (" " + w + " ") in n}


# ---------- token handling ----------
_token_lock = threading.Lock()


def _post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _save_token(tok, old=None):
    tok = dict(tok)
    if old and not tok.get("refresh_token"):
        tok["refresh_token"] = old.get("refresh_token")
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(tok, f, indent=1)
    return tok


def _load_token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def access_token(force_refresh=False):
    with _token_lock:
        tok = _load_token()
        if not tok:
            raise RuntimeError("Not logged in to Spotify. Run:  python spotify_player.py login")
        if force_refresh or time.time() >= tok.get("expires_at", 0):
            new = _post_form(ACCOUNTS + "/api/token", {
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
                "client_id": CLIENT_ID,
            })
            tok = _save_token(new, tok)
        return tok["access_token"]


def logged_in():
    return _load_token() is not None


def login():
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = ACCOUNTS + "/authorize?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "scope": SCOPES, "state": state, "code_challenge_method": "S256", "code_challenge": challenge,
    })
    result = {}

    class CB(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if urllib.parse.urlparse(self.path).path != "/callback":
                self.send_response(404); self.end_headers(); return
            result["code"] = (q.get("code") or [None])[0]
            result["state"] = (q.get("state") or [None])[0]
            result["error"] = (q.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h3>Spotify login finished. You can close this tab.</h3>")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 8888), CB)
    srv.timeout = 300
    print("Opening your browser to log in to Spotify. If nothing opens, paste this URL into Chrome:\n\n" + url + "\n", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    deadline = time.time() + 300
    while not result and time.time() < deadline:
        srv.handle_request()
    srv.server_close()
    if result.get("error") or not result.get("code"):
        raise RuntimeError("Spotify login failed: " + str(result.get("error") or "no code returned"))
    if result.get("state") != state:
        raise RuntimeError("Spotify login failed: state mismatch")
    tok = _post_form(ACCOUNTS + "/api/token", {
        "grant_type": "authorization_code", "code": result["code"], "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID, "code_verifier": verifier,
    })
    _save_token(tok)
    print("Logged in. Token saved to", TOKEN_FILE, flush=True)


# ---------- API calls ----------
class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status, self.body = status, body


LAST = {"status": None, "raw": ""}   # what Spotify answered to the most recent successful call


def api(method, path, params=None, body=None, _retry=True):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + access_token(),
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            LAST["status"], LAST["raw"] = r.status, raw.decode("utf-8", "replace")[:200]
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except ValueError:   # some endpoints answer with plain text
                return {"_raw": LAST["raw"]}
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        if e.code == 401 and _retry:
            access_token(force_refresh=True)
            return api(method, path, params, body, _retry=False)
        if e.code == 429 and _retry:
            wait = min(int(e.headers.get("Retry-After", "2")), 5)
            time.sleep(wait)
            return api(method, path, params, body, _retry=False)
        raise ApiError(e.code, text)


# ---------- matching ----------
def score(cand, title, artist, album):
    """Higher is better. Title + artist match = 80, album match up to +15, penalties for live/karaoke/etc."""
    s = 0.0
    nt, ct = norm(title), norm(cand.get("name"))
    if ct == nt:
        s += 50
    elif nt and ct and (nt in ct or ct in nt):
        s += 20
    else:
        return 0.0
    na = norm(artist)
    names = [norm(a.get("name")) for a in cand.get("artists", [])]
    if na in names:
        s += 30
    elif any(na in n or n in na for n in names if n):
        s += 10
    else:
        return 0.0
    cal = cand.get("album", {})
    nb, cb = norm(album), norm(cal.get("name"))
    if nb and cb:
        if nb == cb:
            s += 15
        elif nb in cb or cb in nb:
            s += 7
    wanted = has_bad(title) | has_bad(album or "")
    extra = (has_bad(cand.get("name")) | has_bad(cal.get("name"))) - wanted
    if extra:
        s -= 40
    if cal.get("album_type") == "compilation":
        s -= 3
    if cand.get("is_playable") is False:
        s -= 100
    s += (cand.get("popularity") or 0) / 100 * 5   # tiebreak only
    return s


def search(title, artist, album=None):
    """Returns (best_candidate_or_None, best_score, all_scored)."""
    t = norm(title) or title
    queries = [f'track:"{t}" artist:"{artist}"', f"{t} {artist}"]
    scored = []
    seen = set()
    for q in queries:
        data = api("GET", "/search", {"q": q, "type": "track", "limit": 10, "market": "from_token"})
        for c in data.get("tracks", {}).get("items", []):
            if c["id"] in seen:
                continue
            seen.add(c["id"])
            scored.append((score(c, title, artist, album), c))
        best = max((x[0] for x in scored), default=0)
        if best >= ACCEPT_SCORE:
            break
    scored.sort(key=lambda x: -x[0])
    if scored and scored[0][0] >= ACCEPT_SCORE:
        return scored[0][1], scored[0][0], scored
    return None, (scored[0][0] if scored else 0), scored


# ---------- playback ----------
def _pick_device(devices):
    usable = [d for d in devices if not d.get("is_restricted")]
    if PREFERRED_DEVICE:
        for d in usable:
            if PREFERRED_DEVICE.lower() in d.get("name", "").lower():
                return d
    for d in usable:
        if d.get("is_active"):
            return d
    return usable[0] if usable else None


VERIFY = True            # after starting a track, ask Spotify what is really playing (costs ~0.5 s); falls back if not
_dev = {}


def _state():
    try:
        return api("GET", "/me/player") or {}
    except ApiError:
        return {}


def _summary(st):
    item = st.get("item") or {}
    dev = st.get("device") or {}
    if not st:
        return "Spotify reports no player state at all"
    return f'is_playing={st.get("is_playing")}, device={dev.get("name")}, track={item.get("name")}'


def _confirm(uri, delays=(0.4, 0.6, 1.0)):
    want = uri.split(":")[-1]
    st = {}
    for d in delays:
        time.sleep(d)
        st = _state()
        item = st.get("item") or {}
        if st.get("is_playing") and want in (item.get("id"), (item.get("linked_from") or {}).get("id")):
            return True, st
    return False, st


def _m_direct(dev_id, uri):
    api("PUT", "/me/player/play", {"device_id": dev_id}, {"uris": [uri], "position_ms": 0})


def _m_transfer(dev_id, uri):
    api("PUT", "/me/player", None, {"device_ids": [dev_id], "play": True})
    time.sleep(0.4)
    _m_direct(dev_id, uri)


def _m_queue(dev_id, uri):
    api("POST", "/me/player/queue", {"uri": uri, "device_id": dev_id})
    api("POST", "/me/player/next", {"device_id": dev_id})


METHODS = [("direct", _m_direct), ("transfer-then-play", _m_transfer), ("queue-and-skip", _m_queue)]


def _choose_device(refresh=False):
    if _dev.get("id") and not refresh:
        return _dev
    dev = _pick_device(api("GET", "/me/player/devices").get("devices", []))
    if not dev:
        raise RuntimeError("No Spotify device found. Open Spotify on your computer or phone, then try again.")
    _dev.clear()
    _dev.update({"id": dev["id"], "name": dev.get("name")})
    return _dev


def play_uri(uri):
    """Start a track on the chosen device and (if VERIFY) confirm it is really playing, escalating through
    direct play, transfer-then-play, and queue-and-skip. Returns 'device via method'."""
    dev = _choose_device()
    last = {}
    for name, fn in METHODS:
        try:
            fn(dev["id"], uri)
        except ApiError as e:
            if e.status == 403 and "PREMIUM" in e.body.upper():
                raise RuntimeError("Spotify says this needs Premium on the logged-in account.")
            if e.status == 404:
                dev = _choose_device(refresh=True)
            last = {"error": str(e)}
            continue
        if not VERIFY:
            return f'{dev["name"]} via {name}'
        ok, st = _confirm(uri)
        if ok:
            return f'{dev["name"]} via {name}'
        last = st
    raise RuntimeError("Spotify accepted the commands but the track never started. Last state: "
                       + (last.get("error") or _summary(last)))


def diagnose():
    """Step-by-step playback check that prints what Spotify says after each method."""
    print("Devices Spotify can see:")
    for d in api("GET", "/me/player/devices").get("devices", []):
        print(f'  {d.get("name")!r}  type={d.get("type")}  active={d.get("is_active")}  restricted={d.get("is_restricted")}  '
              f'private_session={d.get("is_private_session")}  volume={d.get("volume_percent")}  id={d.get("id")}')
    print("Player state now:", _summary(_state()))
    print("(Tip: if Spotify's player is empty, start any song by hand in the app first, then run this again.)")
    cand, sc, _ = search("Confined", "As I Lay Dying", "Shadows Are Security")
    if not cand:
        print("search found nothing; stopping")
        return
    dev = _choose_device(refresh=True)
    print(f'Test track: {cand["name"]} ({cand["uri"]}), target device: {dev["name"]}')
    for name, fn in METHODS:
        print(f"\n[{name}] sending...")
        try:
            fn(dev["id"], cand["uri"])
            print(f'  Spotify answered HTTP {LAST["status"]}' + (f' with: {LAST["raw"]!r}' if LAST["raw"] else " (empty body)"))
        except ApiError as e:
            print("  ERROR:", e)
            continue
        ok, st = _confirm(cand["uri"], delays=(0.5, 1.0, 1.5))
        print("  result:", "PLAYING" if ok else "NOT playing", "|", _summary(st))
        if ok:
            print("\n=> this method works. The receiver will use it.")
            return
    print("\nNone of the methods started playback. Please paste this whole output.")


# ---------- cache + log ----------
_cache_lock = threading.Lock()
try:
    with open(CACHE_FILE, encoding="utf-8") as f:
        _cache = json.load(f)
except Exception:
    _cache = {}


def _cache_put(tid, entry):
    with _cache_lock:
        _cache[tid] = entry
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_cache, f, indent=1, ensure_ascii=False)


def _log(entry):
    entry["t"] = datetime.now().isoformat(timespec="seconds")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _say(msg):
    print(f"[{datetime.now():%H:%M:%S}] SPOT {msg}", flush=True)


_current = {"tid": None}

# ---------- watching Spotify for the user pausing/resuming ----------
# The receiver sets these to forward Spotify-side pause/resume to Pandora.
on_user_pause = None
on_user_resume = None
_w = {"id": None, "quiet_until": 0.0, "paused_flag": False, "pause_count": 0, "last_progress": 0}
WATCH_SEC = 1.2


def _arm_watcher(track_id, quiet=3.0):
    _w.update({"id": track_id, "quiet_until": time.time() + quiet, "paused_flag": False,
               "pause_count": 0, "last_progress": 0})


def _own_action(paused):
    """We changed Spotify's state ourselves (sync from Pandora or an ad): don't treat it as the user's doing."""
    _w["quiet_until"] = time.time() + 3.0
    _w["paused_flag"] = paused
    _w["pause_count"] = 0


def pause():
    _own_action(True)
    dev = _choose_device()
    api("PUT", "/me/player/pause", {"device_id": dev["id"]})


def resume():
    _own_action(False)
    dev = _choose_device()
    api("PUT", "/me/player/play", {"device_id": dev["id"]})


def _watch_once():
    if not _w["id"] or time.time() < _w["quiet_until"]:
        return
    st = _state()
    item = st.get("item") or {}
    if _w["id"] not in (item.get("id"), (item.get("linked_from") or {}).get("id")):
        return   # nothing, or something else is playing: not ours to interpret
    prog, dur = st.get("progress_ms") or 0, item.get("duration_ms") or 0
    if st.get("is_playing"):
        _w["last_progress"], _w["pause_count"] = prog, 0
        if _w["paused_flag"]:
            _w["paused_flag"] = False
            _say("resumed in Spotify")
            if on_user_resume:
                on_user_resume()
    elif not _w["paused_flag"]:
        if dur and dur - _w["last_progress"] < 4500:
            return   # it just reached the end of the track, not a pause
        _w["pause_count"] += 1
        if _w["pause_count"] >= 2:
            _w["paused_flag"] = True
            _say("paused in Spotify")
            if on_user_pause:
                on_user_pause()


def start_watcher():
    def loop():
        while True:
            time.sleep(WATCH_SEC)
            try:
                _watch_once()
            except Exception:
                pass
    threading.Thread(target=loop, daemon=True).start()


def play_for_track(ev):
    """Called by the receiver for each new Pandora track. Runs in its own thread."""
    tid, title, artist, album = ev.get("pandoraTrackId"), ev.get("title"), ev.get("artist"), ev.get("album")
    if not (tid and title and artist):
        return
    _current["tid"] = tid
    t0 = time.time()
    since_page = int(t0 * 1000) - ev.get("sentMs", int(t0 * 1000))
    try:
        hit = _cache.get(tid)
        from_cache = bool(hit)
        if hit:
            uri, label, sc = hit["uri"], hit["label"], hit.get("score")
            t_search = 0
        else:
            cand, sc, scored = search(title, artist, album)
            t_search = int((time.time() - t0) * 1000)
            if not cand:
                _say(f"NO MATCH for {title} - {artist} (best score {sc:.0f}). Pandora is still playing it.")
                _log({"tid": tid, "title": title, "artist": artist, "album": album, "result": "no_match",
                      "bestScore": sc, "top": [{"score": round(s), "name": c["name"], "album": c["album"]["name"]} for s, c in scored[:3]]})
                return
            uri = cand["uri"]
            label = f'{cand["name"]} - {", ".join(a["name"] for a in cand["artists"])} - {cand["album"]["name"]}'
            _cache_put(tid, {"uri": uri, "label": label, "score": round(sc), "durationMs": cand.get("duration_ms"),
                             "pandora": {"title": title, "artist": artist, "album": album}})
        if _current["tid"] != tid:   # Pandora already moved on while we were searching
            _say(f"skipped (track changed during search): {title}")
            return
        t1 = time.time()
        device = play_uri(uri)
        t_play = int((time.time() - t1) * 1000)
        total = since_page + int((time.time() - t0) * 1000)
        _arm_watcher(uri.split(":")[-1])
        _say(f"PLAY {label}  [score {sc:.0f}{', cached' if from_cache else ''}; search {t_search} ms, play {t_play} ms; {total} ms after Pandora's page flipped; device: {device}]")
        _log({"tid": tid, "title": title, "artist": artist, "album": album, "result": "played", "uri": uri,
              "score": sc, "cached": from_cache, "searchMs": t_search, "playMs": t_play, "totalMs": total})
    except Exception as e:
        _say(f"ERROR {e}")
        _log({"tid": tid, "title": title, "artist": artist, "result": "error", "error": str(e)})


def note_details(tid, duration_sec):
    """Called when Pandora's track length is known (3 s in). Flags a length mismatch, which usually means a wrong version."""
    hit = _cache.get(tid)
    if not hit or not duration_sec or not hit.get("durationMs"):
        return
    diff = abs(hit["durationMs"] / 1000 - duration_sec)
    if diff > 5:
        _say(f"WARNING length differs by {diff:.0f}s from Pandora ({duration_sec}s vs {hit['durationMs']/1000:.0f}s): {hit['label']}")
        _log({"tid": tid, "result": "length_mismatch", "pandoraSec": duration_sec, "spotifySec": hit["durationMs"] / 1000})


def on_ad():
    _current["tid"] = None
    _w["id"] = None
    if PAUSE_ON_AD and logged_in():
        try:
            pause()
            _say("paused for Pandora ad (filler comes in a later step)")
        except Exception as e:
            _say(f"could not pause: {e}")


# ---------- command line ----------
def _main(argv):
    if not argv or argv[0] not in ("login", "devices", "test", "diagnose"):
        print(__doc__)
        return
    if argv[0] == "login":
        login()
    elif argv[0] == "diagnose":
        diagnose()
    elif argv[0] == "devices":
        for d in api("GET", "/me/player/devices").get("devices", []):
            print(("* " if d.get("is_active") else "  ") + f'{d.get("name")}  ({d.get("type")})')
    else:
        rest = [a for a in argv[1:] if a != "--play"]
        if len(rest) < 2:
            print('usage: python spotify_player.py test "Title" "Artist" ["Album"] [--play]')
            return
        title, artist, album = rest[0], rest[1], (rest[2] if len(rest) > 2 else None)
        cand, sc, scored = search(title, artist, album)
        for s, c in scored[:6]:
            print(f'{s:6.1f}  {c["name"]} - {", ".join(a["name"] for a in c["artists"])} - {c["album"]["name"]} ({c["album"].get("release_date")})')
        print("-> chosen:" if cand else "-> no match above threshold; best was", cand["uri"] if cand else f"{sc:.0f}")
        if cand and "--play" in argv:
            print("playing on:", play_uri(cand["uri"]))


if __name__ == "__main__":
    _main(sys.argv[1:])
