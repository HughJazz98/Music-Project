# Changelog

Legacy files (`pandora_receiver_L.py`, `spotify_playerL.py`) are untouched. New versions are `_v<number>`.

## v11 (2026-10-08) — "See all" stations from the GUI, stale-track fix

Files: `pandora_receiver_v11.py`, `spotify_player_v11.py`, `mirror_library_v11.py` (unchanged apart from the version name in its
import), **`pandora_userscript_v9.user.js`** (header 0.9; replace it in Tampermonkey).

### Fixed: an older track could override a newer one on Spotify
In a log from v10 two track events arrived in the same second (`Pretty Low`, then `Heretics Fork`). Heretics started in 1 s, then
the older `Pretty Low` failed its "is this really playing?" check (Spotify was playing the other track), escalated to
`transfer-then-play` 4 s later and ended up playing, while Pandora was on Heretics. `play_uri` now asks before every attempt, and
after every failed check, whether its track is still the one Pandora is on, and stops if not
(`stopped starting <track>: Pandora already moved on` in the terminal; `"result": "superseded"` in `spotify_log.jsonl`).
Station switching makes quick back-to-back track events more likely, which is why this matters now. Normal playback is unchanged.
This is a reading of the log; the same two events on your machine will confirm it (look for the new "stopped starting" line).

### Added
- **"See all" button on the Stations row of the player page.** It tells the userscript to click Pandora's own "See all" next to
  the station list, read every station link that appears, then put the page back (browser-back if the route changed, else Escape).
  The terminal prints `STATIONS stations_all: clicked ... "See all"; route A -> B; N station links` so a wrong guess is visible.
- **Switching to a station that is not in the short list now works too:** if the link is not on the page, the userscript opens
  "See all" first, finds the station and clicks it. All known stations are clickable (the greyed-out state is gone).
- "Now Playing" (a link on the page that is not a station) is ignored and removed from `stations_seen.json`.
- The userscript can now run commands that take a moment (the command result is sent when it finishes).

### Tested
Against a mock page (a short station list, a "See all" button that reveals more links in a pop-up, Escape closes it): the list,
the pop-up closing and switching to a station that was hidden all worked. Not tried on the real Pandora page.

## v10 (2026-10-08) — stations, accounts panel

Files: `pandora_receiver_v10.py`, `spotify_player_v10.py`, `mirror_library_v10.py`, **`pandora_userscript_v8.user.js`**
(header 0.8; replace the userscript in Tampermonkey). Track path and pause/skip sync unchanged. `spotify_player_v10.py`
is the v9 file renamed (it already asks for the playlist permissions).

### Added
- **Station shown for each track** ("Station <name>", with Pandora's tuner mode such as Crowd Faves). The userscript already read
  the station name for its own log; v8 now also sends it (and its link) with every track event.
- **Station list + switching (needs testing on the real page).** The userscript lists the station links Pandora is showing
  (`a[href*="/station/"]`, only a handful) when each track's details are sent; the receiver prints
  `STATIONS on the Pandora page: ...` and remembers every station it has seen in `stations_seen.json`. Buttons on the player
  page switch to a station by clicking its link on the Pandora page (new `station` command). Only stations Pandora is
  showing right now can be switched to; the others are listed but greyed out. If a switch fails the page shows why (and the
  terminal prints `PANDORA COULD NOT STATION: ...`). For ~6 s after a switch command Pandora's play/pause flicker is ignored.
  These selectors were written without seeing Pandora's page: if the STATIONS line is empty or a switch does nothing, send me
  that line and I will correct the selector.
- **Accounts panel** (Spotify / Pandora / Last.fm), each with a status: Spotify = who is logged in and whether playlist
  permission is there; Pandora = whether the Pandora tab's userscript is reporting in (plus a link to your thumbs page if you
  fill in your username); Last.fm = what you have filled in (username, whether an API key and secret are saved; they are
  never displayed). Fill them in `mirror_config.json`:
  `"pandora": {"username": ""}, "lastfm": {"username": "", "api_key": "", "api_secret": ""}`. Existing config files keep
  working; add those two parts by hand. Keep the file private. Nothing is sent to Last.fm yet.
- Login messages now name the exact file/command to run for the version you have.

## v9 (2026-10-08) — playlists (live), plus placeholders for thumbs history, scrobbles and Last.fm

Files: `pandora_receiver_v9.py`, `spotify_player_v9.py`, **new** `mirror_library_v9.py` (keep all three in the same folder).
Userscript stays at v7. Track path, sync and `/cmd` unchanged.

