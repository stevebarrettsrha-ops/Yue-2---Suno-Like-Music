"""
server.py - YuE Studio backend.

Run:  python server.py            (opens http://127.0.0.1:7788)
"""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

import bootstrap
import manager
from bootstrap import (APP_DIR, ComfyProcess, Progress, comfy_online,
                       detect_comfy_dirs, load_config, save_config)
from comfy import ComfyClient, ComfyError

DATA_DIR = APP_DIR / "data"
TRACKS_DIR = DATA_DIR / "tracks"
LIBRARY_PATH = DATA_DIR / "library.json"
WEB_DIR = APP_DIR / "web"
PORT = int(os.environ.get("YUE_STUDIO_PORT", "7788"))

app = Flask(__name__, static_folder=None)

cfg = load_config()
progress = Progress()
comfy_proc = ComfyProcess()
client = ComfyClient(cfg["comfy_url"])

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
library_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# library
# --------------------------------------------------------------------------- #
def read_library() -> list[dict]:
    with library_lock:
        if not LIBRARY_PATH.exists():
            return []
        try:
            return json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []


def write_library(items: list[dict]) -> None:
    with library_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LIBRARY_PATH.write_text(json.dumps(items, indent=2), encoding="utf-8")


def add_track(track: dict) -> None:
    items = read_library()
    items.insert(0, track)
    write_library(items)


def title_from(style: str, lyrics: str) -> str:
    for line in (lyrics or "").splitlines():
        line = line.strip()
        if line and not line.startswith("["):
            words = line.split()
            return " ".join(words[:6]).strip(" ,.!?-").title() or "Untitled"
    words = (style or "Untitled").split(",")[0].split()
    return " ".join(words[:5]).title() or "Untitled"


# --------------------------------------------------------------------------- #
# progress over the ComfyUI websocket (optional dependency)
# --------------------------------------------------------------------------- #
ws_progress: dict[str, dict] = {}


def ws_listener() -> None:
    try:
        import websocket  # websocket-client
    except ImportError:
        return
    while True:
        try:
            url = cfg["comfy_url"].replace("http://", "ws://").replace(
                "https://", "wss://")
            ws = websocket.WebSocket()
            ws.connect(f"{url}/ws?clientId={client.client_id}", timeout=10)
            current = None
            while True:
                raw = ws.recv()
                if not isinstance(raw, str):
                    continue
                msg = json.loads(raw)
                mtype, data = msg.get("type"), msg.get("data") or {}
                pid = data.get("prompt_id") or current
                if mtype == "execution_start":
                    current = data.get("prompt_id")
                elif mtype == "executing" and pid:
                    ws_progress.setdefault(pid, {})["node"] = data.get("node")
                elif mtype == "progress" and pid:
                    ws_progress.setdefault(pid, {}).update(
                        value=data.get("value", 0), max=data.get("max", 0))
                elif mtype in ("execution_success", "execution_error") and pid:
                    ws_progress.pop(pid, None)
        except Exception:
            time.sleep(4)


