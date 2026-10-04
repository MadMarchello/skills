#!/usr/bin/env python3
"""Download an MTS Link recording and assemble one MP4 with audible audio."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import struct
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import imageio_ffmpeg
from tqdm import tqdm

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
WORKERS = 2
RETRIES = 6
LIVE_RMS = 200.0
LIVE_MAX_DB = -25.0
URL_RE = re.compile(
    r"https://my\.mts-link\.ru/(?:j/)?(?P<org>\d+)/(?P<event>\d+)/record-new/"
    r"(?P<session>\d+)(?:/record-file/(?P<record>\d+))?(?:/(?P<token>[0-9a-fA-F]{16,64}))?/?",
    re.I,
)


def ffmpeg_bin() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def parse_url(url: str) -> dict[str, str | None]:
    m = URL_RE.match(url.strip())
    if not m:
        raise SystemExit(f"Unrecognized MTS Link URL: {url}")
    return m.groupdict()


def record_api_urls(session: str, record: str | None, token: str | None) -> list[str]:
    qs = {"withoutCuts": "false"}
    if token:
        qs["recordAccessToken"] = token
    q = urlencode(qs)
    urls = [f"https://gw.mts-link.ru/api/eventsessions/{session}/record?{q}"]
    if record:
        urls.insert(
            0,
            f"https://gw.mts-link.ru/api/event-sessions/{session}/record-files/{record}/flow?{q}",
        )
    return urls


def fetch_record_json(
    session: str,
    record: str | None,
    token: str | None,
    cookie_session_id: str | None,
) -> dict[str, Any]:
    cookies = {}
    if cookie_session_id:
        cookies["sessionId"] = cookie_session_id
    headers = {"User-Agent": UA, "Accept": "application/json"}
    last_status = None
    with httpx.Client(timeout=60.0, follow_redirects=True, cookies=cookies) as client:
        for url in record_api_urls(session, record, token):
            r = client.get(url, headers=headers)
            last_status = r.status_code
            try:
                data = r.json()
            except Exception:
                data = {}
            if r.status_code == 200 and isinstance(data, dict) and data.get("eventLogs"):
                return data
            if r.status_code == 403 or (isinstance(data, dict) and data.get("error", {}).get("code") == 403):
                continue
    raise SystemExit(
        "403 from /record. The hex token in the share URL is recordAccessToken, "
        "not --session-id. For participant-only recordings log in via Playwright "
        "and fetch the JSON in-page (credentials: include), or pass the real "
        f"sessionId cookie. last_status={last_status}"
    )


def _user_display(user: dict[str, Any]) -> tuple[int | None, str | None, str | None]:
    user_id = user.get("id")
    nickname = str(user.get("nickname") or "").strip() or None
    name = " ".join(p for p in (user.get("name"), user.get("secondName")) if p).strip() or None
    return (int(user_id) if user_id is not None else None), nickname, name


def conference_users(data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Map conference/screenshare stream id -> participant."""
    users: dict[int, dict[str, Any]] = {}
    by_part: dict[int, dict[str, Any]] = {}
    for ev in data.get("eventLogs") or []:
        if not isinstance(ev, dict):
            continue
        module = ev.get("module")
        payload = ev.get("data") if isinstance(ev.get("data"), dict) else None
        if payload is None:
            continue
        if module in {"conference.add", "conference.update", "conference.delete"}:
            cid = payload.get("id")
            if cid is None:
                continue
            cid = int(cid)
            row = users.get(cid, {})
            part_id = payload.get("participationId")
            if part_id is not None:
                row["participation_id"] = int(part_id)
            uid, nick, name = _user_display(payload.get("user") or {})
            if uid is None and payload.get("userId") is not None:
                uid = int(payload["userId"])
            if uid is not None:
                row["user_id"] = uid
            if nick:
                row["nickname"] = nick
            if name:
                row["display_name"] = name
            users[cid] = row
            if "participation_id" in row:
                by_part[row["participation_id"]] = row
        elif module == "participation.rename":
            part_id = payload.get("id")
            nick = str(payload.get("nickname") or "").strip()
            if part_id is not None and nick and int(part_id) in by_part:
                by_part[int(part_id)]["nickname"] = nick
    return users


