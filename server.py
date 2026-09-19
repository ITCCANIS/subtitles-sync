import os
import glob
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
        # Query the item directly
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

        temp_synced = f"{srt}.tmp"
        print(f"[RUNNING] ffsubsync on: {srt}", flush=True)

        cmd = [
            "ffsubsync",
            video_path,
            "-i", srt,
            "-o", temp_synced
        ]

        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            os.replace(temp_synced, srt)
            with open(marker, "w") as f:
                f.write("")
            print(f"[SUCCESS] Retimed and marked: {marker}", flush=True)
        else:
            if os.path.exists(temp_synced):
                os.remove(temp_synced)
            print(f"[ERROR] Sync failed for {srt}:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}", flush=True)

@app.post("/webhook")
async def jellyfin_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.body()
        raw_text = body.decode("utf-8")
        print(f"[RAW INCOMING] {raw_text}", flush=True)
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
