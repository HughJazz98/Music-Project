#!/usr/bin/env python3
"""
Release date and producer from Wikipedia (v14). Standard library only.

    python wiki_v14.py "Lose Yourself" "Eminem" "8 Mile"      prints what it finds for that song (needs internet)

Wikipedia album pages (and song/single pages) have an info box at the top with "Released" and "Producer". This reads it.
  - A normal album: the album's page gives the album's release date and producer(s).
  - A single (the album Pandora gives is the song itself, or "... - Single"): the song's own page gives the song's release
    date and names the album it is on; the producer shown is that ALBUM's. If Wikipedia has no song page, the album is
    taken from MusicBrainz instead (the earliest real album the song appears on).
Results are remembered in wiki_cache_v1.json so each song is looked up once.
"""
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://en.wikipedia.org/w/api.php"
SITE = "https://en.wikipedia.org/wiki/"
UA = "PandoraMirror/0.14 (personal use)"
_last = [0.0]
_lock = threading.Lock()


def norm(s):
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s or "")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def get(params):
    """One Wikipedia API call, kept to a few per second."""
    url = API + "?" + urllib.parse.urlencode(dict(params, format="json", formatversion=2))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with _lock:
        wait = 0.35 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                return json.load(r)
        finally:
            _last[0] = time.time()


