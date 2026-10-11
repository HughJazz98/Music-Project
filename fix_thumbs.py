#!/usr/bin/env python3
"""
fix_thumbs.py - tidy the Pandora thumbs files in Thumbs/ and teach the mirror to read that folder.

    python fix_thumbs.py --dry-run      show what would change, write nothing
    python fix_thumbs.py                do it

Run it in the Playlist folder (the one that contains Thumbs/ and mirror_library_v11.py). Standard library only.

What it does
  1. STATION: the station name is taken from each file's name ("Em Likes.json" -> "Em"), or from STATION_NAMES below if
     you list a nicer name there. It is written into the file's top-level "station" and into every track's "station",
     replacing the wrong values the scraper left behind ("Upgrade", "My CollectionBrowse", ...).
  2. VOTE: the vote stays whatever each track already says. Only a track with no vote gets one from the file name
     (Likes -> up, Dislikes -> down). Tracks whose vote disagrees with their file name are reported, not changed.
  3. CLEANUP: rows titled "This content is no longer on Pandora" are removed. "count" is updated; "expectedCount" is left
     alone, so a file that is still incomplete keeps showing the gap. "removedPlaceholders" records how many were dropped.
  4. LOADER: mirror_library_v11.py is patched so load_thumbs() reads every *.json in Thumbs/ (plus the two older single
     files if they exist). It also remembers each song's vote per station ("by_station") and prints how many songs were
     both liked and disliked (on the same or different stations).

Safety: every file is copied into Thumbs/_backup_<time>/ (the loader to mirror_library_v11.py.bak-<time>) before it is
changed, files that need no change are not touched, and running it twice is harmless.
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys
from datetime import datetime

THUMBS_DIR = "Thumbs"
LIBRARY_FILE = "mirror_library_v11.py"

# File-name stem -> station name to write into the data. Anything not listed here uses the stem itself.
# Example:  "Em": "Eminem Radio",
STATION_NAMES = {
}

PLACEHOLDER_TITLES = {"this content is no longer on pandora"}
SUFFIX = re.compile(r"[\s_-]+(dislikes?|disikes?|likes?)$", re.I)     # also catches the "Disikes" typo


def split_name(path):
    """'Bassnectar Disikes.json' -> ('Bassnectar', 'down'); 'Em Likes.json' -> ('Em', 'up'); no suffix -> (stem, None)."""
    stem = os.path.splitext(os.path.basename(path))[0].strip()
    m = SUFFIX.search(stem)
    if not m:
        return STATION_NAMES.get(stem, stem), None
    base = stem[:m.start()].strip()
    return STATION_NAMES.get(base, base), ("down" if m.group(1).lower().startswith("dis") else "up")


def fix_file(path):
    """Returns (new_data, report_lines). new_data is None when the file cannot be understood."""
    station, file_vote = split_name(path)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    wrapped = isinstance(data, dict)
    tracks = data.get("tracks") if wrapped else data
    if not isinstance(tracks, list):
        return None, ["  skipped: no list of tracks found"]
    report, removed, mismatched, kept = [], 0, 0, []
    for t in tracks:
        if not isinstance(t, dict):
            kept.append(t)
            continue
        if str(t.get("title", "")).strip().lower() in PLACEHOLDER_TITLES:
            removed += 1
            continue
        t["station"] = station
        if not t.get("vote"):
            if file_vote:
                t["vote"] = file_vote
        elif file_vote and str(t["vote"]).lower() != file_vote:
            mismatched += 1
        kept.append(t)
    if wrapped:
        data["station"] = station
        if file_vote and not data.get("vote"):
            data["vote"] = file_vote
        data["tracks"] = kept
        data["count"] = len(kept)
        if removed or data.get("removedPlaceholders"):
            data["removedPlaceholders"] = int(data.get("removedPlaceholders") or 0) + removed
        exp = data.get("expectedCount")
        if isinstance(exp, int) and exp != len(kept) + int(data.get("removedPlaceholders") or 0):
            report.append(f"  NOTE: expectedCount is {exp} but the file holds {len(kept)} tracks"
                          f" (+{data.get('removedPlaceholders') or 0} placeholders removed): still incomplete?")
    else:
        data = kept
    report.insert(0, f"  station -> \"{station}\"; {len(kept)} tracks kept, {removed} placeholder rows removed")
    if mismatched:
        report.append(f"  NOTE: {mismatched} tracks have a vote that disagrees with the file name (left as they are)")
    return data, report


NEW_LOADER = '''def _thumb_sources():
    """(path, default_vote) for every thumbs file: the Thumbs/ folder first, then the two older single files."""
    out = []
    if os.path.isdir(THUMBS_DIR):
        out += [(p, None) for p in sorted(glob.glob(os.path.join(THUMBS_DIR, "*.json")))]
    out += [(f, v) for f, v in ((THUMBS_FILE, None), (THUMBS_FILE_COMMUNITY, "up")) if os.path.exists(f)]
    return out


def load_thumbs():
    """Read every thumbs file (Thumbs/*.json and the older single files); otherwise leave the placeholder in place."""
    found = _thumb_sources()
    if not found:
        return
    out, skipped, placeholders, votes_seen = {}, 0, 0, {}
    for fname, default_vote in found:
        try:
            with open(fname, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            _say(f"could not read {fname}: {e}")
            continue
        hint = None
        if isinstance(data, dict) and isinstance(data.get("tracks"), list):      # the per-station files in Thumbs/
            hint, data = data.get("station"), data["tracks"]
        for station, title, artist, vote in _records(data, hint, default_vote):
            if vote not in ("up", "down"):
                skipped += 1
                continue
            if title.strip().lower() in _PLACEHOLDER_TITLES:
                placeholders += 1
                continue
            k = key(title, artist)
            e = out.setdefault(k, {"vote": vote, "stations": [], "by_station": {}})
            e["vote"] = vote                                 # if a song is voted both ways, the file read last decides
            votes_seen.setdefault(k, set()).add(vote)
            if station:
                if station not in e["stations"]:
                    e["stations"].append(station)
                e["by_station"][station] = vote
    mixed = sum(1 for s in votes_seen.values() if len(s) > 1)
    with _lock:
        thumbs.clear()
        thumbs.update(out)
    ups = sum(1 for v in out.values() if v["vote"] == "up")
    thumbs_info.update(state="loaded", message="", count=len(out))
    _say(f"THUMBS loaded {len(out)} songs ({ups} up, {len(out) - ups} down) from {len(found)} files"
         + (f"; {mixed} songs were both liked and disliked (the file read last wins)" if mixed else "")
         + (f"; {skipped} entries had no readable vote and were skipped" if skipped else "")
         + (f"; {placeholders} placeholder rows ignored" if placeholders else ""))


'''


def patch_loader(stamp, dry):
    if not os.path.exists(LIBRARY_FILE):
        return [f"  {LIBRARY_FILE} not found in this folder: loader not patched"]
    with open(LIBRARY_FILE, encoding="utf-8") as f:
        src = f.read()
    if "def _thumb_sources(" in src:
        return ["  loader already patched, left alone"]
    start, end = src.find("def load_thumbs():"), src.find("def thumb_for(")
    anchor = re.search(r'^THUMBS_FILE_COMMUNITY = .*$', src, re.M)
    if start < 0 or end < start or not anchor:
        return ["  could not find load_thumbs() / THUMBS_FILE_COMMUNITY in the loader: NOT patched (send me the file)"]
    new = src[:start] + NEW_LOADER + src[end:]
    a = re.search(r'^THUMBS_FILE_COMMUNITY = .*$', new, re.M)
    new = (new[:a.end()] + '\nTHUMBS_DIR = "Thumbs"                 # one file per station and vote, written by fix_thumbs.py'
           + '\n_PLACEHOLDER_TITLES = {"this content is no longer on pandora"}' + new[a.end():])
    if not re.search(r"^import glob$", new, re.M):
        new = new.replace("import json\n", "import glob\nimport json\n", 1)
    try:
        compile(new, LIBRARY_FILE, "exec")
    except SyntaxError as e:
        return [f"  patched loader failed its syntax check ({e}): NOT patched"]
    if dry:
        return ["  would patch load_thumbs() to read Thumbs/*.json"]
    shutil.copy2(LIBRARY_FILE, f"{LIBRARY_FILE}.bak-{stamp}")
    with open(LIBRARY_FILE, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    return [f"  patched load_thumbs() to read Thumbs/*.json (old copy: {LIBRARY_FILE}.bak-{stamp})"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    args = ap.parse_args()
    if not os.path.isdir(THUMBS_DIR):
        sys.exit(f"No {THUMBS_DIR}/ folder here. Run this from the Playlist folder.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(THUMBS_DIR, f"_backup_{stamp}")
    files = sorted(glob.glob(os.path.join(THUMBS_DIR, "*.json")))
    if not files:
        sys.exit(f"No .json files in {THUMBS_DIR}/")
    changed = 0
    print(("DRY RUN: nothing will be written\n" if args.dry_run else "") + f"{len(files)} files in {THUMBS_DIR}/")
    for path in files:
        print(os.path.basename(path))
        try:
            with open(path, encoding="utf-8") as f:
                before = json.load(f)
            after, report = fix_file(path)
        except Exception as e:
            print(f"  skipped: {e}")
            continue
        print("\n".join(report))
        if after is None:
            continue
        if after == before:
            print("  already clean, not touched")
            continue
        changed += 1
        if not args.dry_run:
            os.makedirs(backup, exist_ok=True)
            shutil.copy2(path, os.path.join(backup, os.path.basename(path)))
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(after, f, ensure_ascii=False, indent=1)
                f.write("\n")
    print(f"\n{changed} file(s) {'would change' if args.dry_run else 'changed'}" + ("" if args.dry_run or not changed else f"; originals in {backup}"))
    print("\nLoader")
    print("\n".join(patch_loader(stamp, args.dry_run)))


if __name__ == "__main__":
    main()
