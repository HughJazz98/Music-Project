#!/usr/bin/env python3
"""
Setup / maintenance menu for mirror_config.json  (v2: replaces the one-pass v1).

    python setup_mirror_config_v2.py              the menu
    python setup_mirror_config_v2.py --status     just print what is set up and what is wrong, change nothing

Run it in the mirror folder (it uses the newest spotify_player_v*.py and mirror_library_v*.py it finds there).
Nothing is written until you choose Save; Quit leaves the file as it was. Before saving it shows what changes (secrets masked) and
keeps a dated backup of the old file. Settings this script does not know about are left alone. Standard library only.
Keep mirror_config.json private: it holds your Last.fm secret.

What it can do
  Spotify   check the login, open the login if needed, show who you are and whether playlist permission is there
  Playlists pick which playlists the mirror watches: filter by text, toggle by number or range, see track counts, spot duplicate
            names, see playlists Spotify will not let the mirror read, and drop playlists that no longer exist. Saved by id.
  Pandora   username (you can paste your thumbs-page address instead)
  Last.fm   username, API key, API secret: format-checked, the username + key are tested against Last.fm, and the key + secret
            pair is tested with a signed request (so a mistyped secret is caught now, not when scrobbling starts)
  Thumbs    checks whether thumbs_history.json (or the community export) is next to the receiver and how many songs it holds
"""
import copy
import datetime
import getpass
import glob
import hashlib
import importlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

CONFIG_FILE = "mirror_config.json"
THUMBS_FILES = ("thumbs_history.json", "liked_songs_grouped_by_station.json")
HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")


# ---------- finding the mirror's own modules ----------
def newest(prefix):
    sys.path.insert(0, os.getcwd())
    found = []
    for f in glob.glob(prefix + "_v*.py"):
        m = re.match(re.escape(prefix) + r"_v(\d+)\.py$", os.path.basename(f))
        if m:
            found.append((int(m.group(1)), f[:-3]))
    return importlib.import_module(max(found)[1]) if found else None


# ---------- small helpers ----------
def mask(s):
    return "(empty)" if not s else (s[:3] + "..." + s[-2:] if len(s) > 8 else "saved")


def ask(prompt, current=None):
    shown = f" [{current}]" if current else ""
    a = input(f"{prompt}{shown}: ").strip()
    return a if a else (current or "")


def yes(prompt, default=True):
    a = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not a else a in ("y", "yes")


def parse_numbers(text, upto):
    """'1,3, 5-7' -> [1,3,5,6,7]; raises ValueError on anything else."""
    out = set()
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(part)
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if a < 1 or b > upto or a > b:
            raise ValueError(part)
        out.update(range(a, b + 1))
    return sorted(out)


# ---------- Last.fm ----------
LASTFM = "https://ws.audioscrobbler.com/2.0/"


