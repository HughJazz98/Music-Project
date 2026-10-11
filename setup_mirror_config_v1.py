#!/usr/bin/env python3
"""
One-off setup for mirror_config.json (run it once, and again whenever you want to change something).

    python setup_mirror_config_v1.py

Run it in the same folder as the receiver and spotify_player_v*.py. It asks, in order:
  1. Spotify   - makes sure you are logged in with permission to read playlists (opens the login if not), then shows your
                 playlists as a numbered list so you can pick the ones the mirror should watch.
  2. Pandora   - your Pandora username (used for the link to your thumbs page).
  3. Last.fm   - username, API key and API secret (create the key at https://www.last.fm/api/account/create).
Press Enter on any question to keep what is already in the file. Your old file is saved as mirror_config.json.bak first.
Anything in the file this script does not know about is left alone. The secret is typed hidden and is never printed.
Standard library only. Keep mirror_config.json private (it holds your Last.fm secret).
"""
import getpass
import glob
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


def load_player():
    """Import the newest spotify_player_v<N>.py in this folder."""
    sys.path.insert(0, os.getcwd())
    found = []
    for f in glob.glob("spotify_player_v*.py"):
        m = re.match(r"spotify_player_v(\d+)\.py$", os.path.basename(f))
        if m:
            found.append((int(m.group(1)), f[:-3]))
    if not found:
        sys.exit("I can't find spotify_player_v<number>.py in this folder. Run this script from the folder with the mirror files.")
    return importlib.import_module(max(found)[1])


def ask(prompt, current=None):
    shown = f" [{current}]" if current else ""
    answer = input(f"{prompt}{shown}: ").strip()
    return answer if answer else (current or "")


def ask_secret(prompt, have):
    note = " (you won't see it as you type or paste; Enter keeps the saved one)" if have else " (you won't see it as you type or paste)"
    answer = getpass.getpass(f"{prompt}{note}: ").strip()
    return answer


def pick(count, default_all):
    """Parse '1,3,5-7' / 'all' / '' into a sorted list of 1-based numbers."""
    while True:
        raw = input("Numbers to watch (like 1,4,7-9), 'all', or Enter to keep what you have: ").strip().lower()
        if not raw:
            return None
        if raw == "all":
            return list(default_all)
        out, ok = set(), True
        for part in raw.replace(" ", "").split(","):
            m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
            if not m:
                ok = False
                break
            a, b = int(m.group(1)), int(m.group(2) or m.group(1))
            if a < 1 or b > count or a > b:
                ok = False
                break
            out.update(range(a, b + 1))
        if ok:
            return sorted(out)
        print(f"  That didn't parse. Use numbers between 1 and {count}, like 2,5,8-10.")


def spotify_section(sp, cfg):
    print("\n=== Spotify ===")
    if not sp.logged_in() or not sp.has_scope("playlist-read-private"):
        print("You are not logged in to Spotify with permission to read playlists yet." if sp.logged_in()
              else "You are not logged in to Spotify yet.")
        if input("Open the Spotify login in your browser now? [Y/n]: ").strip().lower() in ("", "y", "yes"):
            sp.login()
        if not sp.logged_in() or not sp.has_scope("playlist-read-private"):
            print("Skipping playlists (no playlist permission). Run this script again after logging in.")
            return
    try:
        me = sp.api("GET", "/me")
        my_id = me.get("id")
        print(f"Logged in as {me.get('display_name') or my_id}.")
        mine, offset = [], 0
        while True:
            page = sp.api("GET", "/me/playlists", {"limit": 50, "offset": offset})
            mine += [p for p in page.get("items", []) if p]
            offset += 50
            if not page.get("next"):
                break
    except Exception as e:
        print(f"Could not read your playlists from Spotify ({e}). Skipping; run this again later.")
        return

    current = [str(x) for x in (cfg.get("playlists") or [])]

    def is_current(p):
        return any(c == p["id"] or c.lower() == (p.get("name") or "").lower() or p["id"] in c for c in current)

    readable = []
    print("\nYour playlists (only ones you own or collaborate on can be read by the mirror):")
    for i, p in enumerate(mine, 1):
        owner = p.get("owner") or {}
        yours = owner.get("id") == my_id or p.get("collaborative")
        if yours:
            readable.append(i)
        tags = ("  <- currently watched" if is_current(p) else "") + ("" if yours else "  (someone else's: can't be read)")
        print(f"  {i:>3}. {p.get('name')}   [{p['id'][:6]}...]{tags}")
    chosen = pick(len(mine), readable)
    if chosen is None:
        print("Keeping the playlists already in the file.")
        return
    dropped = [i for i in chosen if i not in readable]
    chosen = [i for i in chosen if i in readable]
    if dropped:
        print("  Left out (someone else's playlists, Spotify won't let the mirror read them): " + ", ".join(str(i) for i in dropped))
    cfg["playlists"] = [mine[i - 1]["id"] for i in chosen]
    cfg["_playlist_names"] = {mine[i - 1]["id"]: mine[i - 1].get("name") for i in chosen}   # just so you can read the ids above
    print(f"Watching {len(chosen)} playlist(s). (Ids are saved, so duplicate names such as two 'Dad Rock' playlists are not a problem.)")