def clip_from_media_event(ev: dict[str, Any], users: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    payload = ev.get("data") if isinstance(ev.get("data"), dict) else None
    if not payload:
        return None
    url = payload.get("url")
    if not url:
        return None
    stream = payload.get("stream") if isinstance(payload.get("stream"), dict) else {}
    conf = None
    kind = None
    if isinstance(stream.get("conference"), dict) and stream["conference"].get("id") is not None:
        conf = int(stream["conference"]["id"])
        kind = "conference"
    elif isinstance(stream.get("screensharing"), dict) and stream["screensharing"].get("id") is not None:
        conf = int(stream["screensharing"]["id"])
        kind = "screensharing"
    person = users.get(conf or -1, {})
    return {
        "start": float(ev.get("relativeTime") or 0),
        "name": url.rsplit("/", 1)[-1],
        "url": url,
        "kind": kind,
        "conference_id": conf,
        "stream_id": stream.get("id"),
        "user_id": person.get("user_id"),
        "nickname": person.get("nickname"),
        "display_name": person.get("display_name"),
    }


def clips_from_record(data: dict[str, Any]) -> tuple[str, float, list[dict]]:
    users = conference_users(data)
    clips = []
    seen: set[str] = set()
    for ev in data.get("eventLogs") or []:
        if not isinstance(ev, dict):
            continue
        clip = clip_from_media_event(ev, users)
        if not clip or clip["url"] in seen:
            continue
        seen.add(clip["url"])
        clips.append(clip)
    name = re.sub(r'[\\/:*?"<>|]+', " ", str(data.get("name") or "lecture")).strip()
    duration = float(data.get("duration") or 0)
    if not clips or not duration:
        raise SystemExit("Record JSON has no media clips or duration")
    return name, duration, clips


def compact_clip(c: dict) -> dict[str, Any]:
    row: dict[str, Any] = {"start": c["start"], "url": c["url"]}
    for key in ("kind", "conference_id", "stream_id", "user_id", "nickname", "display_name"):
        if c.get(key) is not None:
            row[key if key != "display_name" else "name"] = c[key]
    return row


def clip_from_compact_row(row: dict[str, Any] | list) -> dict[str, Any]:
    if isinstance(row, (list, tuple)):
        start, url = row
        return {"start": float(start), "name": url.rsplit("/", 1)[-1], "url": url}
    url = row["url"]
    return {
        "start": float(row.get("start") or 0),
        "name": url.rsplit("/", 1)[-1],
        "url": url,
        "kind": row.get("kind"),
        "conference_id": row.get("conference_id"),
        "stream_id": row.get("stream_id"),
        "user_id": row.get("user_id"),
        "nickname": row.get("nickname"),
        "display_name": row.get("name") or row.get("display_name"),
    }


def load_clips(meta_path: Path) -> tuple[str, float, list[dict]]:
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    if "eventLogs" in data:
        return clips_from_record(data)
    clips = [clip_from_compact_row(row) for row in data["clips"]]
    return data["name"], float(data["duration"]), clips


def download_one(clip: dict, dest_dir: Path) -> str:
    dest = dest_dir / clip["name"]
    clip["path"] = dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    if dest.exists() and dest.stat().st_size > 0:
        return f"skip {dest.name}"
    last_err: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            headers = {"User-Agent": UA}
            if tmp.exists() and tmp.stat().st_size > 0:
                headers["Range"] = f"bytes={tmp.stat().st_size}-"
            timeout = httpx.Timeout(60.0, read=600.0, write=60.0, pool=60.0)
            with httpx.Client(timeout=timeout, follow_redirects=True, http2=False) as client:
                with client.stream("GET", clip["url"], headers=headers) as resp:
                    if resp.status_code == 416 and tmp.exists():
                        tmp.replace(dest)
                        return f"ok {dest.name} ({dest.stat().st_size})"
                    resp.raise_for_status()
                    mode = "ab" if resp.status_code == 206 else "wb"
                    if resp.status_code != 206 and tmp.exists():
                        tmp.unlink()
                    with open(tmp, mode) as f:
                        for chunk in resp.iter_bytes(1024 * 256):
                            f.write(chunk)
            tmp.replace(dest)
            return f"ok {dest.name} ({dest.stat().st_size})"
        except Exception as e:
            last_err = e
            time.sleep(min(2 * attempt, 12))
    raise last_err or RuntimeError("download failed")


def download_all(clips: list[dict], chunks: Path) -> None:
    errors: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(download_one, c, chunks): c for c in clips}
        with tqdm(total=len(clips), desc="download") as bar:
            for fut in as_completed(futs):
                try:
                    msg = fut.result()
                    bar.set_postfix_str(msg[:40], refresh=False)
                except Exception as e:
                    errors.append((futs[fut]["name"], str(e)))
                    bar.set_postfix_str("ERR", refresh=False)
                bar.update(1)
    if not errors:
        return
    print("retrying failed serially:", [n for n, _ in errors])
    serial_errors = []
    for name, _ in errors:
        clip = next(c for c in clips if c["name"] == name)
        try:
            print(download_one(clip, chunks))
        except Exception as e:
            serial_errors.append((name, str(e)))
    if serial_errors:
        raise SystemExit(f"download errors: {serial_errors}")


