// ==UserScript==
// @name         Pandora Now Playing Logger
// @namespace    local.pandora.mirror
// @version      0.9
// @description  Logs Pandora track changes and sends them to a local receiver, and carries out commands (skip, thumbs, pause/play) coming back from it. Ctrl+Shift+L downloads the log.
// @match        https://www.pandora.com/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// @noframes
// ==/UserScript==

(function () {
  'use strict';

  const POLL_MS = 250;
  const GONE_MS = 1500;                       // how long "no track on screen" must last before it counts
  const STORE_KEY = 'pandoraLoggerLog_v3';
  const MAX_ENTRIES = 2000;
  const RECEIVER = 'http://127.0.0.1:8765/';  // pandora_receiver.py

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
  // The "See all" next to Pandora's station list: prefer one whose surroundings contain station links.
  function findSeeAll() {
    const cands = [...document.querySelectorAll('a, button, [role="button"]')].filter((e) => /^\s*see all\s*$/i.test(e.innerText || e.textContent || ''));
    for (const c of cands) {
      let p = c.parentElement;
      for (let i = 0; p && i < 6; i++, p = p.parentElement) if (p.querySelector('a[href*="/station/"]')) return c;
    }
    return cands[0] || null;
  }
  // Click "See all", read every station link that shows up, then put the page back (back button if the route changed, else Escape).
  async function openAllStations(restore) {
    const btn = findSeeAll();
    if (!btn) return { found: false, note: 'no "See all" control found on the page' };
    const before = location.pathname + location.search;
    btn.click();
    await sleep(1500);
    const list = stationLinks();
    const after = location.pathname + location.search;
    const note = 'clicked ' + btn.tagName.toLowerCase() + ' "See all"; route ' + before + ' -> ' + after + '; ' + list.length + ' station links';
    if (restore) {
      if (after !== before) history.back();
      else document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true }));
    }
    return { found: true, list, note, changedRoute: after !== before };
  }
  function currentStationHref() {
    const a = q('.NowPlayingTopInfoSessionName__link');
    return a && a.getAttribute ? a.getAttribute('href') : null;
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
        console.log('[pandora-logger] receiver not reachable at', RECEIVER, '(is pandora_receiver.py running?)');
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


  // ---------- commands coming back from the receiver (long poll) ----------
  // The receiver holds each GET /cmd request open for up to ~20 s and answers as soon as it has a command.
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
        // Switch station by clicking its link. If it is not among the few the page shows, open "See all" first.
        const want = String(c.arg || '').trim().toLowerCase();
        if (!want) return { ok: false, reason: 'no station name given' };
        const find = () => [...document.querySelectorAll('a[href*="/station/"]')].find((a) => stationName(a).toLowerCase() === want);
        let link = find();
        if (link) { link.click(); return { ok: true }; }
        return openAllStations(false).then((r) => {
          link = find();
          if (link) { link.click(); return { ok: true, reason: 'found via See all: ' + r.note }; }
          return { ok: false, reason: 'station "' + c.arg + '" not found, even after See all (' + (r.note || '') + ')' };
        });
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

  console.log('[pandora-logger] v0.9 running with', W.__pandoraLog.length, 'saved entries. Sending to', RECEIVER, '. Ctrl+Shift+L downloads the log.');
})();