def pandora_section(cfg):
    print("\n=== Pandora ===")
    print("Your Pandora username is the last part of your thumbs page address: pandora.com/profile/thumbs/<username>")
    p = cfg.setdefault("pandora", {})
    p["username"] = ask("Pandora username", p.get("username"))


def lastfm_check(user, key):
    url = "https://ws.audioscrobbler.com/2.0/?" + urllib.parse.urlencode({"method": "user.getinfo", "user": user, "api_key": key, "format": "json"})
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            data = json.load(e)
        except Exception:
            return f"Last.fm answered HTTP {e.code}"
    except Exception as e:
        return f"could not reach Last.fm to check ({e}); saved anyway"
    if "error" in data:
        return f"Last.fm says: {data.get('message')} (error {data['error']})"
    return None


def lastfm_section(cfg):
    print("\n=== Last.fm ===")
    print("Create an API account at https://www.last.fm/api/account/create (any name and description). It shows an API key and a")
    print("shared secret. Paste them here. Press Enter to leave anything unchanged or empty.")
    l = cfg.setdefault("lastfm", {})
    l["username"] = ask("Last.fm username", l.get("username"))
    key = ask("Last.fm API key", l.get("api_key"))
    l["api_key"] = key
    secret = ask_secret("Last.fm API secret", bool(l.get("api_secret")))
    if secret:
        l["api_secret"] = secret
    l.setdefault("api_secret", "")
    if l["username"] and l["api_key"]:
        problem = lastfm_check(l["username"], l["api_key"])
        print("Last.fm check: " + (problem or "username and API key work."))


def main():
    sp = load_player()
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            sys.exit(f"{CONFIG_FILE} exists but is not valid JSON ({e}). Fix or delete it, then run this again.")
        shutil.copyfile(CONFIG_FILE, CONFIG_FILE + ".bak")
        print(f"Found {CONFIG_FILE}; the old copy is saved as {CONFIG_FILE}.bak")
    spotify_section(sp, cfg)
    pandora_section(cfg)
    lastfm_section(cfg)
    cfg.setdefault("playlists", [])
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1, ensure_ascii=False)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass
    l = cfg.get("lastfm", {})
    print(f"\nSaved {CONFIG_FILE}:")
    print(f"  playlists watched : {len(cfg['playlists'])}")
    print(f"  Pandora username  : {cfg.get('pandora', {}).get('username') or '(empty)'}")
    print(f"  Last.fm           : username {l.get('username') or '(empty)'}, API key {'saved' if l.get('api_key') else 'empty'}, secret {'saved' if l.get('api_secret') else 'empty'}")
    print("Restart the receiver (python pandora_receiver_v11.py) to use it; the playlists are picked up within a minute.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled; nothing was changed.")