# --------------------------------------------------------------------------- #
# generation job
# --------------------------------------------------------------------------- #
def run_job(job_id: str, params: dict) -> None:
    def set_state(**kw):
        with jobs_lock:
            jobs[job_id].update(kw)

    try:
        set_state(stage="Building the graph", pct=2)
        built = client.build_prompt(params)
        set_state(stage="Queued in ComfyUI", pct=4, seed=built["seed"])
        prompt_id = client.queue(built["prompt"])
        set_state(prompt_id=prompt_id, stage="Writing the melody plan", pct=6)

        started = time.time()
        last_stage = ""
        while True:
            time.sleep(1.5)
            with jobs_lock:
                if jobs[job_id].get("cancelled"):
                    client.interrupt()
                    set_state(status="cancelled", stage="Cancelled")
                    return

            err = client.failed(prompt_id)
            if err:
                set_state(status="error", error=err, stage="Failed")
                return

            outs = client.outputs(prompt_id)
            if outs:
                break

            wp = ws_progress.get(prompt_id) or {}
            value, maximum = wp.get("value", 0), wp.get("max", 0)
            if maximum:
                # KSampler steps dominate the visible progress band 15-90%.
                pct = 15 + min(value / maximum, 1.0) * 75
                stage = "Rendering audio"
            else:
                elapsed = time.time() - started
                pct = min(6 + elapsed / 6.0, 14)
                stage = "Writing the melody plan"
            if stage != last_stage:
                last_stage = stage
            set_state(pct=round(pct, 1), stage=stage,
                      elapsed=round(time.time() - started))

            if time.time() - started > 3600:
                set_state(status="error", stage="Timed out",
                          error="No audio after an hour. Check the ComfyUI log.")
                return

        set_state(stage="Saving the track", pct=94)
        item = outs[0]
        track_id = uuid.uuid4().hex[:12]
        ext = Path(item["filename"]).suffix or ".flac"
        TRACKS_DIR.mkdir(parents=True, exist_ok=True)
        dest = TRACKS_DIR / f"{track_id}{ext}"
        with client.view(item) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(1024 * 256):
                    fh.write(chunk)

        track = {
            "id": track_id,
            "title": params.get("title") or title_from(params.get("style", ""),
                                                       params.get("lyrics", "")),
            "style": params.get("style", ""),
            "lyrics": params.get("lyrics", ""),
            "instrumental": bool(params.get("instrumental")),
            "seed": built["seed"],
            "duration": params.get("duration"),
            "mode": params.get("mode"),
            "steps": params.get("steps"),
            "cfg": params.get("cfg"),
            "sampler": params.get("sampler"),
            "checkpoint": built["ckpt"],
            "cover": bool(params.get("reference_audio")),
            "file": dest.name,
            "created": time.time(),
        }
        add_track(track)
        set_state(status="done", pct=100, stage="Ready", track=track)
    except ComfyError as exc:
        set_state(status="error", error=str(exc), stage="Failed")
    except Exception as exc:  # noqa: BLE001
        set_state(status="error", error=f"{type(exc).__name__}: {exc}",
                  stage="Failed")


# --------------------------------------------------------------------------- #
# routes - app shell
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/web/<path:name>")
def web_asset(name: str):
    return send_from_directory(WEB_DIR, name)


# --------------------------------------------------------------------------- #
# routes - setup
# --------------------------------------------------------------------------- #
@app.get("/api/status")
def api_status():
    online = comfy_online(cfg["comfy_url"])
    models_dir = Path(cfg["models_dir"]) if cfg.get("models_dir") else None
    missing = []
    if models_dir and models_dir.is_dir():
        missing = [Path(m[0]).name for m in bootstrap.missing_models(models_dir, cfg)]
    ready = bool(cfg.get("setup_complete")) and online and not missing
    payload = {
        "ready": ready,
        "comfy_online": online,
        "setup_complete": bool(cfg.get("setup_complete")),
        "missing_models": missing,
        "config": {k: cfg.get(k) for k in
                   ("comfy_url", "comfy_dir", "models_dir", "managed",
                    "auto_start_comfy", "download_cover_model", "download_bf16",
                    "torch_index")},
        "detected": detect_comfy_dirs(),
        "comfy_running_managed": comfy_proc.alive(),
    }
    if online:
        try:
            payload["checkpoints"] = client.checkpoints()
            payload["has_cover_model"] = bool(
                [e for e in client.audio_encoders() if "sheetsage" in e.lower()])
            sampler = client.node_inputs("KSampler")
            payload["samplers"] = list(sampler["sampler_name"][0])
            payload["schedulers"] = list(sampler["scheduler"][0])
            fmt = client.node_inputs("SaveAudioAdvanced").get("format")
            payload["formats"] = list(fmt[0]) if fmt and isinstance(fmt[0], list) \
                else ["flac"]
        except Exception as exc:  # ComfyUI up but too old / still loading
            payload["schema_error"] = str(exc)
    return jsonify(payload)


