import os
import glob
import shutil
import subprocess
import json
import urllib.request
import uuid
from fastapi import FastAPI, BackgroundTasks, Request

app = FastAPI()

JELLYFIN_URL = "http://127.0.0.1:8096"
API_KEY = os.getenv("JELLYFIN_API_KEY", "")

EXCLUDED_PATHS = [
    "/mnt/gdrive/Media/Family Register/Season 1",
    "/mnt/gdrive/Media/Person of Interest",
    "/mnt/gdrive/Media/Silo"
]


def check_file_accessible(path: str) -> bool:
    """Checks if a file exists on FUSE even if xattr raises EIO."""
    if not path:
        return False

    try:
        # Check raw directory entry access via low-level file descriptor
        fd = os.open(path, os.O_RDONLY)
        os.close(fd)
        return True
    except OSError:
        pass

    try:
        # Fallback stream read
        with open(path, "rb") as f:
            f.read(1)
        return True
    except Exception:
        return False


def is_path_excluded(file_path: str) -> bool:
    norm_path = os.path.abspath(file_path)

    for excluded in EXCLUDED_PATHS:
        norm_excluded = os.path.abspath(excluded)

        if (
            norm_path == norm_excluded
            or norm_path.startswith(norm_excluded + os.sep)
        ):
            return True

    return False


def get_path_from_jellyfin(item_id: str) -> str:
    if not API_KEY:
        print(
            "[API ERROR] No JELLYFIN_API_KEY set in environment.",
            flush=True
        )
        return ""

    try:
        url = f"{JELLYFIN_URL}/Items?Ids={item_id}&Fields=Path"

        req = urllib.request.Request(
            url,
            headers={
                "X-Emby-Token": API_KEY,
                "Accept": "application/json"
            }
        )

        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            items = data.get("Items", [])

            if items and "Path" in items[0]:
                return items[0]["Path"]

    except Exception as e:
        print(
            f"[API ERROR] Failed to fetch path for ItemId {item_id}: {e}",
            flush=True
        )

    return ""


