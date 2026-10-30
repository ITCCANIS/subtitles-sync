"""Subtitle sync logic, independent of the web server.

Run directly for manual / batch use:
    python sync_core.py "/mnt/gdrive/Media/Show/S01E01.mkv" [--force]
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger("subsync")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str) -> list:
    # "|" separated, because paths often contain spaces and commas.
    raw = os.getenv(name, "")
    return [p.strip() for p in raw.split("|") if p.strip()]


@dataclass
class Config:
    jellyfin_url: str = os.getenv("JELLYFIN_URL", "http://127.0.0.1:8096")
    api_key: str = os.getenv("JELLYFIN_API_KEY", "")
    excluded_paths: list = field(default_factory=lambda: _env_list("EXCLUDED_PATHS"))

    # Split mode lets each part of the episode get its own offset, which
    # fixes TV versions with ad/act breaks synced to Blu-ray. Empty = off.
    split_penalty: str = os.getenv("SPLIT_PENALTY", "7")
    # If the split run fails, try again with a single global offset.
    fallback_no_split: bool = _env_bool("FALLBACK_NO_SPLIT", True)
    # Larger values let split mode match lines to random speech far away.
    max_offset_seconds: str = os.getenv("MAX_OFFSET_SECONDS", "60")
    # Reject split results whose offsets differ by more than this overall.
    max_split_spread: float = float(os.getenv("MAX_SPLIT_SPREAD_SECONDS", "30"))
    # Prefer a text subtitle track inside the video as the timing reference.
    use_embedded_subs: bool = _env_bool("USE_EMBEDDED_SUBS", True)
    # For 5.1/7.1 audio, detect speech on the center (dialogue) channel only.
    dialogue_channel: bool = _env_bool("DIALOGUE_CHANNEL", True)
    parallel_workers: str = os.getenv("PARALLEL_WORKERS", "6")
    # Anything else to pass to ffsubsync, e.g. "--vad silero --gss".
    extra_args: list = field(
        default_factory=lambda: os.getenv("FFSUBSYNC_EXTRA_ARGS", "").split()
    )
    timeout_seconds: int = int(os.getenv("SYNC_TIMEOUT_SECONDS", "3600"))
    # "keep": rename the original to <name>.orig (hidden from Jellyfin,
    # available for re-syncing). "delete": old behaviour.
    originals: str = os.getenv("ORIGINALS", "keep").strip().lower()
    refresh_jellyfin: bool = _env_bool("REFRESH_JELLYFIN", True)
    tmp_dir: str = os.getenv("TMP_DIR", tempfile.gettempdir())


SUB_EXTS = (".srt", ".ass", ".ssa")
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
SYNCED_TAG = ".synced"
ORIG_SUFFIX = ".orig"


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def check_file_accessible(path: str) -> bool:
    """Checks a file is readable on FUSE even if xattr calls raise EIO."""
    if not path:
        return False
    try:
        fd = os.open(path, os.O_RDONLY)
        os.close(fd)
        return True
    except OSError:
        pass
    try:
        with open(path, "rb") as f:
            f.read(1)
        return True
    except Exception:
        return False


def is_path_excluded(file_path: str, excluded: list) -> bool:
    norm = os.path.abspath(file_path)
    for ex in excluded:
        ex = os.path.abspath(ex)
        if norm == ex or norm.startswith(ex + os.sep):
            return True
    return False


def synced_path_for(sub_path: str) -> str:
    """Movie.en.srt -> Movie.en.synced.srt"""
    stem, ext = os.path.splitext(sub_path)
    return f"{stem}{SYNCED_TAG}{ext}"


def is_synced_name(name: str) -> bool:
    stem, _ = os.path.splitext(name)
    return stem.endswith(SYNCED_TAG)


def find_subtitles(video_path: str) -> list:
    """Return original subtitle paths belonging to this exact video.

    A subtitle belongs to the video when it is named "<video>.<ext>" or
    "<video>.<anything>.<ext>". The old "<video>*" glob also matched other
    episodes ("S01E1" matched "S01E10.srt") and then deleted their files.

    Originals already renamed to "*.orig" are returned under their original
    name, so callers can decide whether to re-sync them.
    """
    folder = os.path.dirname(video_path) or "."
    base = os.path.splitext(os.path.basename(video_path))[0]
    found = set()
    try:
        names = os.listdir(folder)
    except OSError as e:
        log.warning("Cannot list %s: %s", folder, e)
        return []

    for name in names:
        logical = name[: -len(ORIG_SUFFIX)] if name.endswith(ORIG_SUFFIX) else name
        stem, ext = os.path.splitext(logical)
        if ext.lower() not in SUB_EXTS:
            continue
        if stem != base and not stem.startswith(base + "."):
            continue
        if is_synced_name(logical):
            continue
        found.add(os.path.join(folder, logical))
    return sorted(found)


def copy_plain(src: str, dst: str) -> None:
    # Plain stream copy: copy2/copystat fail on the FUSE mount (xattr EIO).
    shutil.copyfile(src, dst)


# ---------------------------------------------------------------------------
# Reference selection
# ---------------------------------------------------------------------------

def probe_streams(video_path: str) -> list:
    cmd = [
        "ffprobe", "-v", "error", "-show_streams", "-of", "json", video_path,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return json.loads(res.stdout or "{}").get("streams", [])
    except Exception as e:
        log.warning("ffprobe failed for %s: %s", video_path, e)
        return []


def pick_text_subtitle_stream(streams: list):
    """Index (among subtitle streams) of the best embedded text track.

    Skips forced tracks (they only cover a few lines) and prefers the
    track with the most cues, so the reference covers the whole episode.
    """
    best, best_score = None, None
    sub_index = -1
    for s in streams:
        if s.get("codec_type") != "subtitle":
            continue
        sub_index += 1
        if s.get("codec_name") not in TEXT_SUB_CODECS:
            continue
        disp = s.get("disposition", {}) or {}
        tags = {k.lower(): v for k, v in (s.get("tags", {}) or {}).items()}
        title = str(tags.get("title", "")).lower()
        if disp.get("forced") or "forced" in title:
            continue
        frames = tags.get("number_of_frames") or tags.get("number_of_frames-eng")
        try:
            score = int(frames)
        except (TypeError, ValueError):
            score = 0
        if best is None or score > best_score:
            best, best_score = sub_index, score
    return best


def extract_reference(video_path: str, workdir: str, cfg: Config) -> str:
    """Read the (remote, large) video once and build a small local reference.

    Uses an embedded text subtitle track when there is one (much more
    precise than speech detection), otherwise a mono 16 kHz audio file.
    All subtitles of the video then sync against that local file.
    """
    streams = probe_streams(video_path)
    if cfg.use_embedded_subs:
        idx = pick_text_subtitle_stream(streams)
        if idx is not None:
            ref = os.path.join(workdir, "reference.srt")
            cmd = [
                "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", video_path,
                "-map", f"0:s:{idx}", "-c:s", "srt", ref,
            ]
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=cfg.timeout_seconds
            )
            if res.returncode == 0 and os.path.getsize(ref) > 0:
                log.info("Reference: embedded subtitle track s:%d", idx)
                return ref
            log.warning("Embedded subtitle extraction failed, using audio")

    ref = os.path.join(workdir, "reference.wav")
    center = cfg.dialogue_channel and has_center_channel(streams)
    attempts = [True, False] if center else [False]
    res = None
    for use_center in attempts:
        res = subprocess.run(
            audio_extract_cmd(video_path, ref, use_center),
            capture_output=True, text=True, timeout=cfg.timeout_seconds,
        )
        if res.returncode == 0 and os.path.exists(ref) and os.path.getsize(ref) > 0:
            log.info("Reference: audio track, %s (extracted locally)",
                     "center/dialogue channel" if use_center else "full mix")
            return ref
        if use_center:
            log.warning("Center channel extraction failed, using full mix")
    raise RuntimeError(f"audio extraction failed: {res.stderr.strip()[-500:]}")


def first_audio_stream(streams: list):
    return next((s for s in streams if s.get("codec_type") == "audio"), None)


def has_center_channel(streams: list) -> bool:
    """True for 5.1/7.1-style audio, where dialogue sits in the center."""
    audio = first_audio_stream(streams)
    if not audio:
        return False
    layout = str(audio.get("channel_layout", "")).lower()
    if layout:
        return layout.startswith(("5.", "6.", "7.")) or "fc" in layout.split("(")[0]
    return int(audio.get("channels", 0) or 0) >= 6


def audio_extract_cmd(video_path: str, out: str, center_only: bool) -> list:
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", video_path,
           "-map", "0:a:0", "-vn"]
    if center_only:
        # Music and effects live in the other channels and make speech
        # detection fire early; the center channel is mostly dialogue.
        cmd += ["-af", "pan=mono|c0=FC"]
    else:
        cmd += ["-ac", "1"]
    return cmd + ["-ar", "16000", "-c:a", "pcm_s16le", out]


# ---------------------------------------------------------------------------
# ffsubsync
# ---------------------------------------------------------------------------

def build_ffsubsync_cmd(reference: str, sub_in: str, sub_out: str,
                        cfg: Config, split: bool) -> list:
    cmd = ["ffsubsync", reference, "-i", sub_in, "-o", sub_out]
    if cfg.max_offset_seconds:
        cmd += ["--max-offset-seconds", cfg.max_offset_seconds]
    if cfg.parallel_workers:
        cmd += ["--parallel-workers", cfg.parallel_workers]
    if split and cfg.split_penalty:
        cmd += ["--split-penalty", cfg.split_penalty]
    return cmd + cfg.extra_args


_SRT_TIME = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})"
)
_ASS_TIME = re.compile(r"^Dialogue:\s*[^,]*,(\d+):(\d{2}):(\d{2})\.(\d{2}),", re.M)


def read_cue_starts(path: str) -> list:
    """Start times (seconds) of every cue, in file order."""
    with open(path, "rb") as f:
        text = f.read().decode("utf-8", errors="replace")
    starts = [
        int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
        for h, m, s, ms, *_ in _SRT_TIME.findall(text)
    ]
    if not starts:
        starts = [
            int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100
            for h, m, s, cs in _ASS_TIME.findall(text)
        ]
    return starts


def check_sync_result(sub_in: str, sub_out: str, cfg: "Config") -> str:
    """Return "" if the result looks sane, otherwise the reason it doesn't.

    A TV -> Blu-ray difference moves the offset a few seconds per break, and
    lines stay in order. A bad split result scatters blocks of lines minutes
    apart and on top of each other, which is what these checks catch.
    """
    before, after = read_cue_starts(sub_in), read_cue_starts(sub_out)
    if not before or len(before) != len(after):
        return ""  # can't compare reliably; trust ffsubsync
    offsets = [b - a for a, b in zip(before, after)]
    spread = max(offsets) - min(offsets)
    if spread > cfg.max_split_spread:
        return f"offsets spread over {spread:.0f}s (limit {cfg.max_split_spread:.0f}s)"
    # Lines that were in order must stay in order.
    backwards = sum(
        1 for i in range(1, len(after))
        if before[i] >= before[i - 1] and after[i] < after[i - 1] - 1.0
    )
    if backwards:
        return f"{backwards} line(s) moved before the previous line"
    return ""


def run_ffsubsync(cmd: list, sub_out: str, timeout: int) -> bool:
    log.info("Running: %s", " ".join(cmd))
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.error("ffsubsync timed out after %ss", timeout)
        return False
    # ffsubsync's stderr is mostly progress bars; keep only the tail.
    tail = "\n".join((res.stderr or "").strip().splitlines()[-15:])
    if tail:
        log.info("ffsubsync output (tail):\n%s", tail)
    ok = res.returncode == 0 and os.path.exists(sub_out) and os.path.getsize(sub_out) > 0
    if not ok:
        log.warning("ffsubsync failed (return code %s)", res.returncode)
    return ok


# ---------------------------------------------------------------------------
# Jellyfin
# ---------------------------------------------------------------------------

def _jellyfin_request(cfg: Config, path: str, method: str = "GET"):
    req = urllib.request.Request(
        f"{cfg.jellyfin_url}{path}",
        method=method,
        # Newer Jellyfin versions reject the legacy X-Emby-Token header.
        headers={
            "Authorization": f'MediaBrowser Token="{cfg.api_key}"',
            "Accept": "application/json",
        },
        data=b"" if method == "POST" else None,
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode() or "{}"
        return json.loads(body) if body.strip().startswith(("{", "[")) else {}


def get_path_from_jellyfin(item_id: str, cfg: Config) -> str:
    if not cfg.api_key:
        log.error("No JELLYFIN_API_KEY set; cannot resolve ItemId %s", item_id)
        return ""
    try:
        data = _jellyfin_request(cfg, f"/Items?Ids={item_id}&Fields=Path")
        items = data.get("Items", [])
        if items and items[0].get("Path"):
            return items[0]["Path"]
    except Exception as e:
        log.error("Failed to fetch path for ItemId %s: %s", item_id, e)
    return ""


def refresh_jellyfin_item(item_id: str, cfg: Config) -> None:
    """Make Jellyfin notice the new subtitle without a full library scan."""
    if not (item_id and cfg.api_key and cfg.refresh_jellyfin):
        return
    try:
        _jellyfin_request(
            cfg,
            f"/Items/{item_id}/Refresh?MetadataRefreshMode=Default"
            "&ImageRefreshMode=None&ReplaceAllMetadata=false&ReplaceAllImages=false",
            method="POST",
        )
        log.info("Asked Jellyfin to refresh item %s", item_id)
    except Exception as e:
        log.warning("Jellyfin refresh failed for %s: %s", item_id, e)


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

def _plan_jobs(video_path: str, force: bool) -> list:
    """(input_file, original_path, synced_path) for each subtitle to sync."""
    jobs = []
    for sub in find_subtitles(video_path):
        synced = synced_path_for(sub)
        backup = sub + ORIG_SUFFIX
        source = sub if os.path.exists(sub) else backup
        if not os.path.exists(source):
            continue  # only an .orig-style name with no file behind it
        if not force:
            if os.path.exists(synced):
                log.info("Skip, already synced: %s", synced)
                continue
            if os.path.exists(sub + ".synced"):  # marker from old versions
                log.info("Skip, old .synced marker: %s", sub)
                continue
            if source == backup:
                continue  # original was already processed earlier
        jobs.append((source, sub, synced))

    if force:
        # Subtitles synced by older versions had their original deleted.
        # Re-sync those from the synced file itself (any shift ffsubsync
        # applied before is undone by the new sync anyway).
        planned = {j[2] for j in jobs}
        for orig_like in _synced_without_original(video_path):
            synced = synced_path_for(orig_like)
            if synced not in planned:
                jobs.append((synced, orig_like, synced))
    return jobs


def _synced_without_original(video_path: str) -> list:
    folder = os.path.dirname(video_path) or "."
    base = os.path.splitext(os.path.basename(video_path))[0]
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        stem, ext = os.path.splitext(name)
        if ext.lower() not in SUB_EXTS or not is_synced_name(name):
            continue
        if not stem.startswith(base + "."):
            continue
        original = os.path.join(folder, stem[: -len(SYNCED_TAG)] + ext)
        if not os.path.exists(original) and not os.path.exists(original + ORIG_SUFFIX):
            out.append(original)
    return sorted(out)


def _handle_original(original: str, source: str, cfg: Config) -> None:
    if source != original:
        return  # we synced from the .orig backup; nothing to move
    try:
        if cfg.originals == "delete":
            os.remove(original)
            log.info("Deleted original: %s", original)
        else:
            os.replace(original, original + ORIG_SUFFIX)
            log.info("Kept original as: %s%s", original, ORIG_SUFFIX)
    except OSError as e:
        log.warning("Could not move/delete original %s: %s", original, e)


def sync_subtitles_for_video(video_path: str, cfg: Config = None,
                             item_id: str = "", force: bool = False) -> int:
    """Sync every subtitle of one video. Returns how many were written."""
    cfg = cfg or Config()
    if is_path_excluded(video_path, cfg.excluded_paths):
        log.info("Skip, excluded path: %s", video_path)
        return 0
    if not check_file_accessible(video_path):
        log.warning("Video not accessible: %s", video_path)
        return 0

    jobs = _plan_jobs(video_path, force)
    if not jobs:
        log.info("Nothing to sync for: %s", video_path)
        return 0

    log.info("Start: %s (%d subtitle(s))", video_path, len(jobs))
    written = 0
    with tempfile.TemporaryDirectory(prefix="subsync_", dir=cfg.tmp_dir) as work:
        try:
            reference = extract_reference(video_path, work, cfg)
        except Exception as e:
            log.error("Could not build reference for %s: %s", video_path, e)
            return 0

        for n, (source, original, synced) in enumerate(jobs):
            ext = os.path.splitext(original)[1]
            local_in = os.path.join(work, f"in_{n}{ext}")
            local_out = os.path.join(work, f"out_{n}{ext}")
            try:
                copy_plain(source, local_in)
                ok = run_ffsubsync(
                    build_ffsubsync_cmd(reference, local_in, local_out, cfg, split=True),
                    local_out, cfg.timeout_seconds,
                )
                if ok and cfg.split_penalty:
                    problem = check_sync_result(local_in, local_out, cfg)
                    if problem:
                        log.warning("Rejected split result: %s", problem)
                        ok = False
                if not ok and cfg.split_penalty and cfg.fallback_no_split:
                    log.info("Retrying without split mode")
                    ok = run_ffsubsync(
                        build_ffsubsync_cmd(reference, local_in, local_out, cfg, split=False),
                        local_out, cfg.timeout_seconds,
                    )
                if not ok:
                    log.error("Sync failed, original left untouched: %s", source)
                    continue

                # Write next to the video via a temp name, then swap in, so
                # Jellyfin never sees a half-written file.
                partial = synced + ".part"
                copy_plain(local_out, partial)
                if os.path.getsize(partial) == 0:
                    os.remove(partial)
                    log.error("Empty output, original left untouched: %s", source)
                    continue
                os.replace(partial, synced)
                log.info("Wrote: %s", synced)
                written += 1
                _handle_original(original, source, cfg)
            except Exception as e:
                log.error("Failed processing %s: %s", source, e)

    if written:
        refresh_jellyfin_item(item_id, cfg)
    log.info("Done: %s (%d written)", video_path, written)
    return written


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(2)
    total = sum(sync_subtitles_for_video(p, force="--force" in sys.argv) for p in args)
    sys.exit(0 if total else 1)