# ---------- reading an info box ----------
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_REF = re.compile(r"<ref[^>/]*?/>|<ref[^>]*?>.*?</ref>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_DROP_TPL = {"efn", "sfn", "refn", "cn", "citation needed", "clarify", "when", "who", "nbsp", "small", "citation", "cite web", "cite news"}
_LIST_TPL = {"flatlist", "plainlist", "ubl", "unbulleted list", "hlist", "collapsible list", "bulleted list", "ubli", "plain list", "flat list"}
_INNER = re.compile(r"\{\{([^{}]*)\}\}")


def _strip_markup(text):
    text = _REF.sub("", _COMMENT.sub("", text or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)

    def tpl(m):
        parts = m.group(1).split("|")
        name = parts[0].strip().lower()
        args = [a.strip() for a in parts[1:]]
        if name in _DROP_TPL:
            return ""
        args = [a for a in args if not re.match(r"^(class|style|title|list_style|item_style|indent|df|mf)\s*=", a)]
        if name in _LIST_TPL:
            return "\n".join(args)
        if name in ("nowrap", "nobold", "small", "big", "lang"):
            return args[-1] if args else ""
        return " ".join(a.split("=", 1)[-1] if "=" in a and len(a.split("=", 1)[0]) < 12 else a for a in args)

    text = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", text)      # [[A|B]] -> B, [[A]] -> A (before templates, whose arguments are split on "|")
    prev = None
    while prev != text:                                         # innermost template first
        prev = text
        text = _INNER.sub(tpl, text)
    text = re.sub(r"'{2,}", "", text)
    text = _TAG.sub("", text)
    return text.replace("&amp;", "&").replace("&nbsp;", " ")


def names_from(value):
    """People/company names from an info box field: lists, comma lists, links, '(executive)' notes."""
    out = []
    for raw in re.split(r"[\n*;]|,(?![^()]*\))| & | and (?=[A-Z])", _strip_markup(value)):
        note = " ".join(re.findall(r"\(([^)]*)\)", raw)).lower()
        item = re.sub(r"\([^)]*\)", "", raw).strip(" \t.:-–•")
        if not item or ("executive" in note and "also" not in note) or len(item) > 60 or item.lower() in ("various", "et al", "others"):
            continue
        if item not in out:
            out.append(item)
    return out


_MONTHS = {m: i + 1 for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"])}
_START = re.compile(r"\{\{\s*(?:start[ _]date(?: and age)?|release[ _]date|film[ _]date|bda)\s*\|\s*((?:[a-z]+=[^|}]*\|\s*)*)(\d{4})(?:\s*\|\s*(\d{1,2}))?(?:\s*\|\s*(\d{1,2}))?", re.I)


def date_from(value):
    """'YYYY-MM-DD' (or 'YYYY-MM' / 'YYYY' when Wikipedia only says that) from a Released field, else None."""
    value = _REF.sub("", _COMMENT.sub("", value or ""))
    m = _START.search(value)
    if m:
        y, mo, d = m.group(2), m.group(3), m.group(4)
        return y + (f"-{int(mo):02d}" if mo else "") + (f"-{int(d):02d}" if mo and d else "")
    text = _strip_markup(value)
    mon = "|".join(_MONTHS)
    m = re.search(rf"\b({mon})\s+(\d{{1,2}}),?\s+(\d{{4}})", text, re.I)
    if m:
        return f"{m.group(3)}-{_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
    m = re.search(rf"\b(\d{{1,2}})\s+({mon})\s+(\d{{4}})", text, re.I)
    if m:
        return f"{m.group(3)}-{_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    m = re.search(rf"\b({mon})\s+(\d{{4}})", text, re.I)
    if m:
        return f"{m.group(2)}-{_MONTHS[m.group(1).lower()]:02d}"
    m = re.search(r"\b(1[89]\d\d|20\d\d)\b", text)
    return m.group(1) if m else None


def infobox(wikitext):
    """(kind, {field: raw value}) for the first info box, or (None, {}). kind is 'album', 'song', 'single', ..."""
    wikitext = _COMMENT.sub("", wikitext or "")
    m = re.search(r"\{\{\s*Infobox[ _]([A-Za-z ]+?)\s*(?=[\n|{])", wikitext, re.I)
    if not m:
        return None, {}
    kind, i, depth, start = m.group(1).strip().lower(), m.start(), 0, m.start()
    j = i
    while j < len(wikitext):
        if wikitext.startswith("{{", j) or wikitext.startswith("[[", j):
            depth += 1
            j += 2
        elif wikitext.startswith("}}", j) or wikitext.startswith("]]", j):
            depth -= 1
            j += 2
            if depth == 0:
                break
        else:
            j += 1
    body = wikitext[start + 2:j - 2]
    fields, cur, depth, k = {}, [], 0, 0
    chunks = []
    while k < len(body):
        two = body[k:k + 2]
        if two in ("{{", "[["):
            depth += 1
            cur.append(two)
            k += 2
        elif two in ("}}", "]]"):
            depth -= 1
            cur.append(two)
            k += 2
        elif body[k] == "|" and depth == 0:
            chunks.append("".join(cur))
            cur = []
            k += 1
        else:
            cur.append(body[k])
            k += 1
    chunks.append("".join(cur))
    for c in chunks[1:]:
        if "=" in c:
            key, val = c.split("=", 1)
            fields[key.strip().lower()] = val.strip()
    return kind, fields


# ---------- finding the page ----------
def _page(title):
    d = get({"action": "parse", "page": title, "prop": "wikitext", "section": 0, "redirects": 1})
    return ((d.get("parse") or {}).get("wikitext")) or ""


def find(kind, name, artist, trusted=False):
    """The info box of the Wikipedia page for this album ('album') or song ('song'), checked against the artist, or None."""
    q = f'"{name}" {artist} ' + ("album" if kind == "album" else "song")
    hits = (get({"action": "query", "list": "search", "srsearch": q, "srlimit": 5}).get("query") or {}).get("search") or []
    want, want_a = norm(name), norm(artist)
    for h in hits[:4]:
        title = h.get("title") or ""
        text = _page(title)
        k, f = infobox(text)
        if not k:
            continue
        is_album = "album" in k
        if (kind == "album") != is_album:
            continue
        shown = norm(_strip_markup(f.get("name", ""))) or norm(title)
        if want not in (shown, norm(title)) and not (len(want) > 6 and (want in shown or shown in want)):
            continue
        by = norm(_strip_markup(f.get("artist", ""))) or norm(_strip_markup(f.get("artists", "")))
        if not trusted and want_a and by and want_a not in by and by not in want_a and "various" not in by:
            continue
        return {"title": title, "kind": k, "fields": f}
    return None


_SINGLE_WORDS = re.compile(r"\s*[-–(\[]\s*(single|ep|remix|radio edit)\s*[)\]]?\s*$", re.I)


def single_like(title, album):
    if not album:
        return True
    a = _SINGLE_WORDS.sub("", album)
    return norm(a) == norm(title) or bool(re.search(r"[-–(\[]\s*single\s*[)\]]?\s*$", album, re.I))


def lookup(title, artist, album, parent_album=None):
    """What Wikipedia says. {released, releasedFrom, albumReleased, producers, albumTitle, page, ...}; raises if Wikipedia can't be reached."""
    res = {"released": None, "releasedFrom": None, "albumReleased": None, "producers": [], "producersFrom": None, "albumTitle": None, "page": None}
    album_page, song_prod, direct = None, [], False
    single = single_like(title, album)
    if album and not re.search(r"[-–(\[]\s*single\s*[)\]]?\s*$", album, re.I):
        album_page = find("album", album, artist)
        direct = album_page is not None                      # (an album named like its song, e.g. a self-titled one, is found here too)
    if single or not album_page:
        song = find("song", title, artist)
        if song:
            res["released"], res["releasedFrom"], res["page"] = date_from(song["fields"].get("released", "")), "song", song["title"]
            song_prod = names_from(song["fields"].get("producer", ""))
            link = _strip_markup(song["fields"].get("album", "")).strip()
            if link and not album_page:
                album_page = find("album", link, artist, trusted=True)
    if not album_page and parent_album:
        parent = parent_album() if callable(parent_album) else parent_album       # (asked only now, because it costs a MusicBrainz call)
        if parent:
            album_page = find("album", parent, artist, trusted=True)
    if album_page:
        f = album_page["fields"]
        res["albumTitle"] = _strip_markup(f.get("name", "")).strip() or album_page["title"]
        res["albumReleased"] = date_from(f.get("released", ""))
        res["producers"] = names_from(f.get("producer", ""))
        res["producersFrom"] = "album"
        if not res["page"]:
            res["page"] = album_page["title"]
        if not res["released"] and direct:
            res["released"], res["releasedFrom"] = res["albumReleased"], "album"
    if not res["producers"] and song_prod:
        res["producers"], res["producersFrom"] = song_prod, "song"          # no album page found: the song's own credit is the best there is
    return res


def page_url(title):
    return SITE + urllib.parse.quote((title or "").replace(" ", "_"), safe="()_,'!:")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(0)
    print(json.dumps(lookup(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None), indent=1))
