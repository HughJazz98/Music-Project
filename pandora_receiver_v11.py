#!/usr/bin/env python3
"""
Local receiver for the Pandora userscript (v11 = the legacy receiver + two-way pause/play + a player page that shows the track's metadata).

    python pandora_receiver_v11.py

Listens on http://127.0.0.1:8765, prints every track change the moment it arrives, and
once a track has been playing ~3 s (similar artists known) and a MusicBrainz lookup has
finished, writes one line to nowplaying.txt:

    songName - Artist - Album - releaseDate - genre - similar1, similar2, ...

Every raw event is also appended to events.jsonl. Standard library only.
Open http://127.0.0.1:8765/ in a browser to check that it is running.

v6/v7: the player page also shows cover art, release date, genre, similar artists, the Spotify match, a progress bar and the
    recent tracks. It only reads data the receiver already has; nothing new is asked of Pandora or Spotify on the track path.
v4 additions (the track -> Spotify path above is exactly the legacy one; no polling of Spotify, no extra Spotify calls):
    http://127.0.0.1:8765/player   primitive player: now playing + play/pause + skip
    Pause/play on Pandora's page  -> Spotify pauses/resumes
    Pause/play on the player page -> Spotify first, then Pandora
    Nothing on Spotify's side ever pauses or resumes Pandora.
    Every pause/play decision is printed as SYNC ... and saved to sync_decisions.jsonl.
"""
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import spotify_player_v11 as sp
import mirror_library_v11 as lib
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 8765
EVENTS_FILE = "events.jsonl"
LINES_FILE = "nowplaying.txt"
CACHE_FILE = "enrich_cache_v4.json"   # v4: genre is the album's (MusicBrainz release group); remembers where it came from
# MusicBrainz asks API users to identify themselves with a contact. Put yours here.
USER_AGENT = "PandoraMirrorProofOfLife/0.1 ( put-your-email@example.com )"

SPOTIFY_ON = sp.logged_in()
lock = threading.Lock()
tracks = {}  # pandoraTrackId -> info collected so far for the current play of that track

# Pandora serves edition tags like "(Remastered)" or "(15th Anniversary Edition)"; strip them before searching.
EDITION = re.compile(
    r"\s*[\(\[][^\)\]]*(remaster|deluxe|anniversary|edition|version|mono|stereo|expanded|bonus|explicit)[^\)\]]*[\)\]]",
    re.I,
)


def clean(s):
    return EDITION.sub("", s or "").strip()


# ---------- MusicBrainz lookup (release date + genre), cached on disk ----------
try:
    with open(CACHE_FILE, encoding="utf-8") as f:
        cache = json.load(f)
except Exception:
    cache = {}

mb_lock = threading.Lock()
last_mb_call = 0.0


def save_cache():
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, ensure_ascii=False)


def mb_get(path, params):
    """One MusicBrainz request, kept to about one per second as they require."""
    global last_mb_call
    url = "https://musicbrainz.org/ws/2/" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with mb_lock:
        wait = 1.1 - (time.time() - last_mb_call)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.load(r)
        finally:
            last_mb_call = time.time()


def album_genre(recs, title, artist, album):
    """The album's own genre from MusicBrainz: (genre, release-group id), or (None, rg id/None) if the album has no genres.
    The release group comes from the recording's own release list, else from a release-group search by album + artist."""
    want = sp.norm(clean(album))
    rg = None
    for r in recs:
        for rel in r.get("releases", []):
            g = rel.get("release-group") or {}
            if g.get("id") and sp.norm(clean(rel.get("title", ""))) == want:
                rg = g["id"]
                break
        if rg:
            break
    if not rg:
        data = mb_get("release-group", {"query": f'releasegroup:"{clean(album)}" AND artist:"{artist}"', "fmt": "json", "limit": 5})
        for g in data.get("release-groups", []):
            if sp.norm(clean(g.get("title", ""))) == want:
                rg = g.get("id")
                break
    if not rg:
        return None, None
    info = mb_get(f"release-group/{rg}", {"inc": "genres", "fmt": "json"})
    genres = sorted(info.get("genres", []), key=lambda x: -x.get("count", 0))
    return (genres[0]["name"] if genres else None), rg