def probe(path: Path) -> dict:
    proc = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    err = proc.stderr or ""
    dur = 0.0
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", err)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    width = height = 0
    vm = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    if vm:
        width, height = int(vm.group(1)), int(vm.group(2))
    return {
        "duration": dur,
        "has_video": "Video:" in err,
        "has_audio": "Audio:" in err,
        "width": width,
        "height": height,
        "size": path.stat().st_size if path.exists() else 0,
    }


def pick_video_timeline(clips: list[dict], total: float) -> list[dict]:
    videos = [
        c
        for c in clips
        if c["info"]["has_video"] and c["info"]["duration"] > 0.4 and c["info"]["width"] >= 320
    ]
    if not videos:
        return []

    def score(c: dict) -> tuple:
        i = c["info"]
        return (i["width"] * i["height"], i["size"], i["duration"])

    events = []
    for c in videos:
        events.append((c["start"], 1, c))
        events.append((c["start"] + c["info"]["duration"], -1, c))
    events.sort(key=lambda x: (x[0], x[1]))
    active: list[dict] = []
    timeline = []
    last_t = 0.0
    current = None
    for t, kind, clip in events:
        t = min(max(t, 0.0), total)
        if t > last_t and current is not None:
            timeline.append({"clip": current, "from": last_t, "to": t})
        if kind == 1:
            active.append(clip)
        else:
            active = [a for a in active if a["name"] != clip["name"]]
        current = max(active, key=score) if active else None
        last_t = t
    if last_t < total and current is not None:
        timeline.append({"clip": current, "from": last_t, "to": total})
    merged = []
    for seg in timeline:
        if (
            merged
            and merged[-1]["clip"]["name"] == seg["clip"]["name"]
            and abs(merged[-1]["to"] - seg["from"]) < 0.05
        ):
            merged[-1]["to"] = seg["to"]
        else:
            merged.append(seg)
    return [s for s in merged if s["to"] - s["from"] > 0.05]