### Added
- **Playlist awareness that stays current.** Choose playlists in `mirror_config.json` (created on first run):
  `{"playlists": ["Name", "https://open.spotify.com/playlist/<id>"]}` (name, id or link). If it is empty the receiver prints
  your playlist names to pick from. The player page's new "You" section says whether the current track is in a watched
  playlist (and which), and "Recently played" marks tracks that are. A track counts as "in" a playlist if its Spotify id
  matches, or its title + artist match (other versions of the same song count).
- **How it stays live:** once a minute one call to `GET /me/playlists` returns every playlist's snapshot id; a playlist's
  songs are re-read (`GET /playlists/{id}/items`) only when its snapshot id changed. Adds/removes show up within ~1 minute.
  `PLAYLIST_POLL_SEC` in `mirror_library_v9.py` (0 = off). This runs in its own thread, never on the track path.
- **Log in to Spotify again once** (`python spotify_player_v9.py login`): v9 asks for the `playlist-read-private` and
  `playlist-read-collaborative` permissions. Until then the page says so instead of failing.
- `python spotify_player_v9.py playlists` lists your playlists.
- **Placeholders** on the page, each tagged with its source: Pandora thumbs, Scrobbles, Last.fm.
  - Pandora thumbs becomes real as soon as `thumbs_history.json` exists next to the receiver:
    `[{"station": "...", "title": "...", "artist": "...", "vote": "up"|"down"}, ...]`. The current track then shows
    "Thumbs up/down on <stations>" or "No thumb recorded". The file written by the community browser-console script
    (`liked_songs_grouped_by_station.json`, thumbs up only) is also read if present, but that reader was written from the
    script's description and has not been tried on a real file: check the `LIBRARY THUMBS loaded N` line.
  - Scrobbles and Last.fm: text only. Nothing is read, recorded or sent.

### Notes
- Spotify (Feb 2026 development-mode rules): playlist items can only be read for playlists you own or collaborate on, and
  the endpoint is now `/playlists/{id}/items` with each entry's song under `item` (the code accepts `track` too).
  A followed playlist someone else owns will therefore not work.
- `/state` now also returns `library` (status of playlists, thumbs, scrobbles, Last.fm) and per track `inPlaylists` and `thumb`.

## v8 (2026-10-08) — album genre, recommendation source, Pandora-anchored progress

Files: `pandora_receiver_v8.py`, `spotify_player_v8.py`. Userscript stays at v7 (no change). Track path, sync and `/cmd` unchanged.

### Changed
- **Genre is now the album's genre from MusicBrainz** (the release group of the album Pandora reports). Order: album's genre ->
  tags on the recording -> the artist's top genre. The chip says which one it was ("MusicBrainz, album" / "song" / "artist-wide"),
  and the album-genre chip links to that album's MusicBrainz page. If the album lookup itself fails (network), the result is not
  cached, so it is retried next time. Cache file is now `enrich_cache_v4.json` (key includes the album); older caches are left alone.
  Needs 1-2 extra MusicBrainz requests per new track (in the background; MusicBrainz allows about one per second).
- **MusicBrainz credit** in a footer line on the page, plus the per-chip source tags from v7.
- **"Recommendation algorithm used to get this track: Pandora"** on the track card (`REC_SOURCE` in the receiver, sent to the page
  as `recSource`). Filler/other sources can set it per track later.
