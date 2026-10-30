# subtitles-sync

Jellyfin webhook service that syncs external subtitles (`.srt`, `.ass`, `.ssa`)
to the video with [ffsubsync](https://github.com/smacke/ffsubsync).

## How it works

1. Jellyfin's Webhook plugin sends **Playback Start** to `http://127.0.0.1:8095/webhook`.
2. The video is read **once** to build a local reference: an embedded text
   subtitle track if the video has one, otherwise its audio.
3. Each subtitle is synced with **split mode** (`--split-penalty`), so the
   offset can change during the episode. This fixes subtitles from TV airings
   (ad/act breaks) used on Blu-ray video. If split mode fails, it retries with
   a single offset.
4. The result is written as `Name.synced.srt`. The original is renamed to
   `Name.srt.orig` (Jellyfin ignores it) and Jellyfin is asked to refresh the item.

Jobs run one at a time; duplicate events for the same video are ignored.

## Setup

```bash
cp .env.example .env   # set JELLYFIN_API_KEY
docker compose up -d --build
```

All settings are environment variables; see `docker-compose.yml`.

| Variable | Default | Meaning |
|---|---|---|
| `SPLIT_PENALTY` | `7` | Lower = detects more breaks. Empty = single offset (old behaviour). |
| `MAX_OFFSET_SECONDS` | `60` | Largest shift allowed. Big values make split mode match lines to unrelated speech. |
| `MAX_SPLIT_SPREAD_SECONDS` | `30` | Split results whose offsets differ more than this, or that put lines out of order, are rejected and redone with a single offset. |
| `USE_EMBEDDED_SUBS` | `true` | Use a text subtitle track inside the video as reference. |
| `FFSUBSYNC_EXTRA_ARGS` | | Extra ffsubsync flags, e.g. `--vad silero`. |
| `ORIGINALS` | `keep` | `keep` = rename to `.orig`, `delete` = remove. |
| `REFRESH_JELLYFIN` | `true` | Refresh the item after writing. |
| `EXCLUDED_PATHS` | see compose | `\|`-separated folders to skip. |

## Re-syncing bad results

```bash
curl -X POST http://127.0.0.1:8095/sync -d '{"path": "/mnt/gdrive/Media/Show/S01E01.mkv", "force": true}'
```

Or inside the container: `docker exec subsync-daemon python sync_core.py "/path/video.mkv" --force`.
Force mode syncs from the `.orig` file, or from the old `.synced.srt` if the
original was deleted by an older version.

## Tests

```bash
python -m unittest test_sync_core -v
```