def lookup(title, artist, album=None):
    key = f"{clean(title)}|{artist}|{clean(album)}".lower()
    if key in cache:
        return cache[key]
    result = {"releaseDate": None, "genre": None, "genreFrom": None, "rgid": None}
    try:
        query = f'recording:"{clean(title)}" AND artist:"{artist}"'
        data = mb_get("recording", {"query": query, "fmt": "json", "limit": 25})
        recs = data.get("recordings", [])
        want_t, want_a = sp.norm(title), sp.norm(artist)
        cands = []
        for r in recs:
            names = " ".join(c.get("name", "") for c in r.get("artist-credit", []) if isinstance(c, dict))
            if sp.norm(r.get("title", "")) == want_t and want_a in sp.norm(names):
                cands.append(r)
        # Reissues and remasters are separate recordings with later dates, so take the earliest across all exact matches.
        dated = [r for r in cands if re.match(r"^\d{4}", r.get("first-release-date") or "")]
        best = min(dated, key=lambda r: r["first-release-date"]) if dated else (cands[0] if cands else None)
        if best:
            result["releaseDate"] = best.get("first-release-date") or None
            if album:
                try:
                    g, rg = album_genre(recs, title, artist, album)
                except Exception:
                    g, rg = None, None              # the album lookup failing must not lose the date; fall back to the older genre sources
                    result["error"] = "album lookup failed"   # keeps this result out of the cache so it is retried next time
                result["rgid"] = rg
                if g:
                    result["genre"], result["genreFrom"] = g, "album"
            counts = {}
            for r in cands:
                for t in r.get("tags", []):
                    counts[t["name"]] = counts.get(t["name"], 0) + t.get("count", 0)
            if result["genre"]:
                pass                                # the album's own genre wins
            elif counts:
                result["genre"] = max(counts, key=counts.get)
                result["genreFrom"] = "recording"
            else:  # fall back to the artist's own genres
                credit = [c for c in best.get("artist-credit", []) if isinstance(c, dict)]
                aid = credit[0].get("artist", {}).get("id") if credit else None
                if aid:
                    a = mb_get(f"artist/{aid}", {"inc": "genres", "fmt": "json"})
                    genres = sorted(a.get("genres", []), key=lambda g: -g.get("count", 0))
                    if genres:
                        result["genre"] = genres[0]["name"]
                        result["genreFrom"] = "artist"
    except Exception as e:
        result["error"] = str(e)
    if "error" not in result:
        cache[key] = result
        save_cache()
    return result


# ---------- pause / play (v4) ----------
REC_SOURCE = "Pandora"     # which recommendation algorithm picked the current track; filler or other sources will say so here later
SETTLE_SEC = 4.0           # right after a track change Pandora's play state flickers, so it is not trusted for this long
PANDORA_CMD_TTL = 5.0      # a command the userscript has not picked up within this many seconds is dropped
DECISIONS_FILE = "sync_decisions.jsonl"
state_lock = threading.Lock()
state = {"playing": True, "pandora": None, "settle_until": 0.0}   # pandora: last play state Pandora itself reported
now_playing = {"tid": None}   # which tracks[] entry is current (None during an ad)
history = []               # pandoraTrackIds in the order they started, for the player page
clock = {"base": 0.0, "since": None}   # seconds played so far, and when the clock last started (None = stopped); approximate, follows play/pause


def clock_reset(running=True):
    with state_lock:
        clock["base"], clock["since"] = 0.0, (time.time() if running else None)


def clock_anchor(pos_ms, running):
    """Pandora's audio element reported its exact position: use it instead of the receiver's own running count."""
    with state_lock:
        clock["base"] = max(0.0, pos_ms / 1000.0)
        clock["since"] = time.time() if running else None


def clock_set(running):
    with state_lock:
        if running and clock["since"] is None:
            clock["since"] = time.time()
        elif not running and clock["since"] is not None:
            clock["base"] += time.time() - clock["since"]
            clock["since"] = None
cmd_cv = threading.Condition()
cmd_list = []              # (time, command) waiting for the userscript


