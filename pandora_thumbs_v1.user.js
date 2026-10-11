// ==UserScript==
// @name         Pandora Thumbs Reporter
// @namespace    local.pandora.mirror
// @version      1.0
// @description  Tells the local receiver (v12) whether the playing track is thumbed up or down on Pandora, so the player page can show it. Install it next to "Pandora Now Playing Logger"; it does not replace it.
// @match        https://www.pandora.com/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @noframes
// ==/UserScript==

(function () {
  'use strict';

  const RECEIVER = 'http://127.0.0.1:8765/';
  const POLL_MS = 300;
  const BASELINE_MS = 2500;   // right after a track starts the buttons may still be catching up; what they show then is the starting state, not a new thumb

  const q = (sel) => document.querySelector(sel);
  const trackIdFrom = (href) => {
    const last = (href || '').split('/').pop();
    return /^TR[A-Za-z0-9]+$/.test(last) ? last : null;
  };
  // Same idea as the logger: the Now Playing title or the bottom bar title links to the track; ads have no track link.
  function currentTrackId() {
    const a = q('[data-qa="playing_track_title"]');
    const b = q('[data-qa="mini_track_title"]');
    return (a && trackIdFrom(a.getAttribute('href'))) || (b && trackIdFrom(b.getAttribute('href'))) || null;
  }
  const checked = (el) => (el ? el.getAttribute('aria-checked') === 'true' : null);

  function send(payload) {
    try {
      GM_xmlhttpRequest({
        method: 'POST',
        url: RECEIVER,
        data: JSON.stringify(Object.assign({ event: 'thumbstate', sentMs: Date.now() }, payload)),
        headers: { 'Content-Type': 'application/json' },
        timeout: 2000,
        onerror: () => {},
        ontimeout: () => {},
      });
    } catch (e) { /* receiver not running: nothing to do */ }
  }

  let lastTid = null;
  let trackSince = 0;
  let lastKey = '';

  function tick() {
    const tid = currentTrackId();
    if (tid !== lastTid) {
      lastTid = tid;
      trackSince = Date.now();
      lastKey = '';
    }
    if (!tid) return;                       // an ad, or nothing playing: thumbs do nothing there
    const up = checked(q('[data-qa="thumbs_up_button"]'));
    const down = checked(q('[data-qa="thumbs_down_button"]'));
    const baseline = Date.now() - trackSince < BASELINE_MS;
    const key = [tid, up, down, baseline ? 1 : 0].join('|');
    if (key === lastKey) return;            // only speak when something changed
    lastKey = key;
    send({ pandoraTrackId: tid, up, down, baseline });
  }

  setInterval(tick, POLL_MS);
  console.log('[pandora-thumbs] v1.0 running, reporting to', RECEIVER);
})();
