---
name: download-mts-link
description: >-
  Downloads MTS Link / webinar.ru class recordings into one MP4 with audible
  audio and per-speaker audio stems of the same duration. Use when the user
  asks to скачать лекцию, запись занятия, MTS Link, mts-link, mtslinker,
  my.mts-link.ru, record-new, дорожки по людям, stems for Whisper, or a
  lecture without sound.
---

# Download MTS Link lectures with sound

`mtslinker` often yields a silent MP4. Do **not** treat the hex string in the share URL as `--session-id`. Assemble with ffmpeg using **live** camera audio (RMS ≥ 200), not the empty wowza audio-only files.

## Why mtslinker goes silent

1. `--session-id` is the **cookie** `sessionId` after login. The 32-hex tail of the URL is `recordAccessToken`. Putting the URL token in `--session-id` still 403s on participant-only records.
2. `eventLogs` contains many `mediasession.add` wowza MP4s. Long **audio-only** files are usually empty AAC (RMS ≈ 0). Speech sits on **640×360 camera** files (`has_video` + `has_audio`).
3. mtslinker loads those silent files as `audio_clips` and **replaces** camera audio. Result: picture, no voice.

## URL shape

```
https://my.mts-link.ru/j/{org}/{eventId}/record-new/{eventSessionId}/{recordAccessToken}
https://my.mts-link.ru/{org}/{eventId}/record-new/{eventSessionId}/record-file/{recordFileId}
```

## Workflow

1. Open the URL in Playwright. If the page says the recording is only for participants — **log in**, then reopen the URL.
2. Fetch metadata in-page (cookies stay in the browser):

```js
const token = location.pathname.split("/").pop();
const sessionId = "...eventSessionId...";
const u = `https://gw.mts-link.ru/api/eventsessions/${sessionId}/record?withoutCuts=false&recordAccessToken=${token}`;
const j = await fetch(u, { credentials: "include" }).then(r => r.json());
```

Save `name`, `duration`, and unique `eventLogs[].data.url` with `relativeTime` to `clips.json`. Keep speaker fields: join `data.stream.conference.id` to `conference.add` / `conference.update` (`user.nickname`, `user.id`). Old `[[start, url], ...]` still works, but then stems are named by file hash instead of people.

Public share links may return this JSON without login (`recordAccessToken` query is enough). Participant-only links return 403 until login; `isviewable` can show `viewAccess: "participants"`.

3. Install deps if needed: `pip install httpx tqdm imageio-ffmpeg`

4. Run the assembler (wowza MP4s are usually public; use 2 workers + retries):

```bash
python scripts/download_mts_link.py --meta clips.json --out-dir "DEST"
```

Or after a **real** cookie (not the URL hex):

```bash
python scripts/download_mts_link.py "URL" --session-id COOKIE --out-dir "DEST"
```

Already have `lecture.mp4` and `chunks/` and only need Whisper stems:

```bash
python scripts/download_mts_link.py --meta clips.json --out-dir "DEST" --skip-download --reuse-probe --stems-only
```

5. Confirm sound before finishing. Success looks like:

```
AUDIO_CHECK {'ok': True, 'rms': 1120, ...}
STEMS 8 files in DEST/stems
```

`ffprobe` must show an AAC stream **and** RMS at ~4 minutes (or later) > 200. A silent AAC stream still counts as failure.

Smoke-test first on a long lecture: `--max-duration 420` (camera audio often starts after a few minutes).

## Assemble rules

- **Video:** highest-resolution stream covering each time range (usually 1080p screen share, often **no** audio).
- **Audio mix:** mix **all** live mics, including overlapping cameras and sparse speakers. A 2-point RMS check misses people who talk rarely; keep a track if RMS ≥ 200 **or** `volumedetect` max_volume ≥ −25 dB. Ignore files stuck at max ≈ −90 dB. Cap around 16 tracks.
- **Stems:** one 16 kHz mono file per person who actually spoke, **same duration as the video** (silence padded). Group wowza files by `user.id` from `conference.add`. Overlapping files of the same person go on one stem; different people stay separate so Whisper can skip diarization. Default `DEST/stems/*.flac` plus `stems/manifest.json`. `--stems-format wav|m4a` if needed. Without speaker ids, one stem per live file.
- Output: `DEST/lecture.mp4` (1280×720, libx264 veryfast, AAC 160k) and `DEST/stems/`.

## Do not

- Pass `recordAccessToken` as `--session-id` to mtslinker.
- Use moviepy/`mtslinker` compilation for sound-critical downloads.
- Stop after `ffprobe` shows an Audio stream — measure RMS.
- Scrape browser cookie stores unless the user explicitly provides a session cookie.
- Merge overlapping live files from different `user.id` into one stem — that reintroduces diarization.

See [reference.md](reference.md) for API endpoints and the 403/login gate.