def decide(msg):
    print(f"[{datetime.now():%H:%M:%S}] SYNC {msg}", flush=True)
    with open(DECISIONS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": datetime.now().isoformat(timespec="milliseconds"), "msg": msg}) + "\n")


# ---------- stations (v10) ----------
STATIONS_FILE = "stations_seen.json"       # every station the Pandora page has shown so far: {name: link}
NOT_STATIONS = {"now playing"}             # links on the page that are not stations
try:
    with open(STATIONS_FILE, encoding="utf-8") as f:
        stations_known = {k: v for k, v in json.load(f).items() if k.lower() not in NOT_STATIONS}
except Exception:
    stations_known = {}
stations_page = []                         # names of the stations the Pandora page lists right now (only a few)
now_station = {"name": None}
pandora_seen = {"t": None}                 # when the Pandora tab's userscript last contacted us
last_cmd = {"t": 0.0, "cmd": None, "ok": None, "reason": None}


def note_stations(current, current_href, listed):
    """Remember stations the page shows. `listed` is [{name, href}] from the page's station links."""
    changed = False
    listed = [x for x in listed if (x.get("name") or "").lower() not in NOT_STATIONS] if listed is not None else None
    for name, href in ([(current, current_href)] if current else []) + [(x.get("name"), x.get("href")) for x in (listed or [])]:
        if name and href and name.lower() not in NOT_STATIONS and stations_known.get(name) != href:
            stations_known[name] = href
            changed = True
    if listed is not None:
        names = [x.get("name") for x in listed if x.get("name")]
        if names != stations_page:
            stations_page[:] = names
            print(f"[{datetime.now():%H:%M:%S}] STATIONS on the Pandora page: {', '.join(names) or '(none found)'}", flush=True)
    if changed:
        with open(STATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(stations_known, f, indent=1, ensure_ascii=False)


def send_pandora(cmd):
    decide(f"-> Pandora command: {cmd}")
    if cmd.startswith("station:") or cmd == "stations_all":                 # Pandora flickers while it changes station; do not let that pause Spotify
        with state_lock:
            state["settle_until"] = time.time() + 6.0
    with cmd_cv:
        cmd_list.append((time.time(), cmd))
        cmd_cv.notify_all()


def next_command(timeout=0.0):
    """The next command for the userscript, or None. Returns (command, issued_at); it stays queued until drop_command().
    timeout=0 means answer immediately. The userscript's requests go through Tampermonkey one after another, so a request
    held open here (as v1-v4 did, up to 20 s) delays every track event queued behind it by the same amount."""
    deadline = time.time() + timeout
    with cmd_cv:
        while True:
            now = time.time()
            cmd_list[:] = [c for c in cmd_list if now - c[0] < PANDORA_CMD_TTL]
            if cmd_list:
                return cmd_list[0][1], cmd_list[0][0]
            left = deadline - now
            if left <= 0:
                return None
            cmd_cv.wait(left)


def drop_command(issued, cmd):
    with cmd_cv:
        for i, c in enumerate(cmd_list):
            if c == (issued, cmd):
                del cmd_list[i]
                return


def set_playing(want, source, why=""):
    """source is 'pandora' (you used Pandora's page) or 'gui' (the player page). Spotify always moves first, then Pandora."""
    with state_lock:
        if state["playing"] == want:
            return False
        state["playing"] = want
    clock_set(want)
    word = "play" if want else "pause"
    decide(f"{word.upper()} from {source}" + (f" ({why})" if why else ""))
    if SPOTIFY_ON:
        if want and sp.in_ad():
            pass                    # Pandora is on an ad; Spotify has nothing to resume
        elif want:
            sp.resume()
        else:
            sp.pause()
    if source != "pandora":
        send_pandora(word)
    return True


def recheck_pandora():
    """A Pandora play/pause that arrived during the track-change window: apply it only if Pandora is still in that state."""
    with state_lock:
        p, settled = state["pandora"], time.time() >= state["settle_until"]
    if settled and p is not None and p != state["playing"]:
        set_playing(p, "pandora", "was held back during a track change and is still true")


def _track_view(tid):
    with lock:
        t = dict(tracks.get(tid) or {})
    m = dict(sp.matched.get(tid) or {}) if SPOTIFY_ON else {}
    return {"tid": tid, "title": t.get("title"), "artist": t.get("artist"), "album": t.get("album"),
            "release": t.get("release"), "genre": t.get("genre"), "genreFrom": t.get("genreFrom"), "station": t.get("station"), "mode": t.get("mode"), "rgUrl": ("https://musicbrainz.org/release-group/" + t["rgid"]) if t.get("rgid") and t.get("genreFrom") == "album" else None, "recSource": REC_SOURCE, "similar": t.get("similar") or [],
            "durationSec": t.get("durationSec"), "match": m.get("label"),
            "art": m.get("art"), "url": m.get("url"),
            "inPlaylists": lib.containing((m.get("uri") or "").split(":")[-1] or None, t.get("title"), t.get("artist")) if t.get("title") else [],
            "thumb": lib.thumb_for(t.get("title"), t.get("artist")) if t.get("title") else None}


def snapshot():
    cur = now_playing["tid"]
    with state_lock:
        elapsed = clock["base"] + (time.time() - clock["since"] if clock["since"] is not None else 0.0)
    out = {"playing": state["playing"], "ad": sp.in_ad(), "spotify": SPOTIFY_ON, "elapsed": round(elapsed, 1),
           "library": lib.summary(),
           "accounts": lib.accounts(None if pandora_seen["t"] is None else time.time() - pandora_seen["t"]),
           "stations": {"current": now_station["name"], "onPage": list(stations_page), "known": sorted(stations_known)},
           "lastCmd": dict(last_cmd),
           "track": _track_view(cur) if cur else None,
           "history": [_track_view(t) for t in reversed(history[-12:-1] if history and history[-1] == cur else history[-12:])]}
    return out


PLAYER_HTML = b"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pandora Mirror</title>
<style>
 :root{--bg:#101012;--card:#1a1a1e;--line:#2a2a30;--fg:#ececf0;--dim:#9a9aa6;--acc:#7aa2ff}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,sans-serif;display:flex;justify-content:center}
 main{width:min(760px,100%);padding:28px 16px 48px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;display:flex;gap:20px}
 #art{width:220px;height:220px;border-radius:10px;background:#26262c;object-fit:cover;flex:none}
 .info{min-width:0;flex:1;display:flex;flex-direction:column}
 #t{font-size:1.5rem;font-weight:650;line-height:1.2;overflow-wrap:anywhere} #a{margin-top:.35rem;font-size:1.05rem}
 #b{margin-top:.15rem;color:var(--dim)}
 .chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:.9rem}
 .chip{font-size:.78rem;padding:3px 9px;border-radius:99px;background:#26262c;color:var(--dim)} .chip b{color:var(--fg);font-weight:600} .chip i{font-style:normal;opacity:.6;margin-left:6px;font-size:.72rem}
 .src{font-size:.72rem;color:var(--dim);opacity:.75;margin-top:6px} h2 i{font-style:normal;text-transform:none;letter-spacing:0;opacity:.7;margin-left:6px}
 #sp{margin-top:.8rem;font-size:.8rem;color:var(--dim)} #sp a{color:var(--acc);text-decoration:none}
 .bar{margin-top:auto;padding-top:1rem} .track{height:5px;background:#2e2e35;border-radius:9px;overflow:hidden} #fill{height:100%;width:0;background:var(--acc)}
 .times{display:flex;justify-content:space-between;font-size:.75rem;color:var(--dim);margin-top:5px;font-variant-numeric:tabular-nums}
 .row{display:flex;gap:10px;margin-top:.8rem} button{font-size:1rem;padding:.55rem 1.3rem;border-radius:.6rem;border:0;background:#2a2a30;color:var(--fg);cursor:pointer}
 button:hover{background:#383840} #pp{background:var(--acc);color:#0b1020;font-weight:600;min-width:6rem}
 h2{font-size:.8rem;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin:26px 0 8px}
 #sim{display:flex;flex-wrap:wrap;gap:6px}
 .h{display:flex;gap:12px;align-items:center;padding:8px 0;border-top:1px solid var(--line)} .h img{width:44px;height:44px;border-radius:6px;background:#26262c;object-fit:cover;flex:none}
 .h .x{min-width:0} .h .n{white-space:nowrap;overflow:hidden;text-overflow:ellipsis} .h .m{font-size:.8rem;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .yr{display:flex;gap:10px;align-items:baseline;padding:7px 0;border-top:1px solid var(--line);font-size:.92rem} .yr:first-child{border-top:0}
 .yr .k{width:110px;flex:none;color:var(--dim);font-size:.8rem} .yr .v{flex:1;min-width:0} .yr .v.ph{color:var(--dim);font-style:italic}
 .yr .tag{font-size:.7rem;color:var(--dim);opacity:.75;white-space:nowrap}
 .st{padding:.4rem .9rem;font-size:.85rem;border-radius:99px;background:#26262c;color:var(--fg)} .st.cur{background:var(--acc);color:#0b1020;font-weight:600} .st:disabled{opacity:.4;cursor:default}
 #stl{display:flex;flex-wrap:wrap;gap:6px}
 #note{display:none;margin:0 0 14px;padding:9px 12px;border-radius:9px;background:#3a2326;color:#ffb4b4;font-size:.88rem}
 .yr a{color:var(--acc);text-decoration:none}
 .ad{color:var(--dim)} .empty{color:var(--dim);font-size:.9rem}
 @media(max-width:560px){.card{flex-direction:column}#art{width:100%;height:auto;aspect-ratio:1}}
</style>
<main>
 <div id="note"></div>
 <div class="card">
  <div><img id="art" alt=""><div class="src" id="artsrc"></div></div>
  <div class="info">
   <div id="t">-</div><div id="a"></div><div id="b"></div>
   <div class="chips" id="chips"></div>
   <div id="rec" class="src" style="font-size:.8rem;margin-top:.7rem"></div>
   <div id="sp"></div>
   <div class="bar"><div class="track"><div id="fill"></div></div><div class="times"><span id="el">0:00</span><span id="du"></span></div>
    <div class="row"><button id="pp">Pause</button><button id="sk">Skip</button></div></div>
  </div>
 </div>
 <h2>Stations<i>from Pandora</i> <button class="st" id="seeall" title="Opens Pandora's own See all list and reads every station from it">See all</button></h2><div id="stl"></div>
 <h2>You</h2><div id="you"></div>
 <h2>Accounts</h2><div id="acc"></div>
 <h2>Similar artists<i>from Pandora</i></h2><div id="sim"></div>
 <h2>Recently played</h2><div id="hist"></div>
 <div class="src" style="margin-top:28px">Release dates and genres: <a href="https://musicbrainz.org" target="_blank" style="color:inherit">MusicBrainz</a>. Genre is the album's genre when MusicBrainz has one.</div>
</main>
<script>
const $=id=>document.getElementById(id);
async function cmd(c,extra){await fetch('/cmd',{method:'POST',body:JSON.stringify(Object.assign({cmd:c},extra||{}))});refresh();}
$('pp').onclick=()=>cmd('toggle'); $('sk').onclick=()=>cmd('skip');
const mmss=s=>{s=Math.max(0,Math.floor(s));return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')};
function chip(label,val,src,href){const c=document.createElement(href?'a':'span');if(href){c.href=href;c.target='_blank';c.style.textDecoration='none';}c.className='chip';c.append(label?label+' ':'');const b=document.createElement('b');b.textContent=val;c.append(b);if(src){const i=document.createElement('i');i.textContent=src;c.append(i);}return c;}
let cur={elapsed:0,dur:0,playing:false,at:0};
function tick(){const e=cur.elapsed+(cur.playing?(Date.now()-cur.at)/1000:0);
 $('el').textContent=mmss(cur.dur?Math.min(e,cur.dur):e); $('fill').style.width=(cur.dur?Math.min(100,e/cur.dur*100):0)+'%';}
function setArt(img,u){if(u){if(img.src!==u)img.src=u;img.style.visibility='visible';}else{img.removeAttribute('src');img.style.visibility='hidden';}}
async function refresh(){try{const s=await (await fetch('/state')).json(); const t=s.track;
 $('pp').textContent=s.playing?'Pause':'Play';
 $('t').textContent=s.ad?'Ad playing on Pandora':(t?t.title:'Waiting for a track...');
 $('a').textContent=t&&!s.ad?(t.artist||''):''; $('b').textContent=t&&!s.ad?(t.album||''):'';
 const ch=$('chips');ch.replaceChildren();
 if(t&&!s.ad){if(t.station)ch.append(chip('Station',t.station,'Pandora'+(t.mode?', '+t.mode:'')));if(t.release)ch.append(chip('Released',t.release,'MusicBrainz'));if(t.genre)ch.append(chip('Genre',t.genre,t.genreFrom==='album'?'MusicBrainz, album':t.genreFrom==='artist'?'MusicBrainz, artist-wide':'MusicBrainz, song',t.rgUrl));}
 $('rec').textContent=t&&!s.ad&&t.recSource?'Recommendation algorithm used to get this track: '+t.recSource:'';
 $('sp').replaceChildren();
 if(t&&!s.ad&&t.match){$('sp').append('Playback on Spotify: '+t.match+'  ');
  if(t.url){const l=document.createElement('a');l.href=t.url;l.target='_blank';l.textContent='Open in Spotify';$('sp').append(l);}}
 setArt($('art'),t&&!s.ad?t.art:null); $('artsrc').textContent=t&&!s.ad&&t.art?'Cover: Spotify':'';
 cur={elapsed:s.elapsed,dur:t&&t.durationSec||0,playing:s.playing,at:Date.now()}; $('du').textContent=cur.dur?mmss(cur.dur):''; tick();
 const note=$('note'),lc=s.lastCmd||{};
 if(lc.ok===false&&Date.now()/1000-lc.t<10){note.style.display='block';note.textContent='Pandora could not '+(lc.cmd==='station'?'switch station':lc.cmd)+': '+(lc.reason||'');}else note.style.display='none';
 const stl=$('stl');stl.replaceChildren();const S=s.stations||{known:[],onPage:[]};
 if(!S.known.length){const e=document.createElement('span');e.className='empty';e.textContent='No stations seen yet. They are picked up from the Pandora page as you listen.';stl.append(e);}
 S.known.forEach(n=>{const b=document.createElement('button');b.className='st'+(n===S.current?' cur':'');b.textContent=n;
  b.disabled=n===S.current;b.title=n===S.current?'Playing now':S.onPage.includes(n)?'Switch to this station':'Not in the short list Pandora shows, so the mirror will open See all to find it';
  b.onclick=()=>cmd('station',{name:n});stl.append(b);});
 const acc=$('acc');acc.replaceChildren();const A=s.accounts||{};
 [['Spotify',A.spotify],['Pandora',A.pandora],['Last.fm',A.lastfm]].forEach(([k,v])=>{if(!v)return;
  const r=document.createElement('div');r.className='yr';const a=document.createElement('div');a.className='k';a.textContent=k;
  const b=document.createElement('div');b.className='v'+(v.state==='on'?'':' ph');b.textContent=v.message;
  if(v.thumbsUrl){b.append(' ');const l=document.createElement('a');l.href=v.thumbsUrl;l.target='_blank';l.textContent='Open your thumbs page';b.append(l);}
  const c=document.createElement('div');c.className='tag';c.textContent=v.state==='on'?'connected':v.state==='partial'?'needs attention':'not connected';r.append(a,b,c);acc.append(r);});
 const you=$('you');you.replaceChildren();
 const L=s.library||{}, P=L.playlists||{}, T=L.thumbs||{};
 function row(k,v,tag,ph){const r=document.createElement('div');r.className='yr';const a=document.createElement('div');a.className='k';a.textContent=k;
  const b=document.createElement('div');b.className='v'+(ph?' ph':'');b.textContent=v;const c=document.createElement('div');c.className='tag';c.textContent=tag||'';r.append(a,b,c);you.append(r);}
 const real=t&&!s.ad;
 if(P.state==='ok'){const n=(P.watching||[]).reduce((a,x)=>a+x.count,0);
  row('Playlists',!real?'':(t.inPlaylists.length?'In your playlist: '+t.inPlaylists.join(', '):'Not in your watched playlists'),'Spotify, '+(P.watching||[]).length+' watched ('+n+' songs)');}
 else row('Playlists',P.message||'Starting...','Spotify',true);
 if(T.state==='loaded')row('Pandora thumbs',!real?'':(t.thumb?(t.thumb.vote==='up'?'Thumbs up':'Thumbs down')+(t.thumb.stations.length?' on '+t.thumb.stations.join(', '):''):'No thumb recorded'),'Pandora export, '+T.count+' songs');
 else row('Pandora thumbs',(T.message||'Not imported yet')+' (placeholder)','Pandora',true);
 row('Scrobbles',((L.scrobbles||{}).message||'')+' (placeholder)','Last.fm',true);
 const sim=$('sim');sim.replaceChildren();
 if(t&&!s.ad&&t.similar.length)t.similar.forEach(x=>{const c=document.createElement('span');c.className='chip';c.textContent=x;sim.append(c);});
 else{const e=document.createElement('span');e.className='empty';e.textContent=s.ad?'':'Shows up about 3 s into a track.';sim.append(e);}
 const h=$('hist');h.replaceChildren();
 if(!s.history.length){const e=document.createElement('div');e.className='empty';e.textContent='Nothing yet.';h.append(e);}
 s.history.forEach(x=>{const r=document.createElement('div');r.className='h';const i=document.createElement('img');setArt(i,x.art);
  const d=document.createElement('div');d.className='x';const n=document.createElement('div');n.className='n';n.textContent=x.title||'?';
  const m=document.createElement('div');m.className='m';m.textContent=[x.artist,x.album,x.release&&x.release.slice(0,4),x.genre,x.inPlaylists&&x.inPlaylists.length?'in your playlist':null].filter(Boolean).join(' | ');
  d.append(n,m);r.append(i,d);h.append(r);});
}catch(e){}}
$('seeall').onclick=()=>cmd('stations_all');
refresh(); setInterval(refresh,1000); setInterval(tick,250);
</script>"""


# ---------- event handling ----------
def finalize(tid):
    """Write the full line once both the page details and the lookup are in."""
    with lock:
        t = tracks.get(tid)
        if not t or t.get("printed") or not (t.get("details") and t.get("enriched")):
            return
        t["printed"] = True
        parts = [
            t.get("title") or "?",
            t.get("artist") or "?",
            t.get("album") or "?",
            t.get("release") or "?",
            t.get("genre") or "?",
            ", ".join(t.get("similar") or []) or "?",
        ]
    line = " - ".join(parts)
    print(f"[{datetime.now():%H:%M:%S}] LINE {line}", flush=True)
    with open(LINES_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def enrich(tid):
    with lock:
        t = tracks.get(tid)
        title, artist, album = (t.get("title"), t.get("artist"), t.get("album")) if t else (None, None, None)
    res = lookup(title, artist, album) if title and artist else {}
    with lock:
        if tid in tracks:
            tracks[tid]["release"] = res.get("releaseDate")
            tracks[tid]["genre"] = res.get("genre")
            tracks[tid]["genreFrom"] = res.get("genreFrom")
            tracks[tid]["rgid"] = res.get("rgid")
            tracks[tid]["enriched"] = True
    finalize(tid)


def handle(ev):
    now_ms = int(time.time() * 1000)
    ev["receivedMs"] = now_ms
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    kind = ev.get("event")
    stamp = datetime.now().strftime("%H:%M:%S")
    lag = now_ms - ev.get("sentMs", now_ms)

    if kind == "ad":
        print(f"[{stamp}] AD  {ev.get('title')}", flush=True)
        if SPOTIFY_ON:
            threading.Thread(target=sp.on_ad, daemon=True).start()
        now_playing["tid"] = None
        clock_reset()
        with state_lock:
            state["playing"], state["pandora"], state["settle_until"] = True, True, time.time() + SETTLE_SEC
    elif kind == "track":
        tid = ev.get("pandoraTrackId")
        if not tid:
            return
        with lock:
            tracks[tid] = {
                "title": ev.get("title"),
                "artist": ev.get("artist"),
                "album": ev.get("album"),
                "station": ev.get("station"),
                "mode": ev.get("mode"),
                "details": False,
                "enriched": False,
                "printed": False,
            }
        if ev.get("station"):
            now_station["name"] = ev["station"]
            note_stations(ev["station"], ev.get("stationHref"), None)
        print(f"[{stamp}] NOW  {ev.get('title')} - {ev.get('artist')} - {ev.get('album') or '?'}   (page to receiver: {lag} ms)", flush=True)
        threading.Thread(target=enrich, args=(tid,), daemon=True).start()
        if SPOTIFY_ON:
            threading.Thread(target=sp.play_for_track, args=(ev,), daemon=True).start()
        # (bookkeeping for pause/play only after the Spotify thread has been started, so the track path stays as fast as the legacy one)
        now_playing["tid"] = tid
        if not history or history[-1] != tid:
            history.append(tid)
            del history[:-50]
        clock_reset()
        with state_lock:
            state["playing"], state["pandora"], state["settle_until"] = True, True, time.time() + SETTLE_SEC
    elif kind == "playstate":
        playing = bool(ev.get("playing"))
        word = "play" if playing else "pause"
        with state_lock:
            state["pandora"] = playing
            wait = state["settle_until"] - time.time()
        if wait > 0:
            decide(f"ignored Pandora {word}: a track just changed (rechecking in {wait + 0.5:.1f}s)")
            threading.Timer(wait + 0.5, recheck_pandora).start()
        else:
            set_playing(playing, "pandora", "Pandora's audio element")
            if ev.get("posMs") is not None:         # outside the track-change window the position is Pandora's real one
                clock_anchor(ev["posMs"], playing)
    elif kind == "cmdResult":
        last_cmd.update(t=time.time(), cmd=ev.get("cmd"), ok=bool(ev.get("ok")), reason=ev.get("reason"))
        if ev.get("ok") and ev.get("cmd") in ("station", "stations_all") and ev.get("reason"):
            print(f"[{stamp}] STATIONS {ev.get('cmd')}: {ev.get('reason')}", flush=True)
        if not ev.get("ok"):
            print(f"[{stamp}] PANDORA COULD NOT {str(ev.get('cmd')).upper()}: {ev.get('reason')}", flush=True)
    elif kind == "stations":                    # the full list, after "See all"
        note_stations(None, None, ev.get("stations") or [])
    elif kind == "details":
        tid = ev.get("pandoraTrackId")
        with lock:
            t = tracks.get(tid)
            if not t:
                return
            t["similar"] = ev.get("similar") or []
            t["durationSec"] = ev.get("durationSec")
            if ev.get("album"):
                t["album"] = ev["album"]
            t["details"] = True
        if ev.get("station") is not None or ev.get("stations") is not None:
            if ev.get("station"):
                now_station["name"] = ev["station"]
            note_stations(ev.get("station"), ev.get("stationHref"), ev.get("stations"))
        if SPOTIFY_ON:
            sp.note_details(tid, ev.get("durationSec"))
        finalize(tid)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/cmd":                      # the userscript polls this: 200 {"cmd","issuedMs"}, or 204 right away when idle
            pandora_seen["t"] = time.time()
            got = next_command()
            if got is not None:
                drop_command(got[1], got[0])    # taken off the queue BEFORE sending, so the immediate re-poll can't get it again
            try:
                if got is None:
                    self._send(204)
                else:
                    name, _, arg = got[0].partition(":")        # "station:Cruel Hand Radio" -> cmd "station", arg "Cruel Hand Radio"
                    out = {"cmd": name, "issuedMs": int(got[1] * 1000)}
                    if arg:
                        out["arg"] = arg
                    self._send(200, json.dumps(out).encode())
            except OSError:
                if got is not None:             # that poll was already gone: put the command back for the next one
                    with cmd_cv:
                        cmd_list.insert(0, (got[1], got[0]))
                        cmd_cv.notify_all()
        elif path == "/state":
            self._send(200, json.dumps(snapshot()).encode())
        elif path == "/player":
            self._send(200, PLAYER_HTML, "text/html; charset=utf-8")
        else:
            self._send(200, b"pandora receiver is running\n", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        path = urllib.parse.urlparse(self.path).path
        if path == "/cmd":                      # buttons on the player page
            try:
                cmd = json.loads(raw).get("cmd")
                if cmd == "toggle":
                    set_playing(not state["playing"], "gui")
                elif cmd in ("play", "pause"):
                    set_playing(cmd == "play", "gui")
                elif cmd in ("skip", "thumbs_up", "thumbs_down"):
                    send_pandora(cmd)
                elif cmd == "stations_all":
                    send_pandora("stations_all")
                elif cmd == "station":
                    name = str(json.loads(raw).get("name") or "").strip()
                    if name:
                        send_pandora("station:" + name)
                self._send(200, json.dumps(snapshot()).encode())
            except Exception as e:
                print("bad command:", e, flush=True)
                self._send(400, b"{}")
            return
        self._send(204)                         # events from the userscript, handled exactly as in the legacy receiver
        try:
            handle(json.loads(raw))
        except Exception as e:
            print("bad event:", e, flush=True)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    if not SPOTIFY_ON:
        print("Spotify not logged in yet, so only logging. To enable playback run:  python spotify_player.py login", flush=True)
    lib.start(SPOTIFY_ON)                        # thumbs files + (when logged in to Spotify) the playlist watcher, one Spotify call a minute
    print(f"Receiver listening on http://{HOST}:{PORT}  (events -> {EVENTS_FILE}, lines -> {LINES_FILE})", flush=True)
    print(f"Player page: http://{HOST}:{PORT}/player", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
