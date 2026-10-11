// ==UserScript==
// @name         Pandora Now Playing Logger
// @namespace    local.pandora.mirror
// @version      0.11
// @description  Logs Pandora track changes and sends them to a local receiver, and carries out commands (skip, thumbs, pause/play, switch station) coming back from it. Ctrl+Shift+L downloads the log.
// @match        https://www.pandora.com/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// @noframes
// ==/UserScript==

// v0.11: a station Pandora only lists under "See all" is switched in steps, and each step is checked against what is really playing:
//   1. the station's link is on the page, or found by opening "See all" and scrolling its list -> click it
//   2. the link (or the station's address, opened inside the page) only shows the station's own page, so press the Play control found there
//   3. last resort: load the station's address afresh and press Play there
// The command result lists what each step saw. If no Play control is found, the page's clickable things are saved to the log (Ctrl+Shift+L).
// Ctrl+Shift+D downloads the same list for whatever page is open (open a station's page first, then press it).

(function () {
  'use strict';

  const POLL_MS = 250;
  const GONE_MS = 1500;                       // how long "no track on screen" must last before it counts
  const STORE_KEY = 'pandoraLoggerLog_v3';
  const MAX_ENTRIES = 2000;
  const RECEIVER = 'http://127.0.0.1:8765/';  // pandora_receiver_v14.py

  const W = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;

  try {
    W.__pandoraLog = JSON.parse(localStorage.getItem(STORE_KEY) || '[]');
  } catch (e) {
    W.__pandoraLog = [];
  }

  const q = (sel) => document.querySelector(sel);
  const text = (sel) => {
    const el = q(sel);
    return el ? el.textContent.trim() : null;
  };
  const toSec = (t) => {
    if (!t || !/^\d+:\d{2}(:\d{2})?$/.test(t)) return null;
    return t.split(':').map(Number).reduce((a, b) => a * 60 + b, 0);
  };
  // Pandora's scrolling title can render its text three times in a row. Collapse an exact triple.
  const dedupe = (s) => s.trim().replace(/^(.{3,}?)\1{2}$/, '$1');
  const trackIdFrom = (href) => {
    const last = (href || '').split('/').pop();
    return /^TR[A-Za-z0-9]+$/.test(last) ? last : null;
  };
  const hostOf = (u) => {
    try { return new URL(u).hostname; } catch (e) { return null; }
  };

  // ---------- readers ----------
  function readMediaSession() {
    const m = navigator.mediaSession && navigator.mediaSession.metadata;
    if (!m || !m.title) return null;
    return { title: m.title, artist: m.artist, album: m.album };
  }

  // The REAL play state: is one of Pandora's audio/video elements actually playing, and where is it?
  // (The play/pause button flickers while Pandora switches songs; the element's paused flag does not.)
  function audioState() {
    const els = [...document.querySelectorAll('audio[id^="mediaelement_"], video')];
    if (!els.length) return null;
    const playing = els.filter((el) => !el.paused);
    const el = playing[0] || els.slice().sort((a, b) => b.currentTime - a.currentTime)[0];
    return { playing: playing.length > 0, posMs: Math.round(el.currentTime * 1000) };
  }

  // the now-playing area (only exists while the Now Playing page is open)
  function readDom() {
    const el = q('[data-qa="playing_track_title"]');
    const title = el ? dedupe(el.textContent) : '';
    if (!title) return null;
    return {
      title,
      artist: text('[data-qa="playing_artist_name"]'),
      album: text('[data-qa="playing_album_name"]'),
      pandoraTrackId: trackIdFrom(el.getAttribute('href')),
    };
  }

  // the bottom player bar (present on every page; during ads it says "Advertisement")
  function readBar() {
    const el = q('[data-qa="mini_track_title"]');
    const title = el ? dedupe(el.textContent) : '';
    if (!title) return null;
    return {
      title,
      artist: text('[data-qa="mini_track_artist_name"]'),
      album: null,
      pandoraTrackId: trackIdFrom(el.getAttribute('href')),
    };
  }

  const currentTrackId = () => {
    const d = readDom();
    const b = readBar();
    return (d && d.pandoraTrackId) || (b && b.pandoraTrackId) || null;
  };

  // Hostnames only (never full URLs, which carry tokens). Only elements that are playing right now.
  function playingMedia() {
    return [...document.querySelectorAll('audio[id^="mediaelement_"], video')]
      .filter((el) => !el.paused)
      .map((el) => ({ id: el.id || el.tagName.toLowerCase(), host: hostOf(el.currentSrc || el.src), sec: Math.round(el.currentTime) }));
  }

  function context() {
    const modeEl = q('.DiscoveryTunerListItem__option--active [data-qa="discovery_tuner_mode_button_name"]');
    const elapsed = toSec(text('[data-qa="elapsed_time"]'));
    const remaining = toSec(text('[data-qa="remaining_time"]'));
    const up = q('[data-qa="thumbs_up_button"]');
    const playEl = q('[data-qa="play_button"], [data-qa="pause_button"]');
    return {
      station: text('.NowPlayingTopInfoSessionName__link'),
      mode: modeEl && modeEl.firstChild ? modeEl.firstChild.textContent.trim() : null,
      elapsedSec: elapsed,
      remainingSec: remaining,
      // remaining_time does not count down, so elapsed+remaining is only trustworthy right after a track starts
      durationSec: elapsed != null && remaining != null && elapsed <= 5 ? elapsed + remaining : null,
      thumbedUp: up ? up.getAttribute('aria-checked') === 'true' : null,
      // data-qa tells the state: pause_button = playing, play_button = paused or just switching (the aria-label is always "Play")
      playButton: playEl ? playEl.getAttribute('data-qa') : null,
      pageTitle: document.title,
      playingMedia: playingMedia(),
    };
  }

  // The station you are on, and the station links Pandora currently shows on the page (it only lists a handful).
  function stationName(el) {
    return ((el.innerText || el.textContent || '').split('\n').map((x) => x.trim()).filter(Boolean)[0]) || '';
  }
  function stationLinks() {
    const out = [];
    const seen = new Set();
    document.querySelectorAll('a[href*="/station/"]').forEach((a) => {
      const name = stationName(a);
      const href = a.getAttribute('href');
      if (name && href && !seen.has(href)) { seen.add(href); out.push({ name, href }); }
    });
    return out;
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  async function waitFor(fn, ms, step) {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      if (fn()) return true;
      await sleep(step || 150);
    }
    return !!fn();
  }
  // The "See all" next to Pandora's station list: prefer one whose surroundings contain station links.
  function findSeeAll() {
    const cands = [...document.querySelectorAll('a, button, [role="button"]')].filter((e) => /^\s*see all\s*$/i.test(e.innerText || e.textContent || ''));
    for (const c of cands) {
      let p = c.parentElement;
      for (let i = 0; p && i < 6; i++, p = p.parentElement) if (p.querySelector('a[href*="/station/"]')) return c;
    }
    return cands[0] || null;
  }
  // Scroll everything that holds station links one screen down (long lists are only drawn as you scroll). True if anything moved.
  function scrollOnce() {
    let moved = false;
    const boxes = new Set();
    document.querySelectorAll('a[href*="/station/"]').forEach((a) => {
      for (let p = a.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
        if (p.scrollHeight > p.clientHeight + 10 && /auto|scroll|overlay/.test(getComputedStyle(p).overflowY)) boxes.add(p);
      }
    });
    boxes.forEach((p) => {
      const was = p.scrollTop;
      p.scrollTop = was + Math.max(100, p.clientHeight * 0.9);
      if (p.scrollTop !== was) moved = true;
    });
    const y = window.scrollY;
    window.scrollBy(0, Math.max(100, window.innerHeight * 0.9));
    if (window.scrollY !== y) moved = true;
    return moved;
  }
  // Click "See all", wait for the list, scroll it to the end, and collect every station link seen on the way.
  // `until` (optional): stop as soon as it returns true (the link we want is on screen); the page is then left as it is.
  // Otherwise the page is put back afterwards when `restore` is set (back button if the route changed, else Escape).
  async function openAllStations(restore, until) {
    const btn = findSeeAll();
    if (!btn) return { found: false, list: [], note: 'no "See all" control found on the page' };
    const before = location.pathname + location.search;
    btn.click();
    const acc = new Map();
    const collect = () => stationLinks().forEach((s) => acc.set(s.href, s));
    await waitFor(() => { collect(); return until ? until() : acc.size > 0; }, 4000);
    let steps = 0;
    while (!(until && until()) && steps < 12) {
      const moved = scrollOnce();
      await sleep(350);
      collect();
      steps++;
      if (!moved) break;
    }
    const satisfied = !!(until && until());
    const after = location.pathname + location.search;
    const note = 'opened "See all" (route ' + before + ' -> ' + after + '), scrolled ' + steps + 'x, saw ' + acc.size + ' station links' + (satisfied ? ', wanted one is on screen' : '');
    if (restore && !satisfied) {
      if (after !== before) history.back();
      else document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true }));
    }
    return { found: true, list: [...acc.values()], note, changedRoute: after !== before, satisfied };
  }
  function currentStationHref() {
    const a = q('.NowPlayingTopInfoSessionName__link');
    return a && a.getAttribute ? a.getAttribute('href') : null;
  }

  // ---------- switching station ----------
  // Clicking a station's link (or going to its address) only opens that station's own page. The station starts when
  // that page's Play control is pressed, so that is the last step; a station counts as switched only when something new
  // is really playing: Pandora's station label names it, or a different track started.
  const idOf = (href) => {
    const m = /(\d{6,})\s*$/.exec(String(href || '').split('?')[0]);
    return m ? m[1] : null;
  };
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const inFixed = (el) => {
    for (let p = el; p && p !== document.documentElement; p = p.parentElement) {
      const pos = getComputedStyle(p).position;
      if (pos === 'fixed' || pos === 'sticky') return true;
    }
    return false;
  };
  const labelOf = (el) => ((el.getAttribute('aria-label') || '') + ' ' + (el.innerText || el.textContent || '') + ' ' + (el.getAttribute('title') || '')).replace(/\s+/g, ' ').trim();
  // a click the way a mouse makes it (for controls that ignore a bare .click())
  function mouseClick(el) {
    try { el.scrollIntoView({ block: 'center' }); } catch (e) { /* ignore */ }
    ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach((t) => {
      el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window, button: 0 }));
    });
  }
  // The Play control on a station's own page. The player bar at the bottom is fixed on screen and only resumes the CURRENT
  // station, so anything inside a fixed/sticky container is not considered.
  function findPlayControl(name) {
    const want = String(name || '').toLowerCase().replace(/\s+radio$/, '');
    const cands = [...document.querySelectorAll('button, [role="button"], a')]
      .filter((e) => visible(e) && !inFixed(e))
      .filter((e) => /\b(play|listen)\b/i.test(labelOf(e)) && !/pause|thumb|skip|replay|shuffle/i.test(labelOf(e)));
    return cands.find((e) => labelOf(e).toLowerCase().indexOf(want) >= 0)
      || cands.find((e) => /^\s*(play|listen)\s*$/i.test(labelOf(e)))
      || cands[0] || null;
  }
  // A compact picture of what can be clicked on the page right now (for working out what Pandora's pages look like).
  function domDigest() {
    return [...document.querySelectorAll('button, [role="button"], a, [data-qa]')].filter(visible).slice(0, 400).map((e) => {
      const o = { tag: e.tagName.toLowerCase() };
      const qa = e.getAttribute('data-qa'); if (qa) o.qa = qa;
      const al = e.getAttribute('aria-label'); if (al) o.label = al.slice(0, 60);
      const tx = (e.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 40); if (tx) o.text = tx;
      const hr = e.getAttribute('href'); if (hr) o.href = hr.slice(0, 70);
      if (inFixed(e)) o.fixed = true;
      return o;
    });
  }
  const startedPlaying = (id, before) => {
    const h = currentStationHref();
    if (id && h && h.indexOf(id) >= 0) return true;
    const t = currentTrackId();
    return !!t && t !== before.track;
  };
  // On a station's page: find its Play control, press it, and wait for the station to start.
  async function playFromStationPage(name, id, before, trace) {
    let btn = null;
    await waitFor(() => (btn = findPlayControl(name)), 6000);
    if (!btn) {
      const near = domDigest().filter((o) => /play|listen/i.test((o.label || '') + ' ' + (o.text || '') + ' ' + (o.qa || ''))).slice(0, 8);
      trace.push('no Play control found on the station page; Play-like things seen: ' + JSON.stringify(near));
      emit('domDigest', { why: 'no Play control for ' + name, digest: domDigest() });
      return false;
    }
    trace.push('Play control "' + labelOf(btn).slice(0, 40) + '"');
    btn.click();
    if (await waitFor(() => startedPlaying(id, before), 7000)) return true;
    const again = findPlayControl(name) || btn;
    mouseClick(again);
    if (await waitFor(() => startedPlaying(id, before), 5000)) return true;
    trace.push('pressing Play did not start the station');
    return false;
  }
  const PENDING = 'pandoraMirrorPendingStation';
  async function switchStation(name, hrefHint) {
    const want = String(name || '').trim().toLowerCase();
    const id = idOf(hrefHint);
    const trace = [];
    const before = { track: currentTrackId() };
    const ok = (how) => ({ ok: true, reason: how + '; ' + trace.join('; ') });
    const onStationPage = () => !!id && location.pathname.indexOf(id) >= 0;
    const findLink = () => [...document.querySelectorAll('a[href*="/station/"]')]
      .find((a) => (id && (a.getAttribute('href') || '').indexOf(id) >= 0) || stationName(a).toLowerCase() === want);
    let link = findLink();
    if (!link) {
      trace.push('not among the stations on the page');
      const r = await openAllStations(false, () => !!findLink());
      trace.push(r.note);
      link = findLink();
      if (!link) trace.push('link still not found after See all');
    }
    if (link) {
      trace.push('link ' + link.getAttribute('href'));
      link.click();
      if (await waitFor(() => startedPlaying(id, before), 3500)) return ok('clicking the link started the station');
      if (!onStationPage()) {
        const l2 = findLink();
        if (l2) mouseClick(l2);
        await waitFor(onStationPage, 2500);
        if (startedPlaying(id, before)) return ok('a mouse-style click started the station');
      }
      if (onStationPage()) {
        trace.push('link opened the station page');
        if (await playFromStationPage(name, id, before, trace)) return ok('pressed Play on the station page');
      } else {
        trace.push('link did not open the station page');
      }
    }
    if (hrefHint) {
      if (!onStationPage()) {
        try {
          history.pushState({}, '', hrefHint);
          window.dispatchEvent(new PopStateEvent('popstate', { state: {} }));
          await sleep(1200);
          trace.push('went to ' + hrefHint + ' inside the page');
        } catch (e) {
          trace.push('address change failed: ' + e);
        }
      }
      if (onStationPage() && await playFromStationPage(name, id, before, trace)) return ok('pressed Play on the station page');
      // last resort: load the station's page afresh and press Play there (the page restarts, so the result is sent after the reload)
      try { sessionStorage.setItem(PENDING, JSON.stringify({ name, href: hrefHint, t: Date.now(), trace })); } catch (e) { /* ignore */ }
      trace.push('reloading the page at ' + hrefHint);
      setTimeout(() => { location.assign(hrefHint); }, 300);
      return new Promise(() => {});          // never settles: the page is about to go away
    }
    return { ok: false, reason: 'station "' + name + '" could not be switched and no address is known for it (' + trace.join('; ') + ')' };
  }
  // after the last-resort reload: press Play on the station page and report
  async function resumePending() {
    let p = null;
    try { p = JSON.parse(sessionStorage.getItem(PENDING) || 'null'); sessionStorage.removeItem(PENDING); } catch (e) { /* ignore */ }
    if (!p || Date.now() - p.t > 60000) return;
    const id = idOf(p.href);
    const trace = (p.trace || []).concat(['page reloaded']);
    await sleep(2500);
    const before = { track: currentTrackId() };
    const good = location.pathname.indexOf(id) >= 0 && await playFromStationPage(p.name, id, before, trace);
    const res = good ? { ok: true, reason: 'pressed Play after reloading; ' + trace.join('; ') }
      : { ok: false, reason: 'station "' + p.name + '" did not start after reloading (' + trace.join('; ') + ')' };
    emit('command', { cmd: 'station', arg: p.name }, res);
    send('cmdResult', Object.assign({ cmd: 'station', arg: p.name }, res));
  }

  function similarArtists() {
    return [...document.querySelectorAll('.ContentSection--similarArtists .RowItemCenterColumn__mainText')]
      .map((e) => e.textContent.trim());
  }

  // Ad: Pandora's placeholder text ("Advertisement" in the bar, "Your station will be right back." on the page)
  // or a title with no track link.
  const looksLikeAd = (info) => /advertis|right back/i.test((info.title || '') + ' ' + (info.artist || '')) || info.pandoraTrackId === null;

  // ---------- sending to the local receiver ----------
  let receiverWarned = false;
  function send(event, payload) {
    const body = JSON.stringify(Object.assign({ event, sentMs: Date.now() }, payload));
    const fail = () => {
      if (!receiverWarned) {
        receiverWarned = true;
        console.log('[pandora-logger] receiver not reachable at', RECEIVER, '(is the receiver running?)');
      }
    };
    try {
      GM_xmlhttpRequest({
        method: 'POST',
        url: RECEIVER,
        data: body,
        headers: { 'Content-Type': 'application/json' },
        timeout: 2000,
        onload: () => { receiverWarned = false; },
        onerror: fail,
        ontimeout: fail,
      });
    } catch (e) { fail(); }
  }

  let lastSentId = null;
  let inAd = false;
  function onTrackChange(info) {
    if (looksLikeAd(info)) {
      if (!inAd) {
        inAd = true;
        lastSentId = null;
        send('ad', { title: info.title });
      }
      return;
    }
    inAd = false;
    if (info.pandoraTrackId && info.pandoraTrackId !== lastSentId) {
      lastSentId = info.pandoraTrackId;
      const c = context();
      send('track', {
        pandoraTrackId: info.pandoraTrackId,
        title: info.title,
        artist: info.artist,
        album: info.album,
        mode: c.mode,
        thumbedUp: c.thumbedUp,
        station: c.station,
        stationHref: currentStationHref(),
      });
    }
  }

  // ---------- logging ----------
  function emit(src, info, extra) {
    const entry = Object.assign({ t: new Date().toISOString(), src }, info, context(), extra || {});
    W.__pandoraLog.push(entry);
    if (W.__pandoraLog.length > MAX_ENTRIES) W.__pandoraLog.splice(0, W.__pandoraLog.length - MAX_ENTRIES);
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(W.__pandoraLog));
    } catch (e) { /* storage unavailable: in-memory log still works */ }
    console.log('[pandora-logger]', src, entry);
  }

  // Related artists and duration are readable a few seconds after a track starts. Only use them if that
  // track is still current, otherwise they belong to a different track.
  const relatedDone = new Set();
  function tryRelated(trackId, attempt) {
    setTimeout(() => {
      if (currentTrackId() !== trackId) return;
      const similar = similarArtists();
      if (similar.length || attempt === 1) {
        emit('related', { pandoraTrackId: trackId, similar });
        const d = readDom();
        send('details', {
          pandoraTrackId: trackId,
          album: d ? d.album : null,
          similar,
          durationSec: context().durationSec,
          station: context().station,
          stationHref: currentStationHref(),
          stations: stationLinks(),
        });
      } else {
        tryRelated(trackId, 1);
      }
    }, attempt === 0 ? 3000 : 5000);
  }
  function scheduleRelated(trackId) {
    if (relatedDone.has(trackId)) return;
    relatedDone.add(trackId);
    tryRelated(trackId, 0);
  }

  // One tracker per source. A track counts as changed only when its key changes; a brief
  // "nothing on screen" flicker (page re-rendering) is ignored.
  function makeTracker(src, read, reportGone) {
    let current = null;
    let lastTrack = null;
    let missingSince = null;
    return () => {
      const info = read();
      if (info) {
        missingSince = null;
        const key = info.pandoraTrackId || (info.title + '|' + info.artist);
        if (key !== current) {
          emit(src, info, { adGuess: looksLikeAd(info), sameTrackAsBefore: key === lastTrack });
          current = key;
          lastTrack = key;
          if (src !== 'mediaSession') onTrackChange(info);
          if (info.pandoraTrackId) scheduleRelated(info.pandoraTrackId);
        }
      } else {
        if (missingSince === null) missingSince = Date.now();
        if (current !== null && Date.now() - missingSince > GONE_MS) {
          if (reportGone) emit(src, { title: null }, { gone: true }); // not necessarily an ad: could be navigation or a re-render
          current = null;
        }
      }
    };
  }


  // ---------- commands coming back from the receiver ----------
  const click = (sel, what) => {
    const el = q(sel);
    if (!el) return { ok: false, reason: what + ' button not found (the Now Playing page may need to be open)' };
    el.click();
    return { ok: true };
  };
  function runCommand(c) {
    switch (c.cmd) {
      case 'skip':
        return click('[data-qa="skip_button"]', 'skip');
      case 'thumbs_up': {
        const el = q('[data-qa="thumbs_up_button"]');
        if (el && el.getAttribute('aria-checked') === 'true') return { ok: true, reason: 'already thumbed up' };
        return click('[data-qa="thumbs_up_button"]', 'thumbs up');
      }
      case 'thumbs_down': {
        const el = q('[data-qa="thumbs_down_button"]');
        if (el && el.getAttribute('aria-checked') === 'true') return { ok: true, reason: 'already thumbed down' };
        return click('[data-qa="thumbs_down_button"]', 'thumbs down');
      }
      case 'pause':
        if (q('[data-qa="pause_button"]')) return click('[data-qa="pause_button"]', 'pause');
        return q('[data-qa="play_button"]') ? { ok: true, reason: 'already paused' } : { ok: false, reason: 'play/pause button not found' };
      case 'play':
        if (q('[data-qa="play_button"]')) return click('[data-qa="play_button"]', 'play');
        return q('[data-qa="pause_button"]') ? { ok: true, reason: 'already playing' } : { ok: false, reason: 'play/pause button not found' };
      case 'station': {
        // The receiver sends "<station name>@@<station address>" (the address from stations_seen.json); an older receiver sends just the name.
        const raw = String(c.arg || '');
        const at = raw.lastIndexOf('@@');
        const name = (at >= 0 ? raw.slice(0, at) : raw).trim();
        const href = at >= 0 ? raw.slice(at + 2).trim() : '';
        if (!name) return { ok: false, reason: 'no station name given' };
        return switchStation(name, href || null);
      }
      case 'stations_all':
        return openAllStations(true).then((r) => {
          if (!r.found) return { ok: false, reason: r.note };
          send('stations', { stations: r.list });
          return { ok: true, reason: r.note };
        });
      default:
        return { ok: false, reason: 'unknown command ' + c.cmd };
    }
  }
  function onCommand(c) {
    const done = (res) => {
      emit('command', { cmd: c.cmd, arg: c.arg }, res);
      send('cmdResult', Object.assign({ cmd: c.cmd, arg: c.arg }, res));
    };
    if (Date.now() - (c.issuedMs || 0) > 8000) return done({ ok: false, reason: 'stale (older than 8 s), ignored' });
    let res;
    try { res = runCommand(c); } catch (e) { return done({ ok: false, reason: String(e) }); }
    Promise.resolve(res).then(done, (e) => done({ ok: false, reason: String(e) }));   // some commands (station, stations_all) take a moment
  }
  function pollCommands() {
    const again = (ms) => setTimeout(pollCommands, ms);
    try {
      GM_xmlhttpRequest({
        method: 'GET',
        url: RECEIVER + 'cmd',
        timeout: 30000,
        onload: (r) => {
          if (r.status === 200) {
            try { onCommand(JSON.parse(r.responseText)); } catch (e) { /* bad command, ignore */ }
            again(50);
          } else if (r.status === 204) {
            again(100);
          } else {
            again(3000);   // receiver is an older version or something is off
          }
        },
        onerror: () => again(3000),
        ontimeout: () => again(500),
      });
    } catch (e) { again(3000); }
  }

  // Tell the receiver when Pandora itself is paused or resumed. The state comes from the audio element
  // (the button is only the fallback) and must hold for PLAYSTATE_STABLE_MS before it counts.
  const PLAYSTATE_STABLE_MS = 400;
  let lastPlayState = null;
  let pendingState = null;
  let pendingSince = 0;
  function playStateTick() {
    const a = audioState();
    const s = a ? a.playing : (q('[data-qa="pause_button"]') ? true : q('[data-qa="play_button"]') ? false : null);
    if (s === null) return;
    if (lastPlayState === null) { lastPlayState = s; return; }
    if (s === lastPlayState) { pendingState = null; return; }
    if (pendingState !== s) { pendingState = s; pendingSince = Date.now(); return; }
    if (Date.now() - pendingSince >= PLAYSTATE_STABLE_MS) {
      lastPlayState = s;
      pendingState = null;
      emit('playstate', { playing: s, posMs: a ? a.posMs : null });
      send('playstate', { playing: s, posMs: a ? a.posMs : null });
    }
  }

  // Log when a <video> element appears or disappears (to catch video ads)
  let lastVideos = 0;
  function videoProbe() {
    const n = document.querySelectorAll('video').length;
    if (n !== lastVideos) {
      lastVideos = n;
      emit('videoProbe', { videos: n });
    }
  }

  const trackers = [
    makeTracker('mediaSession', readMediaSession, false),
    makeTracker('dom', readDom, true),
    makeTracker('bar', readBar, false),
  ];
  setInterval(() => {
    trackers.forEach((t) => t());
    videoProbe();
    playStateTick();
  }, POLL_MS);
  pollCommands();
  resumePending();

  // Ctrl+Shift+D downloads a list of the clickable things on the open page
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.shiftKey && e.code === 'KeyD') {
      e.preventDefault();
      const blob = new Blob([JSON.stringify({ url: location.pathname, digest: domDigest() }, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'pandora-page-' + new Date().toISOString().replace(/[:.]/g, '-') + '.json';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    }
  });

  // Ctrl+Shift+L downloads the log as a JSON file
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.shiftKey && e.code === 'KeyL') {
      e.preventDefault();
      const blob = new Blob([JSON.stringify(W.__pandoraLog, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'pandora-log-' + new Date().toISOString().replace(/[:.]/g, '-') + '.json';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      console.log('[pandora-logger] downloaded', W.__pandoraLog.length, 'entries');
    }
  });

  console.log('[pandora-logger] v0.11 running with', W.__pandoraLog.length, 'saved entries. Sending to', RECEIVER, '. Ctrl+Shift+L downloads the log.');
})();
