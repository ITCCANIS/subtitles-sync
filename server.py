import os
import glob
import subprocess
from fastapi import FastAPI, BackgroundTasks, Request

app = FastAPI()

def sync_subtitles_for_video(video_path: str):
    if not os.path.exists(video_path):
        print(f"[WARN] File path does not exist: {video_path}")
        return

    base_name = os.path.splitext(video_path)[0]
    srt_candidates = glob.glob(f"{glob.escape(base_name)}*.srt")

    # Filter out temp files
    srt_candidates = [s for s in srt_candidates if not s.endswith(".tmp")]

    if not srt_candidates:
        return

    for srt in srt_candidates:
        # A lightweight marker file next to the subtitle prevents re-syncing on every play
        marker = f"{srt}.synced"
        if os.path.exists(marker):
            continue

        temp_synced = f"{srt}.tmp"
        print(f"[INFO] Synchronizing: {srt} against {video_path}")

        cmd = [
            "ffsubsync",
            video_path,
            "-i", srt,
            "-o", temp_synced,
            "--overwrite-input"
        ]

        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            os.replace(temp_synced, srt)
            # Create a 0-byte marker file to indicate this subtitle has been timed
            with open(marker, "w") as f:
                f.write("")
            print(f"[SUCCESS] Retimed and marked: {srt}")
        else:
            if os.path.exists(temp_synced):
                os.remove(temp_synced)
            print(f"[ERROR] Sync failed for {srt}:\n{res.stderr}")

@app.post("/webhook")
async def jellyfin_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    
    # Jellyfin's Playback Start payload maps path directly or inside an Item object
    video_path = data.get("ItemPath") or data.get("Path")
    
    if not video_path and "Item" in data:
        video_path = data["Item"].get("Path")

    if video_path:
        print(f"[EVENT] Received playback event for: {video_path}")
        background_tasks.add_task(sync_subtitles_for_video, video_path)
        return {"status": "queued", "path": video_path}

    return {"status": "ignored", "reason": "no valid path in payload"}