def sync_subtitles_for_video(video_path: str):
    if is_path_excluded(video_path):
        print(
            f"[SKIP] Path is in exclusion list: {video_path}",
            flush=True
        )
        return

    if not check_file_accessible(video_path):
        print(
            f"[WARN] File path is not accessible: {video_path}",
            flush=True
        )
        return

    print(
        f"[TASK START] Processing video: {video_path}",
        flush=True
    )

    base_name = os.path.splitext(video_path)[0]

    # Find all SRT files associated with the video.
    srt_candidates = glob.glob(
        f"{glob.escape(base_name)}*.srt"
    )

    # Ignore:
    #   *.tmp
    #   *.synced.srt
    #
    # We never want to use an already-synced subtitle as ffsubsync input.
    srt_candidates = [
        s
        for s in srt_candidates
        if not s.endswith(".tmp")
        and not s.endswith(".synced.srt")
    ]

    if not srt_candidates:
        print(
            f"[INFO] No original .srt files found for: {base_name}",
            flush=True
        )
        return

    for srt in srt_candidates:

        # New Jellyfin-compatible output filename.
        #
        # Example:
        #   Movie.srt
        #
        # becomes:
        #   Movie.synced.srt
        synced_srt = f"{os.path.splitext(srt)[0]}.synced.srt"

        # Old marker filename used by previous versions.
        #
        # Example:
        #   Movie.srt.synced
        #
        # We DON'T create this anymore, but if one already exists,
        # skip the subtitle so old jobs don't get re-synced.
        old_marker = f"{srt}.synced"

        # ---------------------------------------------------------
        # SKIP CHECK 1:
        # New synced subtitle already exists.
        # ---------------------------------------------------------
        if os.path.exists(synced_srt):
            print(
                f"[SKIP] Synced subtitle already exists: {synced_srt}",
                flush=True
            )
            continue

        # ---------------------------------------------------------
        # SKIP CHECK 2:
        # Old .synced marker exists.
        #
        # We no longer CREATE this marker, but we still recognize
        # existing ones so subtitles processed by the old version
        # aren't processed again.
        # ---------------------------------------------------------
        if os.path.exists(old_marker):
            print(
                f"[SKIP] Old .synced marker exists: {old_marker}",
                flush=True
            )
            continue

        unique_id = uuid.uuid4().hex[:8]

        local_input = f"/tmp/input_{unique_id}.srt"
        local_output = f"/tmp/synced_{unique_id}.srt"

        try:
            # Copy original subtitle into /tmp before running ffsubsync.
            shutil.copyfile(
                srt,
                local_input
            )

            print(
                f"[RUNNING] ffsubsync on: {srt} "
                f"(ID: {unique_id})",
                flush=True
            )

            cmd = [
                "ffsubsync",
                video_path,
                "-i",
                local_input,
                "-o",
                local_output,
                "--parallel-workers",
                "6"
            ]

            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True
            )

            if res.stdout.strip():
                print(
                    f"[FFSUBSYNC STDOUT]\n{res.stdout.strip()}",
                    flush=True
                )

            if res.stderr.strip():
                print(
                    f"[FFSUBSYNC STDERR]\n{res.stderr.strip()}",
                    flush=True
                )

            # ---------------------------------------------------------
            # Verify ffsubsync succeeded and produced a valid file.
            # ---------------------------------------------------------
            if (
                res.returncode == 0
                and os.path.exists(local_output)
                and os.path.getsize(local_output) > 0
            ):

                # Create the final Jellyfin-compatible subtitle.
                #
                # IMPORTANT:
                # We create the new file BEFORE deleting the original.
                shutil.copyfile(
                    local_output,
                    synced_srt
                )

                # Verify the final file.
                if (
                    not os.path.exists(synced_srt)
                    or os.path.getsize(synced_srt) == 0
                ):
                    print(
                        f"[ERROR] Failed to create valid synced subtitle: "
                        f"{synced_srt}",
                        flush=True
                    )

                    if os.path.exists(synced_srt):
                        try:
                            os.remove(synced_srt)
                        except OSError:
                            pass

                    continue

                print(
                    f"[SUCCESS] Created synced subtitle: {synced_srt}",
                    flush=True
                )

                # -----------------------------------------------------
                # Only NOW delete the original subtitle.
                # -----------------------------------------------------
                try:
                    os.remove(srt)

                    print(
                        f"[REMOVED] Original subtitle deleted: {srt}",
                        flush=True
                    )

                except OSError as e:
                    print(
                        f"[WARN] Could not delete original subtitle "
                        f"{srt}: {e}",
                        flush=True
                    )

                # -----------------------------------------------------
                # IMPORTANT:
                #
                # We DO NOT create:
                #
                #   Movie.srt.synced
                #
                # The existence of Movie.synced.srt is now our
                # primary skip mechanism.
                # -----------------------------------------------------

                print(
                    f"[SUCCESS] Finished syncing: {synced_srt}",
                    flush=True
                )

            else:
                print(
                    f"[ERROR] Sync did not produce a valid output for "
                    f"{srt} "
                    f"(return code: {res.returncode})",
                    flush=True
                )

        except Exception as e:
            print(
                f"[ERROR] Failed processing {srt}: {e}",
                flush=True
            )

        finally:
            # Always clean up temporary files.
            for p in (local_input, local_output):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass


@app.post("/webhook")
async def jellyfin_webhook(
    request: Request,
    background_tasks: BackgroundTasks
):
    try:
        body = await request.body()
        raw_text = body.decode("utf-8")

        data = (
            json.loads(raw_text)
            if raw_text.strip()
            else {}
        )

    except Exception as e:
        print(
            f"[PARSE ERROR] {e}",
            flush=True
        )
        data = {}

    video_path = data.get("Path") or data.get("ItemPath")
    item_id = data.get("ItemId") or data.get("Id")

    # 1. If path is empty, resolve via Jellyfin ItemId API.
    if (not video_path or video_path == "") and item_id:
        print(
            f"[LOOKUP] Fetching path for ItemId: {item_id}",
            flush=True
        )

        video_path = get_path_from_jellyfin(item_id)

    # 2. Check accessibility using raw file descriptor.
    if video_path and check_file_accessible(video_path):

        if is_path_excluded(video_path):
            print(
                f"[SKIP] Ignored webhook event for excluded path: "
                f"{video_path}",
                flush=True
            )

            return {
                "status": "ignored",
                "reason": "path excluded"
            }

        print(
            f"[QUEUED] Sync job for: {video_path}",
            flush=True
        )

        background_tasks.add_task(
            sync_subtitles_for_video,
            video_path
        )

        return {
            "status": "queued",
            "path": video_path
        }

    print(
        f"[IGNORED] Could not resolve a valid path on disk. "
        f"Resolved path: '{video_path}'",
        flush=True
    )

    return {
        "status": "ignored"
    }
