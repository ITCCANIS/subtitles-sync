"""Jellyfin webhook receiver that syncs external subtitles with ffsubsync.

Endpoints:
  POST /webhook   Jellyfin webhook (Playback Start). Body needs Path/ItemPath
                  or ItemId/Id.
  POST /sync      Manual run: {"path": "/mnt/.../video.mkv", "force": true}
  GET  /health    Queue status.
"""
import json
import logging
import queue
import threading

from fastapi import FastAPI, Request

from sync_core import (
    Config,
    check_file_accessible,
    get_path_from_jellyfin,
    is_path_excluded,
    sync_subtitles_for_video,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("subsync")

cfg = Config()
app = FastAPI()

# One job at a time: ffsubsync is CPU heavy and every job reads a whole
# video over the network. Playback Start often fires several times for the
# same item (track changes, transcode restarts), so duplicates are dropped.
_jobs: "queue.Queue[tuple]" = queue.Queue()
_pending: set = set()
_lock = threading.Lock()
_current = {"path": None}


def _worker():
    while True:
        path, item_id, force = _jobs.get()
        _current["path"] = path
        try:
            sync_subtitles_for_video(path, cfg, item_id=item_id, force=force)
        except Exception:
            log.exception("Job crashed for %s", path)
        finally:
            with _lock:
                _pending.discard(path)
            _current["path"] = None
            _jobs.task_done()


threading.Thread(target=_worker, daemon=True, name="subsync-worker").start()


def enqueue(path: str, item_id: str = "", force: bool = False) -> bool:
    with _lock:
        if path in _pending:
            return False
        _pending.add(path)
    _jobs.put((path, item_id, force))
    return True


async def _read_json(request: Request) -> dict:
    try:
        raw = (await request.body()).decode("utf-8")
        return json.loads(raw) if raw.strip() else {}
    except Exception as e:
        log.warning("Could not parse request body: %s", e)
        return {}


def _resolve_and_queue(data: dict, force: bool = False) -> dict:
    video_path = data.get("Path") or data.get("ItemPath") or data.get("path") or ""
    item_id = data.get("ItemId") or data.get("Id") or data.get("item_id") or ""

    if not video_path and item_id:
        log.info("Looking up path for ItemId %s", item_id)
        video_path = get_path_from_jellyfin(item_id, cfg)

    if not video_path or not check_file_accessible(video_path):
        log.info("Ignored: no readable file (resolved path: %r)", video_path)
        return {"status": "ignored", "reason": "path not found"}

    if is_path_excluded(video_path, cfg.excluded_paths):
        log.info("Ignored excluded path: %s", video_path)
        return {"status": "ignored", "reason": "path excluded"}

    if not enqueue(video_path, item_id, force):
        return {"status": "already queued", "path": video_path}

    log.info("Queued: %s", video_path)
    return {"status": "queued", "path": video_path}


@app.post("/webhook")
async def jellyfin_webhook(request: Request):
    return _resolve_and_queue(await _read_json(request))


@app.post("/sync")
async def manual_sync(request: Request):
    data = await _read_json(request)
    return _resolve_and_queue(data, force=bool(data.get("force")))


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "queued": _jobs.qsize(),
        "processing": _current["path"],
        "split_penalty": cfg.split_penalty or "off",
    }
