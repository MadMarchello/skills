# MTS Link recording internals

## Auth

| Thing | What it is | Where |
| --- | --- | --- |
| `recordAccessToken` | Public share token | Last path segment, 32 hex chars |
| `sessionId` | Logged-in cookie | DevTools → Application → Cookies → `sessionId` |
| `eventSessionId` | Numeric session | `/record-new/{this}` |

`GET .../record/isviewable?recordAccessToken=` can be 200 while `/record` is 403.

Participant gate (Playwright): heading «Запись доступна только участникам встречи», button «Войти». After login, `/api/login` and `/record` are 200.

## Metadata

```
GET https://gw.mts-link.ru/api/eventsessions/{eventSessionId}/record?withoutCuts=false&recordAccessToken={token}
GET https://gw.mts-link.ru/api/event-sessions/{eventSessionId}/record-files/{recordFileId}/flow?withoutCuts=false&recordAccessToken={token}
```

Clips: `eventLogs[]` where `module == mediasession.add` and `data.url` is

`https://events-storage.webinar.ru/api-storage/files/wowza/{yyyy}/{mm}/{dd}/{hash}.mp4`

HLS twin (what the player plays):

`https://events-delivery-records.webinar.ru/record/{yyyy}/{mm}/{dd}/{hash}.mp4/playlist.m3u8`

Wowza MP4s usually download without cookies (`200`, `video/mp4`). Use 2 parallel workers; 5+ often hits `WinError 10054` / read timeout. Resume via `.part` + `Range`.

`convertedRecords` is empty unless the organizer made an official single file.

## Audio vs picture (verified 2026-09-14 lecture)

- 1080p screen (~hours, hundreds of MB): video only, RMS 0.
- Audio-only wowza 50–130 MB: many are digital silence (max ≈ −90 dB). Some look silent on a 2-point RMS sample but `volumedetect` max is −3 to −15 dB — those are other participants who speak rarely. Mix them.
- 640×360 camera VA files: real speech. Several cameras can overlap; each person is a separate file. Mixing only the longest stream drops everyone except the lecturer.
- Speaker names are **not** on `mediasession.add`. Join `data.stream.conference.id` to `conference.add` / `conference.update` (`user.id`, `user.nickname`). `participation.rename` updates the nickname. Screenshare files have `stream.screensharing` and usually no audio.
- Per-speaker stems: pad each person's live clips with `adelay`/`apad`/`atrim` to the lecture duration (16 kHz mono). Whisper timestamps then match `lecture.mp4` without diarization.

mtslinker `processor.py` treats failed `VideoFileClip` opens as audio and then `with_audio(combined_audio)`, wiping camera sound.

## mtslinker URL regex

Does not capture the trailing access token. `/j/` is optional in `(?:[^/]+/)?`. Without `record-file/{id}` it calls `/api/eventsessions/{id}/record` on `my.mts-link.ru` with only a cookie — 403 for participant-only and for token-only public links.
