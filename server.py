import os
import glob
import shutil
import subprocess
import json
import urllib.request
from fastapi import FastAPI, BackgroundTasks, Request

app = FastAPI()

JELLYFIN_URL = "http://127.0.0.1:8096"
API_KEY = os.getenv("JELLYFIN_API_KEY", "")

def get_path_from_jellyfin(item_id: str) -> str:
    if not API_KEY:
        print("[API ERROR] No JELLYFIN_API_KEY set in environment.", flush=True)
        return ""

    try:
        url = f"{JELLYFIN_URL}/Items?Ids={item_id}&Fields=Path"
        req = urllib.request.Request(url, headers={
            "X-Emby-Token": API_KEY,
            "Accept": "application/json"
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            items = data.get("Items", [])
            if items and "Path" in items[0]:
                return items[0]["Path"]
    except Exception as e:
        print(f"[API ERROR] Failed to fetch path for ItemId {item_id}: {e}", flush=True)
    return ""

def sync_subtitles_for_video(video_path: str):
    print(f"[TASK START] Processing video: {video_path}", flush=True)

    if not os.path.exists(video_path):
        print(f"[WARN] File path does not exist on disk: {video_path}", flush=True)
        return

    base_name = os.path.splitext(video_path)[0]
    srt_candidates = glob.glob(f"{glob.escape(base_name)}*.srt")
    srt_candidates = [s for s in srt_candidates if not s.endswith(".tmp")]

    if not srt_candidates:
        print(f"[INFO] No .srt files found for: {base_name}", flush=True)
        return

    for srt in srt_candidates:
        marker = f"{srt}.synced"
        if os.path.exists(marker):
            print(f"[SKIP] Already synced: {srt}", flush=True)
            continue

        local_input = "/tmp/input_sub.srt"
        local_output = "/tmp/synced_sub.srt"

        # Clean previous local runs
        for path in (local_input, local_output):
            if os.path.exists(path):
                os.remove(path)

        # Copy remote srt using raw bytes to bypass FUSE xattr issues
        shutil.copyfile(srt, local_input)

        print(f"[RUNNING] ffsubsync on: {srt}", flush=True)

        cmd = [
            "ffsubsync",
            video_path,
            "-i", local_input,
            "-o", local_output
        ]

        # Execute ffsubsync and capture all output
        res = subprocess.run(cmd, capture_output=True, text=True)

        print(f"[FFSUBSYNC STDOUT]\n{res.stdout.strip()}", flush=True)
        if res.stderr.strip():
            print(f"[FFSUBSYNC STDERR]\n{res.stderr.strip()}", flush=True)

        # Verify output file was created and is non-empty
        if os.path.exists(local_output) and os.path.getsize(local_output) > 0:
            # Copy back to rclone without metadata attributes
            shutil.copyfile(local_output, srt)
            with open(marker, "w") as f:
                f.write("")
            print(f"[SUCCESS] Retimed and marked: {marker}", flush=True)
        else:
            print(f"[ERROR] Sync did not produce a valid output for {srt} (return code: {res.returncode})", flush=True)

        # Clean up local scratch files
        for path in (local_input, local_output):
            if os.path.exists(path):
                os.remove(path)

@app.post("/webhook")
async def jellyfin_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.body()
        raw_text = body.decode("utf-8")
        data = json.loads(raw_text) if raw_text.strip() else {}
    except Exception as e:
        print(f"[PARSE ERROR] {e}", flush=True)
        data = {}

    video_path = data.get("Path") or data.get("ItemPath")
    item_id = data.get("ItemId") or data.get("Id")

    if (not video_path or video_path == "") and item_id:
        print(f"[LOOKUP] Fetching path for ItemId: {item_id}", flush=True)
        video_path = get_path_from_jellyfin(item_id)

    if video_path and os.path.exists(video_path):
        print(f"[QUEUED] Sync job for: {video_path}", flush=True)
        background_tasks.add_task(sync_subtitles_for_video, video_path)
        return {"status": "queued", "path": video_path}

    print(f"[IGNORED] Could not resolve a valid path on disk. Resolved path: '{video_path}'", flush=True)
    return {"status": "ignored"}