- Removed the "Progress follows Pandora's clock..." note and the repeated sources from the footer.
- **Progress bar re-anchored to Pandora's real position.** The userscript already sends the audio element's position
  (`posMs`) with every play/pause change; the receiver now sets its progress clock to it (it ignored it before), so after any
  pause/resume the bar matches Pandora exactly. Between those points the receiver's clock runs on its own. Positions seen in the
  4 s window after a track change are not used (Pandora can report the old track's position then).

## v7 (2026-10-08) — player page: sources shown, match score hidden

Files: `pandora_receiver_v7.py`, `spotify_player_v7.py`. Userscript stays at v7. Track path, sync and `/cmd` unchanged.

### Changed on the page
- Match score removed from the page (it is still in the terminal `SPOT PLAY` line and `spotify_log.jsonl`; `/state` no longer sends it).
- Every piece of metadata now says where it comes from: Released and Genre -> "MusicBrainz"; Similar artists -> "from Pandora";
  cover -> "Cover: Spotify"; a note under the progress bar says it follows Pandora's clock.
- Genre says "MusicBrainz, artist-wide" when it is the artist's overall top genre rather than a tag on the recording itself.
- The Spotify line now reads "Playback on Spotify: <matched track>" with an "Open in Spotify" link.

### Changed in the code
- The MusicBrainz lookup records `genreFrom` ("recording" or "artist"). The cache file is now `enrich_cache_v3.json`, so old
  lookups are redone (once per track, in the background, about 1-2 s each, MusicBrainz's rate limit). The old v2 cache is left alone.

## v6 (2026-10-08) — player page shows the track metadata

Files: `pandora_receiver_v6.py`, `spotify_player_v6.py`. Userscript stays at v7. Confirmed working in v5 (your logs): ~1 s
Pandora->Spotify, no unwanted pauses, cross-platform pause and skip. v6 changes none of that: the track path, the
sync/pause logic and `/cmd` are as in v5.

### Added (all on the player page, `http://127.0.0.1:8765/player`)
- Album cover (Spotify's image for the matched track), title, artist, album.
- Release date and genre (the MusicBrainz lookup already in the receiver), as chips.
- The Spotify match: which Spotify track was chosen, its match score, and an "open" link to it. A low score or odd label
  is the quickest way to spot a wrong match.
- Similar artists from Pandora (they appear about 3 s into each track, when the userscript sends them).
- Progress bar with elapsed / total time. Total is Pandora's track length. Elapsed is the receiver's own clock: it restarts
  at each track, stops on pause and runs on resume, so it can be off by a second or so (it is not read from either player).
- "Recently played" list (last 11 tracks before the current one) with cover, title, artist, album, year, genre.
- Layout works at phone width. Page refreshes every second.
- Play/Pause and Skip as before. Thumbs buttons are NOT on the page yet, on purpose (a real thumbs-down is permanent on your
  Pandora account); the receiver already accepts `thumbs_up` / `thumbs_down` commands for when you want them.

### Changed in the code
- `/state` now returns `{playing, ad, spotify, elapsed, track:{...}, history:[...]}` instead of flat title/artist/album.
  The track object has title, artist, album, release, genre, similar, durationSec, match, score, art, url.
- `spotify_player_v6.py` remembers the cover and match per track (`matched`) and stores `art` in `spotify_cache.json`.
  Tracks cached by an older version get their cover fetched once (`GET /tracks/{id}`, after playback has started, in its
  own thread), so it costs one extra Spotify call per old track the first time it is played again, and none on the track path.

### Not done
- Pandora's own cover art isn't used (the userscript doesn't send it); Spotify's is shown instead.
- Elapsed time isn't re-synced from the players after a pause/resume.

## v5 (2026-10-08) — fix: the multi-second delay was the receiver holding `GET /cmd` open

Files: `pandora_receiver_v5.py`, `spotify_player_v5.py` (identical to v4 apart from names/headers). Userscript stays at
v7 (header 0.7); no change needed.

### Problem
v4 still showed `page to receiver: 19377 ms, 19129 ms, 117 ms, 2465 ms, 10407 ms` in the terminal log. That number is the
time between the userscript sending the track event and the receiver getting it, so the lag was before Spotify was involved.
(My earlier guess that it was Spotify-side, e.g. rate limiting, was wrong.)

### Cause
From v1 on, the receiver answered the userscript's `GET /cmd` poll by holding it open for up to 20 s while waiting for a
command. Tampermonkey's `GM_xmlhttpRequest` calls go through one after another, so a track-event POST queued behind that
held-open request waited until it ended (0-20 s). The legacy receiver never had this because it didn't have `/cmd`.

### Changed
- `GET /cmd` now answers immediately: 204 when idle, 200 `{cmd, issuedMs}` when a command is waiting. The userscript
  re-polls every 100 ms (50 ms after a command), as it was already written to do.
- A command is taken off the queue before it is sent (so an immediate re-poll can't receive it twice) and put back only
  if the write fails.
- Emulating Tampermonkey's one-request-at-a-time behaviour against the receivers: track event to receiver took
  3.7-14.8 s with v4 and 46-103 ms with v5.

### What to check
Run the v5 receiver with the same userscript. `page to receiver` should be roughly 100-250 ms, and the total
`ms after Pandora's page flipped` should be close to 1 s again.

## v4 (2026-10-08) — restart from the legacy receiver: pause/play only, no added delay

Files: `pandora_receiver_v4.py`, `spotify_player_v4.py`, `pandora_userscript_v7.user.js` (header version 0.7).
v1-v3 are superseded by this version; they are left on disk for reference only.

### Why
v1-v3 added a lot around the legacy receiver and the delay between Pandora's track change and Spotify playing grew to ~6 s.
v4 is built from `pandora_receiver_L.py` / `spotify_playerL.py` again, with the smallest possible addition.

### What is the same as legacy
- The whole track path: event -> search -> start on Spotify -> verify. `play_for_track` is identical except one added line
  (`_ad["on"] = False`). The receiver's track handler only does its extra bookkeeping AFTER the Spotify thread has started.
- Timing check against a fake Spotify: track event -> Spotify play command took 4-70 ms for both the legacy receiver and v4,
  with and without userscript traffic.
- No background polling of Spotify, no heartbeat from the userscript, no extra Spotify calls. Spotify is only called for a
  pause or resume you actually asked for.

### Added
- Pause/play on Pandora's page -> Spotify pauses/resumes (about 0.5 s later; userscript v7 reads Pandora's audio element
  and waits only 0.4 s for the state to hold).
- Player page (`/player`): Play/Pause toggles Spotify first, then Pandora; Skip goes to Pandora.
- Nothing on Spotify's side ever pauses or resumes Pandora.
- A Pandora play/pause seen within 4 s of a track change is ignored and rechecked once afterward, so Pandora's flicker
  between songs cannot pause Spotify.
- Every pause/play decision prints as `SYNC ...` and is saved to `sync_decisions.jsonl`; every command sent to Pandora is listed.
- New warnings in `spotify_player_v4.py`: `SPOT WARNING slow Spotify call ... took N ms` for any call over 1 s, and
  `SPOT WARNING Spotify is rate limiting us (429) ... Retry-After=N s` when Spotify rate-limits a call
  (the legacy code silently waits up to 5 s on a 429 and retries, which would look like a multi-second delay).

### Removed (on purpose, until the delay is confirmed fixed)
- The Spotify-side watcher, the position heartbeat and the post-resume ALIGN measurement from v2/v3.

## v3 (2026-10-08) — Pandora never paused automatically; real play state; sync measured after pause/play

Files: `pandora_receiver_v3.py`, `spotify_player_v3.py`, **`pandora_userscript_v6.user.js`** (header version 0.6; replace the
old userscript in Tampermonkey, the receiver works best with all three on v3/v6). v1 and v2 files left as they were.

### Problem (from testing v1 and v2)
Around track changes Pandora got an unwanted pause, which shifted Pandora's timeline against Spotify for that track
(a per-track desync). Pausing/resuming by hand (Pandora's page or the player page) also left the two out of step.

### Changed
- **Nothing on Spotify's side can pause or resume Pandora any more** (`MIRROR_SPOTIFY_PAUSE_TO_PANDORA = False`).
  In v1/v2 that was the only automatic way the receiver ever told Pandora to pause, and it relied on Spotify's
  play-state readings, which are unreliable while a track is loading. Pandora is now paused only by the player page's
  buttons or by you on Pandora itself. (Pausing in Spotify's own app is therefore no longer mirrored to Pandora.)
- **Pandora's real play state (userscript v6).** The play/pause button flickers while Pandora switches songs, so v6
  reads the audio element's `paused` flag instead, falling back to the button only if no audio element exists.
  The "must stay changed" wait before reporting dropped from 1.5 s to 0.4 s, so a pause on Pandora reaches
  Spotify in ~0.5-0.7 s instead of ~1.75 s.
- **Position samples.** The userscript sends Pandora's exact playback position once a second and with every play/pause event.
- **Sync is measured after every resume.** At track start the receiver records how far ahead Pandora was when Spotify
  started (`BASELINE`). About 2 s after any resume it compares Pandora's and Spotify's positions against that and
  logs `ALIGN ... drifted +N ms`. By default it only logs. Set `AUTO_REALIGN = True` to have it seek Spotify back in line
  when the drift is over `ALIGN_TOL_MS` (700 ms). Left off by default because Spotify's reported position is coarse;
  check the logged numbers first.
- **Every command to Pandora is logged** in `sync_decisions.jsonl` / the terminal as `-> Pandora command: ...` with how many
  seconds after the last track start it was sent. Pandora play/pause events are logged with their position and timing too.
  If Pandora pauses and there is NO such line, the pause did not come from the receiver.
- Position samples are not written to `events.jsonl` (they would add a line per second).

### Unchanged
- Track-change path (event -> Spotify search/play) and the track-change guard from v2.

## v2 (2026-10-08) — fix: pause/play misfiring around track changes

Files: `pandora_receiver_v2.py`, `spotify_player_v2.py` (v1 files left as they were). Userscript unchanged (v0.5).

### Problem (from testing v1)
On some track changes Pandora got paused and stayed paused while Spotify kept playing the new song.

### Cause
In v1 every play/pause reading was trusted immediately, but right at a track change both sides give false readings:
Spotify's player briefly reports "not playing" while the new track loads, and Pandora's play button flickers
while it switches songs. v1 treated a single reading as the user pausing and passed it on to the other service.

### Changed
- **Track-change guard.** For about 4 s after a new Pandora track (and 3 s after Spotify confirms it started the track),
  play/pause readings from either side are not treated as a user pause/play.
- **Held-back Pandora pauses are rechecked.** A Pandora pause seen during the guard is remembered and only applied
  afterward if Pandora is still paused, so a real pause is delayed, not lost.
- **Spotify-side pauses need two readings in a row, on our own track.** A single "not playing" reading, or a reading
  while Spotify is on a different track than the one we started, is ignored.
- **Decision log.** Every pause/play decision (and every ignored one) prints as `SYNC ...` and is saved to
  `sync_decisions.jsonl`, with the reason. If it misbehaves again, that file shows exactly what triggered it.
- **Lost-command fix.** A command is removed from the queue only after it was actually delivered, so a stale
  long-poll connection (for example after reloading the Pandora tab) can no longer swallow a pause or play.
- Echo-ignore window after our own commands raised from 2.5 s to 3.5 s (Pandora reports state ~1.75 s late).

### Trade-offs
- A pause done in Spotify itself now reaches Pandora after ~3-6 s instead of ~3 s (two readings, `SPOTIFY_POLL_SEC = 3.0`).
- A pause on Pandora's page right at a track change takes a few extra seconds. Pauses from the player page are instant.
- Track-change path (event -> Spotify search/play) is unchanged, so the sync gap should be the same.

## v1 (2026-10-08) — play/pause sync + primitive player page

Files: `pandora_receiver_v1.py`, `spotify_player_v1.py`. Tampermonkey userscript stays at v5 until its half of the
contract below is added.

### Added
- **Two-way pause/play.** A pause or resume on Pandora, on Spotify, or on the player page is mirrored to the other side.
- **Spotify always moves first.** Pause: Spotify pauses, then Pandora is told. Resume: Spotify resumes, then Pandora is told.
  The service the change came from is never told to repeat it.
- **Primitive player page** at `http://127.0.0.1:8765/player`: now playing, Play/Pause toggle, Skip.
- **Spotify-side watcher** (`SPOTIFY_POLL_SEC = 3.0`, set to `0` to turn off) that notices a pause/resume done in Spotify itself.
- `spotify_player_v1.py`: `pause()`, `resume()`, `is_playing()`; `python spotify_player_v1.py pause|resume` from the command line.
- Receiver endpoints: `GET /cmd` (long-poll the existing userscript v0.5 already uses; 204 when idle), `GET /state`, `GET /player`, `POST /cmd`.

### Unchanged on purpose (sync delay)
- The track-change path (event -> Spotify search/queue/play) is byte-for-byte the legacy logic. New code only adds flag
  writes, so the ~1 s Pandora-to-Spotify gap should be the same as `_L`.

### Behaviour notes
- During a Pandora ad Spotify stays paused and the watcher is muted; pressing Play during an ad does not restart the old track.
- Commands the userscript does not pick up within 5 s are dropped (`PANDORA_CMD_TTL`) so a stale pause never fires later.
- The watcher costs about 20 Spotify API calls per minute. Set `SPOTIFY_POLL_SEC = 0` to rely on Pandora-side and player-page controls only.

### Userscript
- Works with the existing userscript v0.5 as-is: it already sends `playstate` events, long-polls `GET /cmd`, and clicks
  Pandora's play/pause/skip/thumbs buttons, and reports back with `cmdResult` (failures are printed by the receiver).
- Known limit: the userscript waits for a Pandora pause/play to be stable for 1.5 s before reporting it, so a pause
  done on Pandora's own page reaches Spotify about 1.5-1.75 s later. Shortening that is a userscript change (v6).