def lastfm_call(params, post=False):
    params = dict(params, format="json")
    body = urllib.parse.urlencode(params)
    req = urllib.request.Request(LASTFM + ("" if post else "?" + body), data=body.encode() if post else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {"error": e.code, "message": f"HTTP {e.code}"}


def lastfm_sig(params, secret):
    """Last.fm's request signature: md5 of all parameters (except format), sorted by name, written name+value, plus the secret."""
    raw = "".join(k + str(params[k]) for k in sorted(params) if k not in ("format", "callback")) + secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def lastfm_verify(l):
    """Returns a list of (ok, message) lines."""
    out = []
    user, key, secret = l.get("username", ""), l.get("api_key", ""), l.get("api_secret", "")
    if key and not HEX32.match(key):
        out.append((False, "API key is not 32 letters/digits (hex); it was probably copied wrong"))
    if secret and not HEX32.match(secret):
        out.append((False, "API secret is not 32 letters/digits (hex); it was probably copied wrong"))
    try:
        if user and key:
            r = lastfm_call({"method": "user.getinfo", "user": user, "api_key": key})
            out.append((("error" not in r), "username + API key: " + (r.get("message", "") + " (error %s)" % r["error"] if "error" in r else "OK")))
        if key and secret:
            p = {"method": "auth.getToken", "api_key": key}
            p["api_sig"] = lastfm_sig(p, secret)
            r = lastfm_call(p)
            out.append((("error" not in r), "API key + secret pair: " + (r.get("message", "") + " (error %s)" % r["error"] if "error" in r else "OK")))
    except Exception as e:
        out.append((None, f"could not reach Last.fm to check ({e}); the values are still saved"))
    return out


# ---------- the editor ----------
class Editor:
    def __init__(self):
        self.sp = newest("spotify_player")
        self.lib = newest("mirror_library")
        self.cfg = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, encoding="utf-8") as f:
                    self.cfg = json.load(f)
            except Exception as e:
                sys.exit(f"{CONFIG_FILE} exists but is not valid JSON ({e}). Fix or delete it, then run this again.")
        self.original = copy.deepcopy(self.cfg)
        self.me = None            # {"id", "name"} once fetched
        self.mine = None          # all your playlists, once fetched

    # --- Spotify ---
    def spotify_ready(self, quiet=False):
        sp = self.sp
        if sp is None:
            if not quiet:
                print("  I can't find spotify_player_v<number>.py in this folder.")
            return False
        if sp.logged_in() and sp.has_scope("playlist-read-private"):
            return True
        if quiet:
            return False
        print("  " + ("Logged in, but without permission to read playlists." if sp.logged_in() else "Not logged in to Spotify."))
        if yes("  Open the Spotify login in your browser now?"):
            try:
                sp.login()
            except Exception as e:
                print(f"  Login did not finish: {e}")
        return sp.logged_in() and sp.has_scope("playlist-read-private")

    def load_me(self):
        if self.me is None and self.spotify_ready(quiet=True):
            try:
                me = self.sp.api("GET", "/me")
                self.me = {"id": me.get("id"), "name": me.get("display_name") or me.get("id")}
            except Exception as e:
                print(f"  (Spotify did not answer: {e})")
        return self.me

    def load_playlists(self, force=False):
        if self.mine is not None and not force:
            return True
        if not self.spotify_ready() or not self.load_me():
            return False
        out, offset = [], 0
        try:
            while True:
                page = self.sp.api("GET", "/me/playlists", {"limit": 50, "offset": offset})
                out += [p for p in page.get("items", []) if p]
                offset += 50
                if not page.get("next"):
                    break
        except Exception as e:
            print(f"  Could not read your playlists ({e}).")
            return False
        self.mine = out
        return True

    # --- status ---
    def spotify_status(self):
        sp = self.sp
        if sp is None:
            return False, "spotify_player_v<number>.py not found in this folder"
        if not sp.logged_in():
            return False, "not logged in"
        if not sp.has_scope("playlist-read-private"):
            return False, "logged in, but no permission to read playlists (log in again)"
        me = self.load_me()
        return True, "logged in" + (f" as {me['name']}" if me else "") + ", playlist permission OK"

    def thumbs_status(self):
        found = [f for f in THUMBS_FILES if os.path.exists(f)]
        if not found:
            return None, "no thumbs file next to the receiver yet (thumbs_history.json)"
        n = None
        if self.lib is not None:
            try:
                seen = set()
                for f in found:
                    with open(f, encoding="utf-8") as fh:
                        data = json.load(fh)
                    for st, t, a, v in self.lib._records(data, None, "up" if f == THUMBS_FILES[1] else None):
                        if v in ("up", "down"):
                            seen.add((self.lib.key(t, a), v))
                n = len(seen)
            except Exception as e:
                return False, f"{', '.join(found)} found but could not be read ({e})"
        return True, f"{', '.join(found)} found" + (f", {n} songs with a readable thumb" if n is not None else "")

    def lines(self):
        c = self.cfg
        ok, msg = self.spotify_status()
        names = c.get("_playlist_names") or {}
        pl = c.get("playlists") or []
        pands = (c.get("pandora") or {}).get("username")
        l = c.get("lastfm") or {}
        tok, tmsg = self.thumbs_status()
        mark = lambda v: "ok " if v else ("-- " if v is None else "!! ")
        return [
            ("1", "Spotify account", mark(ok) + msg),
            ("2", "Playlists watched", mark(bool(pl)) + (f"{len(pl)}: " + ", ".join((names.get(p) or str(p))[:28] for p in pl[:4]) + (" ..." if len(pl) > 4 else "") if pl else "none chosen yet")),
            ("3", "Pandora username", mark(bool(pands)) + (pands or "not set")),
            ("4", "Last.fm", mark(bool(l.get("username") and HEX32.match(l.get("api_key") or "") and HEX32.match(l.get("api_secret") or ""))) +
             f"user {l.get('username') or '(empty)'}, key {mask(l.get('api_key'))}, secret {'saved' if l.get('api_secret') else '(empty)'}"),
            ("5", "Pandora thumbs data", mark(tok) + tmsg),
        ]

    # --- sections ---
    def edit_pandora(self):
        p = self.cfg.setdefault("pandora", {})
        print("\nYour Pandora username is the last part of your thumbs page address: pandora.com/profile/thumbs/<username>.")
        v = ask("Pandora username (or paste that address)", p.get("username"))
        m = re.search(r"/(?:thumbs|profile)/(?:thumbs/)?([^/?#\s]+)", v)
        p["username"] = m.group(1) if m and "/" in v else v

    def edit_lastfm(self):
        l = self.cfg.setdefault("lastfm", {})
        print("\nCreate an API account at https://www.last.fm/api/account/create (any name and description).")
        print("It shows an 'API key' and a 'Shared secret'. Enter keeps what is saved.")
        l["username"] = ask("Last.fm username", l.get("username"))
        l["api_key"] = ask("Last.fm API key", l.get("api_key"))
        sec = getpass.getpass("Last.fm shared secret (hidden as you type or paste" + ("; Enter keeps the saved one" if l.get("api_secret") else "") + "): ").strip()
        if sec:
            l["api_secret"] = sec
        l.setdefault("api_secret", "")
        print("Checking with Last.fm ...")
        for ok, msg in lastfm_verify(l):
            print(("  ok   " if ok else "  --   " if ok is None else "  FAIL ") + msg)

    def edit_playlists(self):
        if not self.load_playlists():
            return
        mine, me_id = self.mine, self.me["id"]
        current = [str(x) for x in (self.cfg.get("playlists") or [])]
        ids = {p["id"] for p in mine}

        def resolve(c):
            m = re.search(r"playlist[/:]([A-Za-z0-9]{22})", c)
            cid = m.group(1) if m else c
            if cid in ids:
                return cid
            return next((p["id"] for p in mine if (p.get("name") or "").lower() == c.lower()), None)

        selected, gone = set(), []
        for c in current:
            r = resolve(c)
            (selected.add(r) if r else gone.append(c))
        readable = {p["id"] for p in mine if (p.get("owner") or {}).get("id") == me_id or p.get("collaborative")}
        counts = {}
        for p in mine:
            counts[(p.get("name") or "").lower()] = counts.get((p.get("name") or "").lower(), 0) + 1
        flt = ""
        print("\nPlaylists. Commands:  numbers/ranges toggle (3 5 8-10) | f <text> filter by name, f alone clears | all | none | done | cancel")
        print("Only playlists you own or collaborate on can be read by the mirror. Saved by id, so duplicate names are fine.")
        while True:
            shown = [(i, p) for i, p in enumerate(mine, 1) if flt in (p.get("name") or "").lower()]
            for i, p in shown:
                total = (p.get("items") or p.get("tracks") or {}).get("total")
                notes = []
                if p["id"] not in readable:
                    notes.append("someone else's: can't be read")
                if counts[(p.get("name") or "").lower()] > 1:
                    notes.append("name used more than once")
                if p.get("public") is False:
                    notes.append("private")
                print(f"  {'[x]' if p['id'] in selected else '[ ]'} {i:>3}. {p.get('name')}  ({total if total is not None else '?'} songs)" + (f"   - {'; '.join(notes)}" if notes else ""))
            if gone:
                print("  Saved but no longer on your account (dropped when you finish): " + ", ".join(g[:12] for g in gone))
            print(f"  {len(selected)} selected" + (f"; filter: '{flt}'" if flt else ""))
            cmd = input("> ").strip()
            low = cmd.lower()
            if low == "done":
                break
            if low == "cancel":
                return
            if low == "all":
                selected |= {p["id"] for _, p in shown if p["id"] in readable}
            elif low == "none":
                selected -= {p["id"] for _, p in shown}
            elif low == "f" or low.startswith("f "):
                flt = low[2:].strip()
            else:
                try:
                    for n in parse_numbers(cmd, len(mine)):
                        pid = mine[n - 1]["id"]
                        if pid not in readable:
                            print(f"  #{n} is someone else's playlist; Spotify won't let the mirror read it.")
                        elif pid in selected:
                            selected.discard(pid)
                        else:
                            selected.add(pid)
                except ValueError:
                    print("  Didn't understand that. Examples: 3   2,5,8-10   f rap   all   done")
        order = [p["id"] for p in mine if p["id"] in selected]
        self.cfg["playlists"] = order
        self.cfg["_playlist_names"] = {p["id"]: p.get("name") for p in mine if p["id"] in selected}

    def thumbs_help(self):
        print("\nThumbs history is read from thumbs_history.json next to the receiver, as a list like:")
        print('  [{"station": "Cruel Hand Radio", "title": "House Arrest", "artist": "Cruel Hand", "vote": "up"}, ...]   (vote: "up" or "down")')
        print("The community export liked_songs_grouped_by_station.json (thumbs up only) is read too.")
        ok, msg = self.thumbs_status()
        print("Right now: " + msg)

    # --- saving ---
    def changes(self):
        def view(c):
            c = copy.deepcopy(c)
            l = c.get("lastfm")
            if l:
                l["api_key"], l["api_secret"] = mask(l.get("api_key")), ("saved" if l.get("api_secret") else "(empty)")
            c.pop("_playlist_names", None)
            return c
        a, b = view(self.original), view(self.cfg)
        out = []
        for k in sorted(set(a) | set(b)):
            if a.get(k) != b.get(k):
                out.append(f"  {k}: {json.dumps(a.get(k), ensure_ascii=False)}  ->  {json.dumps(b.get(k), ensure_ascii=False)}")
        return out

    def save(self):
        diff = self.changes()
        if not diff:
            print("Nothing changed.")
            return True
        print("Changes to save (keys and secrets masked):")
        print("\n".join(diff))
        if not yes("Save these?"):
            return False
        self.cfg.setdefault("playlists", [])
        if os.path.exists(CONFIG_FILE):
            shutil.copyfile(CONFIG_FILE, f"{CONFIG_FILE}.{datetime.datetime.now():%Y%m%d-%H%M%S}.bak")
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, indent=1, ensure_ascii=False)
        try:
            os.chmod(CONFIG_FILE, 0o600)
        except OSError:
            pass
        self.original = copy.deepcopy(self.cfg)
        print(f"Saved {CONFIG_FILE} (old copy kept as a dated .bak). Restart the receiver to use it; playlists are picked up within a minute.")
        return True

    def run(self):
        while True:
            print("\n=== Pandora Mirror setup ===")
            for k, name, status in self.lines():
                print(f"  {k}  {name:<20} {status}")
            dirty = bool(self.changes())
            print(f"  s  Save{' (unsaved changes)' if dirty else ''}      q  Quit{' without saving' if dirty else ''}")
            c = input("Choose: ").strip().lower()
            if c == "1":
                if self.spotify_ready():
                    self.me = None
                    print("  " + self.spotify_status()[1])
            elif c == "2":
                self.edit_playlists()
            elif c == "3":
                self.edit_pandora()
            elif c == "4":
                self.edit_lastfm()
            elif c == "5":
                self.thumbs_help()
            elif c == "s":
                if self.save():
                    return
            elif c == "q":
                if dirty and not yes("Quit and throw away the unsaved changes?", default=False):
                    continue
                print("Nothing was changed on disk." if dirty else "Bye.")
                return


def main():
    ed = Editor()
    if "--status" in sys.argv:
        for k, name, status in ed.lines():
            print(f"{name:<20} {status}")
        return
    ed.run()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled; nothing was changed.")