def interval_overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def audio_rms(path: Path, at: float) -> float:
    wav = path.with_suffix(".rms.wav")
    cmd = [
        ffmpeg_bin(), "-y", "-ss", str(max(at, 0)), "-t", "6", "-i", str(path),
        "-vn", "-ac", "1", "-ar", "16000", str(wav),
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not wav.exists() or wav.stat().st_size < 44:
        wav.unlink(missing_ok=True)
        return 0.0
    with wave.open(str(wav), "rb") as w:
        data = w.readframes(w.getnframes())
    wav.unlink(missing_ok=True)
    n = len(data) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack("<" + "h" * n, data)
    return math.sqrt(sum(s * s for s in samples) / n)


def volume_stats(path: Path) -> tuple[float, float]:
    r = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    mean_db, max_db = -120.0, -120.0
    for line in (r.stderr or "").splitlines():
        if "mean_volume:" in line:
            mean_db = float(line.split("mean_volume:")[-1].replace("dB", "").strip())
        elif "max_volume:" in line:
            max_db = float(line.split("max_volume:")[-1].replace("dB", "").strip())
    return mean_db, max_db


def fill_audio_energy(clips: list[dict]) -> None:
    for c in clips:
        info = c["info"]
        if not info["has_audio"] or info["duration"] < 2:
            info["rms"] = 0.0
            info["mean_db"] = -120.0
            info["max_db"] = -120.0
            continue
        t1 = min(max(info["duration"] * 0.2, 8), max(info["duration"] - 3, 0))
        t2 = min(max(info["duration"] * 0.55, 20), max(info["duration"] - 3, 0))
        info["rms"] = max(audio_rms(c["path"], t1), audio_rms(c["path"], t2))
        mean_db, max_db = volume_stats(c["path"])
        info["mean_db"] = mean_db
        info["max_db"] = max_db


def is_live_clip(c: dict) -> bool:
    info = c.get("info") or {}
    if not info.get("has_audio") or info.get("duration", 0) <= 2:
        return False
    if c.get("kind") == "screensharing":
        return False
    return info.get("rms", 0) >= LIVE_RMS or info.get("max_db", -120) >= LIVE_MAX_DB


def pick_audio_tracks(clips: list[dict], limit: int = 16) -> list[dict]:
    # Mix every live mic, including overlapping cameras and sparse speakers
    # whose 2-point RMS looks silent but volumedetect max_volume is high.
    live = [c for c in clips if is_live_clip(c)]
    live.sort(key=lambda x: -x["info"]["duration"])
    return live[:limit]


def speaker_key(c: dict) -> str:
    uid = c.get("user_id")
    if uid is not None:
        return f"user:{uid}"
    return f"clip:{c['name']}"


def speaker_label(c: dict) -> str:
    return str(c.get("nickname") or c.get("display_name") or c["name"][:16]).strip()


def safe_filename(label: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', " ", label).strip(" .")
    s = re.sub(r"\s+", " ", s)
    return s[:80] or "speaker"


def group_speakers(clips: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for c in clips:
        if not is_live_clip(c):
            continue
        key = speaker_key(c)
        g = groups.get(key)
        if g is None:
            g = {
                "key": key,
                "user_id": c.get("user_id"),
                "label": speaker_label(c),
                "clips": [],
            }
            groups[key] = g
        else:
            lab = speaker_label(c)
            if len(lab) > len(g["label"]):
                g["label"] = lab
        g["clips"].append(c)
    out = list(groups.values())
    out.sort(key=lambda g: -sum(x["info"]["duration"] for x in g["clips"]))
    return out


STEM_CODECS = {
    "flac": (".flac", ["-c:a", "flac"]),
    "wav": (".wav", ["-c:a", "pcm_s16le"]),
    "m4a": (".m4a", ["-c:a", "aac", "-b:a", "64k"]),
}


def unique_stem_name(label: str, used: dict[str, int], ext: str) -> str:
    base = safe_filename(label)
    n = used.get(base, 0) + 1
    used[base] = n
    suffix = "" if n == 1 else f"-{n}"
    return f"{base}{suffix}{ext}"


def export_one_stem(speaker: dict, dest: Path, total: float, fmt: str) -> dict:
    _ext, codec = STEM_CODECS[fmt]
    clips = speaker["clips"]
    inputs: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    for i, c in enumerate(clips):
        inputs += ["-i", str(c["path"])]
        delay = int(max(c["start"], 0) * 1000)
        lab = f"a{i}"
        filters.append(
            f"[{i}:a]aformat=channel_layouts=mono,aresample=16000,"
            f"adelay={delay}:all=1,apad=whole_dur={total:.3f},"
            f"atrim=0:{total:.3f}[{lab}]"
        )
        labels.append(f"[{lab}]")
    if len(labels) == 1:
        filters.append(f"{labels[0]}anull[aout]")
    else:
        n = len(labels)
        filters.append(
            f"{''.join(labels)}amix=inputs={n}:duration=first:dropout_transition=0:normalize=0,"
            f"alimiter=limit=0.95[aout]"
        )
    cmd = [
        ffmpeg_bin(), "-y", *inputs, "-filter_complex", ";".join(filters),
        "-map", "[aout]", *codec, "-t", f"{total:.3f}", str(dest),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "ffmpeg stem failed")[-800:])
    info = probe(dest)
    return {
        "file": dest.name,
        "duration": info["duration"],
        "size": dest.stat().st_size if dest.exists() else 0,
        "ok": abs(info["duration"] - total) <= 1.0,
    }


def export_stems(
    dest_dir: Path,
    clips: list[dict],
    total: float,
    fmt: str = "flac",
) -> list[dict]:
    if fmt not in STEM_CODECS:
        raise SystemExit(f"Unknown --stems-format {fmt}. Use flac, wav, or m4a.")
    speakers = group_speakers(clips)
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext, _codec = STEM_CODECS[fmt]
    used: dict[str, int] = {}
    manifest = {
        "duration": total,
        "sample_rate": 16000,
        "channels": 1,
        "format": fmt,
        "speakers": [],
    }
    print(f"stems: {len(speakers)} speakers -> {dest_dir}")
    for i, sp in enumerate(speakers, 1):
        fname = f"{i:02d}-{unique_stem_name(sp['label'], used, ext)}"
        path = dest_dir / fname
        print(f"  {fname} clips={len(sp['clips'])} {sp['label']}")
        row = export_one_stem(sp, path, total, fmt)
        row.update(
            {
                "index": i,
                "user_id": sp.get("user_id"),
                "name": sp["label"],
                "sources": [
                    {
                        "file": c["name"],
                        "start": c["start"],
                        "duration": c["info"]["duration"],
                        "rms": c["info"].get("rms"),
                        "max_db": c["info"].get("max_db"),
                    }
                    for c in sp["clips"]
                ],
            }
        )
        manifest["speakers"].append(row)
        print(f"    dur={row['duration']:.1f}s size={row['size']/1e6:.1f}MB ok={row['ok']}")
    (dest_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    bad = [s for s in manifest["speakers"] if not s.get("ok")]
    if bad:
        raise SystemExit(f"stem duration mismatch: {[s['file'] for s in bad]}")
    return manifest["speakers"]


def assemble(out: Path, total: float, clips: list[dict], max_duration: float | None) -> Path:
    use_total = min(total, max_duration) if max_duration else total
    segs = pick_video_timeline(clips, use_total)
    audios = pick_audio_tracks(clips)
    print(f"video segments: {len(segs)}")
    print(f"audio tracks: {len(audios)}")
    for a in audios:
        print(
            f"  audio t={a['start']:.1f} dur={a['info']['duration']:.1f}s "
            f"rms={a['info'].get('rms', 0):.0f} max={a['info'].get('max_db', 0):.1f}dB "
            f"{a['info']['size']/1e6:.1f}MB {a['name'][:16]}"
        )
    if not segs:
        raise SystemExit("no video streams found")
    if not audios:
        print("WARNING: no live audio tracks; output may be silent")

    max_w = max(s["clip"]["info"]["width"] or 1280 for s in segs)
    max_h = max(s["clip"]["info"]["height"] or 720 for s in segs)
    w, h = (1280, 720) if max_w * max_h >= 1280 * 720 else (max_w, max_h)
    if w % 2:
        w += 1
    if h % 2:
        h += 1

    inputs: list[str] = []
    filter_lines: list[str] = []
    v_labels: list[str] = []
    idx = 0
    cursor = 0.0
    for i, seg in enumerate(segs):
        if seg["from"] >= use_total:
            break
        to = min(seg["to"], use_total)
        gap = seg["from"] - cursor
        if gap > 0.05:
            filter_lines.append(f"color=c=black:s={w}x{h}:d={gap:.3f}:r=25[gap{i}]")
            v_labels.append(f"[gap{i}]")
        clip = seg["clip"]
        local = max(seg["from"] - clip["start"], 0)
        dur = to - seg["from"]
        inputs += ["-ss", f"{local:.3f}", "-t", f"{dur:.3f}", "-i", str(clip["path"])]
        filter_lines.append(
            f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,fps=25,setsar=1,format=yuv420p[v{i}]"
        )
        v_labels.append(f"[v{i}]")
        idx += 1
        cursor = to
    if cursor < use_total - 0.5:
        gap = use_total - cursor
        filter_lines.append(f"color=c=black:s={w}x{h}:d={gap:.3f}:r=25[gapend]")
        v_labels.append("[gapend]")

    filter_lines.append(f"{''.join(v_labels)}concat=n={len(v_labels)}:v=1:a=0[vout]")

    a_labels = []
    for j, c in enumerate(audios):
        inputs += ["-i", str(c["path"])]
        delay = int(max(c["start"], 0) * 1000)
        filter_lines.append(
            f"[{idx}:a]aformat=channel_layouts=stereo,aresample=48000,"
            f"adelay={delay}:all=1,apad=whole_dur={use_total:.3f},"
            f"atrim=0:{use_total:.3f}[a{j}]"
        )
        a_labels.append(f"[a{j}]")
        idx += 1
    map_audio: list[str] = []
    if a_labels:
        n = len(a_labels)
        filter_lines.append(
            f"{''.join(a_labels)}amix=inputs={n}:duration=first:dropout_transition=0:normalize=0,"
            f"alimiter=limit=0.95[aout]"
        )
        map_audio = ["-map", "[aout]"]

    graph = ";".join(filter_lines)
    cmd = [ffmpeg_bin(), "-y", *inputs, "-filter_complex", graph, "-map", "[vout]", *map_audio]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        "-t", f"{use_total:.3f}", str(out),
    ]
    print("ffmpeg assemble...")
    subprocess.run(cmd, check=True)
    return out


def verify_audio(path: Path, at_sec: float = 120.0) -> dict:
    wav = path.with_suffix(".probe.wav")
    times = [at_sec]
    for extra in (30.0, 90.0, 250.0, 400.0, 600.0, 1200.0):
        if extra not in times:
            times.append(extra)
    last = {"ok": False, "error": "no sample"}
    for t in times:
        cmd = [
            ffmpeg_bin(), "-y", "-ss", str(t), "-t", "8", "-i", str(path),
            "-vn", "-ac", "1", "-ar", "16000", str(wav),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0 or not wav.exists():
            last = {"ok": False, "error": (r.stderr or "")[-400:], "at": t}
            continue
        with wave.open(str(wav), "rb") as w:
            data = w.readframes(w.getnframes())
        wav.unlink(missing_ok=True)
        n = len(data) // 2
        if n == 0:
            last = {"ok": False, "error": "empty wav", "at": t}
            continue
        samples = struct.unpack("<" + "h" * n, data)
        rms = math.sqrt(sum(s * s for s in samples) / n)
        last = {"ok": rms > LIVE_RMS, "rms": rms, "peak": max(abs(s) for s in samples), "at": t}
        if last["ok"]:
            return last
    return last


def main() -> None:
    parser = argparse.ArgumentParser(description="Download MTS Link lecture with audible audio")
    parser.add_argument("url", nargs="?", help="https://my.mts-link.ru/j/.../record-new/...")
    parser.add_argument("--meta", help="clips JSON or raw /record JSON")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--session-id", help="Cookie sessionId after login, not the URL hex token")
    parser.add_argument("--max-duration", type=float, default=None)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument(
        "--reuse-probe",
        action="store_true",
        help="Reuse out-dir/probe.json instead of probing chunks again",
    )
    parser.add_argument(
        "--stems-only",
        action="store_true",
        help="Do not rebuild lecture.mp4; only write per-speaker audio stems",
    )
    parser.add_argument("--skip-stems", action="store_true", help="Do not write per-speaker stems")
    parser.add_argument(
        "--stems-format",
        choices=sorted(STEM_CODECS),
        default="flac",
        help="Per-speaker audio format (16 kHz mono, same duration as the video)",
    )
    args = parser.parse_args()

    root = Path(args.out_dir)
    chunks = root / "chunks"
    root.mkdir(parents=True, exist_ok=True)
    chunks.mkdir(parents=True, exist_ok=True)

    if args.meta:
        name, total, clips = load_clips(Path(args.meta))
    elif args.url:
        parts = parse_url(args.url)
        raw = fetch_record_json(parts["session"], parts["record"], parts["token"], args.session_id)
        (root / "record.json").write_text(json.dumps(raw), encoding="utf-8")
        name, total, clips = clips_from_record(raw)
        compact = {"name": name, "duration": total, "clips": [compact_clip(c) for c in clips]}
        (root / "clips.json").write_text(json.dumps(compact, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        raise SystemExit("Pass a recording URL or --meta clips.json")

    out = root / "lecture.mp4"
    print(f"{name}\nclips={len(clips)} duration={total:.1f}s out={out}")

    if not args.skip_download:
        download_all(clips, chunks)
    else:
        for c in clips:
            c["path"] = chunks / c["name"]

    probe_path = root / "probe.json"
    probed = []
    if args.reuse_probe and probe_path.exists():
        rows = json.loads(probe_path.read_text(encoding="utf-8"))
        by_name = {row["name"]: row for row in rows}
        for c in clips:
            c["path"] = c.get("path") or (chunks / c["name"])
            row = by_name.get(c["name"])
            if not row:
                raise SystemExit(f"probe.json missing {c['name']}")
            c["info"] = {
                "duration": row["duration"],
                "has_video": row["has_video"],
                "has_audio": row["has_audio"],
                "width": row.get("width", 0),
                "height": row.get("height", 0),
                "size": row.get("size", 0),
                "rms": row.get("rms", 0),
                "mean_db": row.get("mean_db", -120.0),
                "max_db": row.get("max_db", -120.0),
            }
            probed.append(row)
        print("reused probe.json")
        if any(
            c["info"]["has_audio"]
            and c["info"]["duration"] > 2
            and c["info"].get("max_db", -120) <= -119
            for c in clips
        ):
            print("volumedetect for sparse speakers...")
            for c in clips:
                if not c["info"]["has_audio"] or c["info"]["duration"] <= 2:
                    continue
                mean_db, max_db = volume_stats(c["path"])
                c["info"]["mean_db"] = mean_db
                c["info"]["max_db"] = max_db
            for row in probed:
                src = next(c for c in clips if c["name"] == row["name"])
                row["mean_db"] = src["info"].get("mean_db", -120)
                row["max_db"] = src["info"].get("max_db", -120)
            probe_path.write_text(json.dumps(probed, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        for c in clips:
            path = c.get("path") or (chunks / c["name"])
            c["path"] = path
            info = probe(path)
            c["info"] = info
            probed.append({"name": c["name"], "start": c["start"], **info})
        fill_audio_energy(clips)
        for row in probed:
            src = next(c for c in clips if c["name"] == row["name"])
            row["rms"] = src["info"].get("rms", 0)
            row["mean_db"] = src["info"].get("mean_db", -120)
            row["max_db"] = src["info"].get("max_db", -120)
        probe_path.write_text(json.dumps(probed, ensure_ascii=False, indent=2), encoding="utf-8")
    live = [
        p
        for p in probed
        if p.get("rms", 0) >= LIVE_RMS or p.get("max_db", -120) >= LIVE_MAX_DB
    ]
    print(
        f"probed video={sum(1 for p in probed if p['has_video'])} "
        f"audio-only={sum(1 for p in probed if p['has_audio'] and not p['has_video'])} "
        f"live-audio={len(live)}"
    )

    use_total = min(total, args.max_duration) if args.max_duration else total
    if not args.stems_only:
        assemble(out, total, clips, args.max_duration)
        print("DONE", out, f"{out.stat().st_size/1e6:.1f} MB")
        sample_at = 30.0 if args.max_duration and args.max_duration < 90 else min(300.0, total / 4)
        chk = verify_audio(out, at_sec=sample_at)
        print("AUDIO_CHECK", chk)
        if not chk.get("ok"):
            raise SystemExit("assembled file has no audible audio")
    if not args.skip_stems:
        stems = export_stems(root / "stems", clips, use_total, args.stems_format)
        print("STEMS", len(stems), "files in", root / "stems")


if __name__ == "__main__":
    main()