@app.post("/api/setup/start")
def api_setup_start():
    if progress.running:
        return jsonify({"ok": False, "error": "Setup is already running."}), 409
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "auto")
    chosen = body.get("comfy_dir", "")
    for key in ("comfy_url", "models_dir", "torch_index",
                "download_cover_model", "download_bf16", "auto_start_comfy"):
        if key in body:
            cfg[key] = body[key]
    client.url = cfg["comfy_url"].rstrip("/")
    save_config(cfg)
    progress.__init__()  # reset log and step states
    threading.Thread(target=bootstrap.run_setup,
                     args=(cfg, progress, comfy_proc, chosen, mode),
                     daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/setup/state")
def api_setup_state():
    since = int(request.args.get("since", 0))
    snap = progress.snapshot(since)
    snap["comfy_tail"] = comfy_proc.tail(12)
    return jsonify(snap)


@app.post("/api/comfy/start")
def api_comfy_start():
    if comfy_online(cfg["comfy_url"]):
        return jsonify({"ok": True, "already": True})
    if not cfg.get("comfy_dir") or not cfg.get("python"):
        return jsonify({"ok": False,
                        "error": "Run setup first."}), 400
    port = int(cfg["comfy_url"].rsplit(":", 1)[-1])
    comfy_proc.start(cfg["python"], Path(cfg["comfy_dir"]), port, progress)
    return jsonify({"ok": True})


@app.post("/api/config")
def api_config():
    body = request.get_json(silent=True) or {}
    for key in ("comfy_url", "comfy_dir", "models_dir", "auto_start_comfy",
                "download_cover_model", "download_bf16", "torch_index"):
        if key in body:
            cfg[key] = body[key]
    client.url = cfg["comfy_url"].rstrip("/")
    save_config(cfg)
    return jsonify({"ok": True, "config": cfg})


# --------------------------------------------------------------------------- #
# routes - generation
# --------------------------------------------------------------------------- #
@app.post("/api/generate")
def api_generate():
    params = request.get_json(silent=True) or {}
    if not (params.get("style") or "").strip():
        return jsonify({"error": "Add a style description before generating."}), 400
    if not comfy_online(cfg["comfy_url"]):
        return jsonify({"error": "ComfyUI is not running. Start it from "
                                 "Settings."}), 503

    count = max(1, min(int(params.get("count") or 1), 4))
    created = []
    for _ in range(count):
        job_id = uuid.uuid4().hex[:12]
        with jobs_lock:
            jobs[job_id] = {"id": job_id, "status": "running", "pct": 0,
                            "stage": "Starting", "created": time.time(),
                            "title": params.get("title") or
                            title_from(params.get("style", ""),
                                       params.get("lyrics", "")),
                            "style": params.get("style", "")}
        threading.Thread(target=run_job, args=(job_id, dict(params)),
                         daemon=True).start()
        created.append(job_id)
        time.sleep(0.2)  # keep queue order stable
    return jsonify({"jobs": created})


@app.get("/api/jobs")
def api_jobs():
    with jobs_lock:
        active = [j for j in jobs.values()
                  if j["status"] == "running" or time.time() - j["created"] < 120]
        return jsonify(sorted(active, key=lambda j: j["created"], reverse=True))


@app.post("/api/jobs/<job_id>/cancel")
def api_cancel(job_id: str):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["cancelled"] = True
    return jsonify({"ok": True})


@app.post("/api/upload-reference")
def api_upload_reference():
    if "file" not in request.files:
        return jsonify({"error": "No file received."}), 400
    try:
        name = client.upload_audio(request.files["file"])
        return jsonify({"ok": True, "name": name})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


# --------------------------------------------------------------------------- #
# routes - library
# --------------------------------------------------------------------------- #
@app.get("/api/library")
def api_library():
    return jsonify(read_library())


@app.get("/api/track/<track_id>")
def api_track(track_id: str):
    for item in read_library():
        if item["id"] == track_id:
            path = TRACKS_DIR / item["file"]
            if not path.exists():
                return jsonify({"error": "Audio file is missing."}), 404
            mime = mimetypes.guess_type(path.name)[0] or "audio/flac"
            return send_file(path, mimetype=mime, conditional=True,
                             download_name=f"{item['title']}{path.suffix}")
    return jsonify({"error": "Track not found."}), 404


@app.patch("/api/track/<track_id>")
def api_rename(track_id: str):
    body = request.get_json(silent=True) or {}
    items = read_library()
    for item in items:
        if item["id"] == track_id:
            if body.get("title"):
                item["title"] = body["title"][:120]
            write_library(items)
            return jsonify({"ok": True, "track": item})
    return jsonify({"error": "Track not found."}), 404


@app.delete("/api/track/<track_id>")
def api_delete(track_id: str):
    items = read_library()
    keep = [i for i in items if i["id"] != track_id]
    gone = [i for i in items if i["id"] == track_id]
    for item in gone:
        try:
            (TRACKS_DIR / item["file"]).unlink(missing_ok=True)
        except OSError:
            pass
    write_library(keep)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# routes - dependencies
# --------------------------------------------------------------------------- #
@app.get("/api/deps")
def api_deps():
    return jsonify({"items": manager.dependencies(cfg),
                    "os": __import__("platform").system(),
                    "torch_index": cfg.get("torch_index", "")})


@app.post("/api/deps/<dep_id>/install")
def api_dep_install(dep_id: str):
    body = request.get_json(silent=True) or {}
    if body.get("torch_index") is not None:
        cfg["torch_index"] = body["torch_index"]
        save_config(cfg)
    try:
        task = manager.install_dependency(dep_id, cfg, body)
        return jsonify({"ok": True, "task": task.view()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400


@app.get("/api/tasks")
def api_tasks():
    since = int(request.args.get("since", 0))
    task_id = request.args.get("id", "")
    if task_id:
        task = manager.TASKS.get(task_id)
        if not task:
            return jsonify({"error": "No such task."}), 404
        return jsonify(task.view(since))
    return jsonify([t.view(t.view()["cursor"]) for t in manager.TASKS.list()[:25]])


@app.post("/api/tasks/<task_id>/cancel")
def api_task_cancel(task_id: str):
    task = manager.TASKS.get(task_id)
    if task:
        task.cancel = True
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# routes - huggingface
# --------------------------------------------------------------------------- #
@app.get("/api/hf/settings")
def api_hf_settings():
    token = cfg.get("hf_token") or ""
    return jsonify({
        "endpoint": cfg.get("hf_endpoint") or manager.DEFAULT_ENDPOINT,
        "token_set": bool(token),
        "token_hint": ("…" + token[-4:]) if len(token) > 4 else "",
        "repo": cfg.get("hf_repo") or manager.DEFAULT_REPO,
        "folders": manager.MODEL_FOLDERS,
        "curated": manager.CURATED,
        "models_dir": cfg.get("models_dir", ""),
    })


@app.post("/api/hf/settings")
def api_hf_settings_save():
    body = request.get_json(silent=True) or {}
    if "token" in body:
        cfg["hf_token"] = (body["token"] or "").strip()
    if body.get("endpoint") is not None:
        cfg["hf_endpoint"] = body["endpoint"].strip() or manager.DEFAULT_ENDPOINT
    if body.get("repo"):
        cfg["hf_repo"] = body["repo"].strip()
    if body.get("models_dir"):
        cfg["models_dir"] = body["models_dir"].strip()
    save_config(cfg)
    return jsonify({"ok": True})


@app.get("/api/hf/browse")
def api_hf_browse():
    repo = (request.args.get("repo") or cfg.get("hf_repo")
            or manager.DEFAULT_REPO).strip()
    revision = request.args.get("revision") or "main"
    try:
        data = manager.hf_list(cfg, repo, revision)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400
    have = {(m["folder"], m["name"]) for m in manager.local_models(cfg)}
    for f in data["files"]:
        folder = manager.guess_folder(f["path"])
        f["folder"] = folder
        f["installed"] = (folder, Path(f["path"]).name) in have
    cfg["hf_repo"] = repo
    save_config(cfg)
    return jsonify(data)


@app.post("/api/hf/download")
def api_hf_download():
    body = request.get_json(silent=True) or {}
    repo = (body.get("repo") or cfg.get("hf_repo") or manager.DEFAULT_REPO).strip()
    path = (body.get("path") or "").strip()
    if not path:
        return jsonify({"error": "Pick a file to download."}), 400
    try:
        task = manager.hf_download(cfg, repo, path,
                                   body.get("folder") or "",
                                   body.get("revision") or "main")
        return jsonify({"ok": True, "task": task.view()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400


@app.get("/api/hf/local")
def api_hf_local():
    return jsonify({"models": manager.local_models(cfg),
                    "models_dir": cfg.get("models_dir", "")})


@app.delete("/api/hf/local")
def api_hf_delete():
    body = request.get_json(silent=True) or {}
    try:
        manager.delete_model(cfg, body.get("folder", ""), body.get("name", ""))
        return jsonify({"ok": True})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400


# --------------------------------------------------------------------------- #
def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TRACKS_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=ws_listener, daemon=True).start()

    if cfg.get("setup_complete") and cfg.get("auto_start_comfy", True) \
            and cfg.get("comfy_dir") and cfg.get("python") \
            and not comfy_online(cfg["comfy_url"]):
        port = int(cfg["comfy_url"].rsplit(":", 1)[-1])
        progress.log("Restarting ComfyUI from the last setup…")
        comfy_proc.start(cfg["python"], Path(cfg["comfy_dir"]), port, progress)

    url = f"http://127.0.0.1:{PORT}"
    print(f"\n  YuE Studio  →  {url}\n")
    if os.environ.get("YUE_STUDIO_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False)
    finally:
        comfy_proc.stop()


if __name__ == "__main__":
    main()
