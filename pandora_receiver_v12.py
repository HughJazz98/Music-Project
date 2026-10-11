#!/usr/bin/env python3
"""
pandora_receiver_v12.py = pandora_receiver_v11.py + three service sections on the player page.

    python pandora_receiver_v12.py

This file does not copy v11: it imports it and adds to it, so keep pandora_receiver_v11.py, spotify_player_v11.py and
mirror_library_v11.py in the same folder (the track -> Spotify path, pause/play sync, stations and everything else stay exactly v11).
Needs lastfm_v12.py and spotify_playlists_v12.py next to it. Open http://127.0.0.1:8765/player as before.

New on the player page (they replace the old "You" placeholder rows)
  Spotify Playlists  every watched playlist with a tick if the track is on it; click to add, click a ticked one to remove.
                     Needs a one-time:  python spotify_playlists_v12.py login
  Pandora Thumbs     thumbs up / down for the playing track, shaded when it is already thumbed. The shading comes from
                     Pandora's own page (needs the new userscript pandora_thumbs_v1.user.js) and, for songs thumbed on other
                     stations, from your Thumbs/ folder. Both buttons are off during ads. Thumbs down also skips, as on Pandora.
                     A thumb you give is also appended to thumbs_live.jsonl (your Thumbs/ files are never touched).
  Last.fm            your own scrobble count for the track, Love (needs a one-time:  python lastfm_v12.py auth) and a link to the artist.
"""
import io
import json
import re
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import ThreadingHTTPServer

import lastfm_v12 as lfm
import mirror_library_v11 as lib
import pandora_receiver_v11 as base
import spotify_playlists_v12 as pls
import spotify_player_v11 as sp

LIVE_FILE = "thumbs_live.jsonl"          # thumbs given since your last manual export of the Thumbs folder, one JSON line each

# ---------- thumbs: what Pandora's page says right now, and what has been thumbed before ----------
live = {"tid": None, "up": None, "down": None}      # the thumb buttons on Pandora's page for the current track
_new = {}                                           # title key -> {station: vote} from thumbs_live.jsonl
_recorded = set()                                   # (track id, vote) already written this session
_rec_lock = threading.Lock()
_pending = {"cmd": None, "tid": None, "t": 0.0}     # a thumb asked for from the player page, waiting for Pandora to confirm


def _stn(name):
    """Station names compared loosely: 'Eminem Radio' == 'eminem'."""
    return re.sub(r"\s+radio$", "", (name or "").strip().lower())


def _load_new():
    try:
        with open(LIVE_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("title") and r.get("artist") and r.get("vote") in ("up", "down"):
                    _new.setdefault(lib.key(r["title"], r["artist"]), {})[r.get("station") or "?"] = r["vote"]
    except FileNotFoundError:
        pass


def _track(tid):
    with base.lock:
        return dict(base.tracks.get(tid) or {}) if tid else {}


def _history(title, artist):
    """{station: vote} from the Thumbs folder plus thumbs given since."""
    h = lib.thumb_for(title, artist) or {}
    by = dict(h.get("by_station") or {})
    if not by and h.get("vote"):                    # a loader without per-station votes: use the one vote for every station it names
        by = {s: h["vote"] for s in (h.get("stations") or [])}
    by.update(_new.get(lib.key(title, artist), {}))
    return by


def _record(vote, tid):
    t = _track(tid)
    if not (t.get("title") and t.get("artist")):
        return
    with _rec_lock:
        if (tid, vote) in _recorded:
            return
        _recorded.add((tid, vote))
        station = t.get("station") or base.now_station["name"] or "?"
        rec = {"t": datetime.now().isoformat(timespec="seconds"), "station": station, "title": t["title"], "artist": t["artist"],
               "vote": vote, "pandoraTrackId": tid}
        with open(LIVE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _new.setdefault(lib.key(t["title"], t["artist"]), {})[station] = vote
    print(f"[{datetime.now():%H:%M:%S}] THUMBS {vote}: {t['title']} - {t['artist']} ({station})  -> {LIVE_FILE}", flush=True)


def on_thumbstate(ev):
    tid = ev.get("pandoraTrackId")
    if not tid:
        return
    prev = dict(live)
    live.update(tid=tid, up=ev.get("up"), down=ev.get("down"))
    if ev.get("baseline") or prev["tid"] != tid:    # the state a track starts with is not a new thumb
        return
    for vote, now, was in (("up", ev.get("up"), prev["up"]), ("down", ev.get("down"), prev["down"])):
        if now is True and was is not True:
            _record(vote, tid)


def on_cmd_result(ev):
    cmd = ev.get("cmd")
    if cmd not in ("thumbs_up", "thumbs_down") or not ev.get("ok"):
        return
    if str(ev.get("reason") or "").startswith("already") or time.time() - _pending["t"] > 10 or _pending["cmd"] != cmd:
        return
    _record("up" if cmd == "thumbs_up" else "down", _pending["tid"])    # a thumbs-down skips at once, so do not wait for the page to show it


# ---------- what the page shows ----------
def _is_ad(cur):
    return (not cur) or sp.in_ad()


def playlists_view(cur, ad):
    summ = lib.summary()["playlists"]
    out = {"state": summ["state"], "message": summ["message"], "canWrite": pls.can_write(), "items": [], "match": None, "hasMatch": False}
    t = _track(cur)
    if t and not ad:
        m = sp.matched.get(cur) or {}
        out["match"], out["hasMatch"] = m.get("label"), bool(m.get("uri"))
        out["items"] = pls.membership(m.get("uri"), t.get("title"), t.get("artist"))
    return out


def thumbs_view(cur, ad):
    t = _track(cur)
    out = {"enabled": bool(t) and not ad, "up": False, "down": False, "source": None, "others": [], "station": None}
    if not t:
        return out
    station = t.get("station") or base.now_station["name"]
    out["station"] = station
    by = _history(t.get("title"), t.get("artist"))
    here = next((v for s, v in by.items() if _stn(s) == _stn(station)), None)
    out["others"] = [{"station": s, "vote": v} for s, v in by.items() if _stn(s) != _stn(station)]
    if live["tid"] == cur and (live["up"] is not None or live["down"] is not None):
        out.update(up=bool(live["up"]), down=bool(live["down"]), source="page")
    elif here:
        out.update(up=here == "up", down=here == "down", source="history")
    return out


def lastfm_view(cur, ad):
    t = _track(cur)
    if not t or ad:
        return {"state": "idle"}
    return lfm.public_state(t.get("artist"), t.get("title"))


def services_view():
    cur = base.now_playing["tid"]
    ad = _is_ad(cur)
    return {"tid": cur, "ad": ad, "playlists": playlists_view(cur, ad), "thumbs": thumbs_view(cur, ad), "lastfm": lastfm_view(cur, ad)}


_orig_snapshot = base.snapshot


def snapshot2():
    out = _orig_snapshot()
    out["services"] = services_view()
    return out


_orig_handle = base.handle


def handle2(ev):
    _orig_handle(ev)                                 # v11 first: logs the event and does everything it always did
    try:
        kind = ev.get("event")
        if kind == "thumbstate":
            on_thumbstate(ev)
        elif kind == "cmdResult":
            on_cmd_result(ev)
    except Exception as e:
        print("v12 event error:", e, flush=True)


# ---------- clicks from the player page ----------
def do_thumb(cmd):
    cur = base.now_playing["tid"]
    if _is_ad(cur):
        out = base.snapshot()
        out["refused"] = "Thumbs do nothing during an ad."
        return out
    _pending.update(cmd=cmd, tid=cur, t=time.time())
    base.send_pandora(cmd)
    return base.snapshot()


def do_playlist(body):
    tid = body.get("tid") or base.now_playing["tid"]
    t = _track(tid)
    if not (tid and t and body.get("playlist")):
        return {"ok": False, "message": "No track to add right now."}
    m = sp.matched.get(tid) or {}
    return pls.toggle(body["playlist"], m.get("uri"), t.get("title"), t.get("artist"))


def do_lastfm(body):
    tid = body.get("tid") or base.now_playing["tid"]
    t = _track(tid)
    if not (tid and t):
        return {"ok": False, "message": "No track to love right now."}
    if body.get("action") != "love":
        return {"ok": False, "message": "Unknown Last.fm action."}
    return lfm.love(t.get("artist"), t.get("title"))


class Handler(base.Handler):
    def _reply(self, obj):
        self._send(200, json.dumps(obj).encode())

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/cmd", "/playlist", "/lastfm"):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
            body = body if isinstance(body, dict) else {}
            if path == "/playlist":
                return self._reply(do_playlist(body))
            if path == "/lastfm":
                return self._reply(do_lastfm(body))
            if body.get("cmd") in ("thumbs_up", "thumbs_down"):
                return self._reply(do_thumb(body["cmd"]))
            self.rfile = io.BytesIO(raw)             # every other command goes on to v11 as it came
        super().do_POST()


# ---------- the page ----------
SECTIONS = r"""
 <h2>Spotify Playlists<i id="plsub"></i></h2><div class="svc" id="pl"></div>
 <h2>Pandora Thumbs<i id="thsub"></i></h2><div class="svc" id="th"></div>
 <h2>Last.fm<i id="lfsub"></i></h2><div class="svc" id="lf"></div>
 <div id="toast"></div>
 <div id="you" style="display:none"></div>
""".encode("utf-8")

STYLE = r"""
<style>
 .svc{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
 .svc .line{font-size:.9rem} .svc .sub{color:var(--dim);font-size:.8rem;margin-top:6px} .svc .note{color:#ffcf99;font-size:.82rem;margin-top:8px}
 .plr{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
 button.pl{display:inline-flex;align-items:center;gap:8px;padding:.45rem .95rem;font-size:.9rem;border-radius:99px;background:#26262c}
 button.pl .box{width:16px;height:16px;border-radius:50%;border:1.5px solid var(--dim);display:inline-flex;align-items:center;justify-content:center;font-size:11px;line-height:1;font-weight:700}
 button.pl.in{background:#1e2c4d;color:#cfdcff} button.pl.in .box{background:var(--acc);border-color:var(--acc);color:#0b1020}
 button.pl:disabled,.ic:disabled{opacity:.45;cursor:default}
 .thr{display:flex;gap:12px;align-items:center;flex-wrap:wrap} .thr .txt{min-width:0;flex:1}
 .ic{display:inline-flex;align-items:center;justify-content:center;width:52px;height:52px;border-radius:50%;background:#26262c;padding:0;flex:none}
 .ic svg{width:26px;height:26px} .ic.on{background:var(--acc);color:#0b1020} .ic.down svg{transform:rotate(180deg)} .ic.heart.on{background:#e0507a;color:#fff}
 a.lnk{display:inline-flex;align-items:center;padding:.5rem 1rem;border-radius:.6rem;background:#2a2a30;color:var(--fg);text-decoration:none;font-size:.9rem}
 a.lnk:hover{background:#383840}
 #toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:#2a2a30;color:var(--fg);padding:9px 16px;border-radius:99px;font-size:.88rem;display:none;max-width:90vw;box-shadow:0 4px 18px #0008;z-index:5}
 #toast.err{background:#3a2326;color:#ffb4b4}
</style>
""".encode("utf-8")

SCRIPT = r"""
<script>
(function(){
const $=id=>document.getElementById(id);
const NS='http://www.w3.org/2000/svg';
const THUMB='M1 21h4V9H1v12zm22-11c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 8.45 7 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-2z';
const HEART='M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z';
function icon(path,filled){const s=document.createElementNS(NS,'svg');s.setAttribute('viewBox','0 0 24 24');const p=document.createElementNS(NS,'path');p.setAttribute('d',path);
 if(filled){p.setAttribute('fill','currentColor');}else{p.setAttribute('fill','none');p.setAttribute('stroke','currentColor');p.setAttribute('stroke-width','1.8');p.setAttribute('stroke-linejoin','round');}
 s.append(p);return s;}
function div(cls,text){const d=document.createElement('div');if(cls)d.className=cls;if(text!=null)d.textContent=text;return d;}
let toastTimer=null;
function toast(msg,err){const t=$('toast');t.textContent=msg;t.className=err?'err':'';t.style.display='block';clearTimeout(toastTimer);toastTimer=setTimeout(()=>{t.style.display='none';},err?7000:2500);}
const seen={};
function render(id,model,build){const j=JSON.stringify(model);if(seen[id]===j)return;seen[id]=j;const el=$(id);el.replaceChildren();build(el);}
function post(url,obj){return fetch(url,{method:'POST',body:JSON.stringify(obj)}).then(r=>r.json());}
const busy={}; let flash={tid:null,up:false,down:false,t:0}; let loveBusy=false; let latest=null;

async function togglePl(pid,tid){busy[pid]=true;draw();
 try{const r=await post('/playlist',{playlist:pid,tid:tid});toast(r.message||(r.ok?'Done':'Did not work'),!r.ok);}catch(e){toast('Could not reach the receiver',true);}
 delete busy[pid];poll();}
async function thumb(kind,tid){flash={tid:tid,up:false,down:false,t:Date.now()};flash[kind]=true;draw();
 try{const r=await post('/cmd',{cmd:'thumbs_'+kind});if(r&&r.refused)toast(r.refused,true);}catch(e){toast('Could not reach the receiver',true);}
 poll();}
async function love(tid){loveBusy=true;draw();
 try{const r=await post('/lastfm',{action:'love',tid:tid});toast(r.message||(r.ok?'Loved':'Did not work'),!r.ok);}catch(e){toast('Could not reach the receiver',true);}
 loveBusy=false;poll();}

function drawPlaylists(S){const P=S.playlists;
 render('pl',{P:P,busy:busy,ad:S.ad,tid:S.tid},el=>{
  if(!S.tid||S.ad){el.append(div('line',S.ad?'An ad is playing, so there is nothing to add.':'Waiting for a track...'));return;}
  if(P.state!=='ok'){el.append(div('line',P.message||'Starting...'));return;}
  if(!P.items.length){el.append(div('line','No playlists are being watched yet (mirror_config.json).'));return;}
  const row=div('plr');
  P.items.forEach(it=>{const b=document.createElement('button');b.className='pl'+(it.has?' in':'');
   const box=document.createElement('span');box.className='box';box.textContent=it.has?'✓':'';b.append(box,document.createTextNode(it.name||'(unnamed)'));
   b.disabled=!!busy[it.id]||!P.canWrite||(!it.has&&!P.hasMatch);
   b.title=!P.canWrite?'Spotify has not allowed playlist changes yet (see the note below)':it.has?'On this playlist. Click to remove it.':(P.hasMatch?'Click to add it':'Waiting for the Spotify match');
   b.onclick=()=>togglePl(it.id,S.tid);row.append(b);});
  el.append(row);
  if(P.match)el.append(div('sub','Adds this Spotify version: '+P.match));
  else el.append(div('sub','Waiting for the Spotify match before anything can be added.'));
  if(!P.canWrite)el.append(div('note','Playlist changes are not allowed yet. In a terminal in this folder run once: python spotify_playlists_v12.py login'));
 });
 $('plsub').textContent=(P.state==='ok'&&S.tid&&!S.ad)?P.items.length+' watched':'';}

function sentence(T){
 if(!T.enabled)return null;
 let s=T.up?'Thumbed up':T.down?'Thumbed down':'Not thumbed';
 if(T.source==='page')s+=' (as shown on Pandora)';else if(T.source==='history')s+=' on '+(T.station||'this station')+' (from your Thumbs folder)';
 return s;}
function drawThumbs(S){const T=S.thumbs;const fresh=flash.tid===S.tid&&Date.now()-flash.t<4000;const f={up:fresh&&flash.up,down:fresh&&flash.down};
 render('th',{T:T,f:f,ad:S.ad,tid:S.tid},el=>{
  const row=div('thr');
  ['up','down'].forEach(k=>{const on=k==='up'?(T.up||f.up):(T.down||f.down);
   const b=document.createElement('button');b.className='ic '+k+(on?' on':'');b.append(icon(THUMB,!!on));
   b.disabled=!T.enabled||(k==='up'&&!!(T.up||f.up))||(k==='down'&&!!(T.down||f.down));
   b.title=!T.enabled?'Thumbs are off during ads':k==='up'?(T.up?'Already thumbed up':'Thumbs up'):(T.down?'Already thumbed down':'Thumbs down (also skips the song)');
   b.onclick=()=>thumb(k,S.tid);row.append(b);});
  const txt=div('txt');
  if(!T.enabled)txt.append(div('line',S.ad?'An ad is playing, so thumbs are off.':'Waiting for a track...'));
  else{txt.append(div('line',sentence(T)));
   const ups=T.others.filter(o=>o.vote==='up').map(o=>o.station),downs=T.others.filter(o=>o.vote==='down').map(o=>o.station);
   if(ups.length)txt.append(div('sub','Also thumbed up on: '+ups.join(', ')));
   if(downs.length)txt.append(div('sub','Also thumbed down on: '+downs.join(', ')));
   if(T.source!=='page'&&!T.up&&!T.down)txt.append(div('sub','Pandora\'s own page has not reported in for this track (is the thumbs userscript installed?).'));}
  row.append(txt);el.append(row);
 });
 $('thsub').textContent='';}

function plural(n,w){return n+' '+w+(n===1?'':'s');}
function drawLastfm(S){const L=S.lastfm;
 render('lf',{L:L,tid:S.tid,loveBusy:loveBusy},el=>{
  if(L.state==='idle'){el.append(div('line',S.ad?'An ad is playing.':'Waiting for a track...'));return;}
  if(L.state==='off'){el.append(div('line',L.message));return;}
  const row=div('thr');
  const b=document.createElement('button');b.className='ic heart'+(L.loved?' on':'');b.append(icon(HEART,!!L.loved));
  b.disabled=!!L.loved||!L.canLove||loveBusy||L.state!=='ok';
  b.title=L.loved?'Already one of your loved tracks':!L.canLove?'Not allowed to love yet (see the note below)':'Love this track on Last.fm';
  b.onclick=()=>love(S.tid);row.append(b);
  const txt=div('txt');
  if(L.state==='loading')txt.append(div('line','Asking Last.fm...'));
  else if(L.state==='error')txt.append(div('line',L.message||'Last.fm did not answer.'));
  else{const n=L.userPlays;
   txt.append(div('line',!L.found?'Last.fm does not know this track under this name':n===null?'Your scrobble count is not available':n===0?'Not scrobbled by you yet':'You have scrobbled this '+plural(n,'time')));
   txt.append(div('sub',(L.loved?'One of your loved tracks. ':'')+'The count catches up a little after the song plays.'));}
  row.append(txt);
  if(L.artistUrl){const a=document.createElement('a');a.className='lnk';a.href=L.artistUrl;a.target='_blank';a.rel='noopener';a.textContent='Artist on Last.fm';row.append(a);}
  el.append(row);
  if(!L.canLove)el.append(div('note','To love tracks from here, run once in a terminal in this folder: python lastfm_v12.py auth'));
 });}

function draw(){if(latest&&latest.services){const S=latest.services;drawPlaylists(S);drawThumbs(S);drawLastfm(S);}}
let polling=false;
async function poll(){if(polling)return;polling=true;try{latest=await (await fetch('/state')).json();draw();}catch(e){}polling=false;}
poll();setInterval(poll,1000);
})();
</script>
""".encode("utf-8")


def build_html(html):
    anchor = b'<h2>You</h2><div id="you"></div>'
    if anchor in html:
        html = html.replace(anchor, SECTIONS)
    else:
        print("v12: could not find the 'You' placeholder in the v11 page; the new sections are added at the end instead.", flush=True)
        html = html + SECTIONS
    html = html.replace(b"Open your thumbs page", b"Open recent thumbs on Pandora")
    return html + STYLE + SCRIPT


def main():
    base.PLAYER_HTML = build_html(base.PLAYER_HTML)
    base.snapshot = snapshot2
    base.handle = handle2
    _load_new()
    if not base.SPOTIFY_ON:
        print("Spotify not logged in yet, so only logging. To enable playback run:  python spotify_player_v11.py login", flush=True)
    lib.start(base.SPOTIFY_ON)
    print("Playlist changes from the page:", "allowed" if pls.can_write() else "NOT yet allowed (run: python spotify_playlists_v12.py login)", flush=True)
    print("Last.fm:", "set up" if lfm.configured() else "not set up (username / API key missing)", "|",
          "can love tracks" if lfm.can_love() else "cannot love yet (run: python lastfm_v12.py auth)", flush=True)
    print(f"Receiver v12 listening on http://{base.HOST}:{base.PORT}  (events -> {base.EVENTS_FILE}, thumbs you give -> {LIVE_FILE})", flush=True)
    print(f"Player page: http://{base.HOST}:{base.PORT}/player", flush=True)
    ThreadingHTTPServer((base.HOST, base.PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
