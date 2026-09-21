"""
server.py - YuE Studio backend.

Run:  python server.py            (opens http://127.0.0.1:7788)
"""

from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

import requests
from flask import Flask, jsonify, request, send_file, send_from_directory

import bootstrap
import manager
from bootstrap import (ComfyProcess, Progress, clean_url,
                       comfy_online, comfy_port, detect_comfy_dirs,
                       load_config, save_config)
from comfy import ComfyClient, ComfyError

DATA_DIR = bootstrap.DATA_DIR          # honours YUE_STUDIO_DATA
TRACKS_DIR = DATA_DIR / "tracks"
LIBRARY_PATH = DATA_DIR / "library.json"
WEB_DIR = bootstrap.APP_DIR / "web"
PORT = int(os.environ.get("YUE_STUDIO_PORT", "7788"))

app = Flask(__name__, static_folder=None)

cfg = load_config()
progress = Progress()
comfy_proc = ComfyProcess()
client = ComfyClient(cfg["comfy_url"])

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
# Re-entrant: every change to the library is a read, an edit and a write, and
# all three have to happen without another thread slipping in between. A song
# finishing while a track is being deleted otherwise writes back a list that
# never knew about the other one, and whichever wrote first is simply gone.
library_lock = threading.RLock()


# --------------------------------------------------------------------------- #
# request input
#
# Every value below arrives as JSON from outside this process. The front end
# always sends the right shapes, but nothing enforces that, and a value of the
# wrong type used to reach str.strip() or int() and end the request in a 500 —
# or, worse, be written into the live config on its way to failing.
# --------------------------------------------------------------------------- #
def as_text(value, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


def as_int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(number, high))


def as_float(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if number != number:                      # NaN compares unequal to itself
        return default
    return max(low, min(number, high))


def as_cursor(raw: str) -> int:
    """A log cursor from a query string, however mangled."""
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def json_object() -> dict:
    """Return request JSON when it is an object, otherwise an empty object.

    Valid JSON can also be an array, string, number, or null.  Those values
    used to reach the first ``.get()`` in a write route and turn malformed
    input into a 500 response.  Keeping the shape check at the HTTP boundary
    lets every endpoint handle such input consistently.
    """
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def as_bool(value, default: bool = False) -> bool:
    """Read a JSON boolean without treating strings such as "false" as true."""
    return value if isinstance(value, bool) else default


# Bounds are the YuE2 and KSampler node limits, so a silly number is clamped
# here instead of being rejected by ComfyUI after the job has already started.
def clean_generate(body: dict) -> dict:
    mode = as_text(body.get("mode"), "full")
    return {
        "style": as_text(body.get("style"))[:4000],
        "lyrics": as_text(body.get("lyrics"))[:20000],
        "title": as_text(body.get("title"))[:120],
        "abc": as_text(body.get("abc"))[:60000],
        "ckpt": as_text(body.get("ckpt"))[:260],
        "format": as_text(body.get("format"))[:20],
        "quality": as_text(body.get("quality"))[:20],
        "sampler": as_text(body.get("sampler"))[:60] or None,
        "scheduler": as_text(body.get("scheduler"))[:60] or None,
        "reference_audio": as_text(body.get("reference_audio"))[:260] or None,
        "mode": mode if mode in ("full", "melody") else "full",
        "cover_mode": (as_text(body.get("cover_mode"))
                       if as_text(body.get("cover_mode")) in ("melody", "full")
                       else "melody"),
        "instrumental": as_bool(body.get("instrumental")),
        "use_abc": as_bool(body.get("use_abc"), True),
        "tiled_decode": as_bool(body.get("tiled_decode"), True),
        "count": as_int(body.get("count"), 1, 1, 4),
        # The engine's own ceiling is applied when the graph is built; this is
        # only here to stop a nonsense number getting that far.
        "duration": as_int(body.get("duration"), 180, 1, 24 * 3600),
        "steps": as_int(body.get("steps"), 32, 1, 10000),
        "top_k": as_int(body.get("top_k"), 100, 1, 32768),
        "abc_tokens": as_int(body.get("abc_tokens"), 8192, 1, 20000),
        "tile_size": as_int(body.get("tile_size"), 1920, 32, 8192),
        "overlap": as_int(body.get("overlap"), 128, 0, 1024),
        "cfg": as_float(body.get("cfg"), 1.0, 0.0, 100.0),
        "top_p": as_float(body.get("top_p"), 0.95, 0.01, 1.0),
        "repetition_penalty": as_float(body.get("repetition_penalty"),
                                       1.2, 0.01, 10.0),
        # None means "pick a fresh one", which is not the same as seed 0.
        "seed": (None if body.get("seed") in (None, "")
                 else as_int(body.get("seed"), 0, 0, 2**63 - 1)),
    }


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


@app.before_request
def only_this_machine():
    """Answer only to this machine's own names.

    The server listens on loopback, but a browser sends a page's requests to
    whatever a hostname resolves to — so a domain an attacker controls, pointed
    at 127.0.0.1, reaches this server as same-origin and can read every answer.
    Checking the name the request was addressed to closes that off and costs
    nothing for a real local visit.
    """
    host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in LOCAL_HOSTS:
        return jsonify({"error": "YuE Studio only answers on this machine."}), 403


# --------------------------------------------------------------------------- #
# library
# --------------------------------------------------------------------------- #
def read_library() -> list[dict]:
    """The song list — always a list of records, whatever the file holds.

    library.json is plain text in a folder people can open: it can be
    hand-edited, restored from an older copy, or left half-written by a crash.
    A shape we did not expect must not stop songs being saved or served, so
    anything that is not a usable record is simply left out.
    """
    with library_lock:
        if not LIBRARY_PATH.exists():
            return []
        try:
            items = json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        return [i for i in items if isinstance(i, dict) and i.get("id")]


def track_file(item: dict, kind: str = "") -> Path | None:
    """Where a library entry's audio lives, or None if it names nothing sane.

    A song is kept twice: a wav to listen to and keep, and an mp3 to send
    someone. `kind` picks one of those; without it you get the wav, or
    whatever single file an older song was saved as.

    Only ever a plain filename inside data/tracks: the name comes out of a
    file anyone can edit, and it is used to both serve and delete.
    """
    name = Path(str(item.get(kind or "file") or "")).name
    return (TRACKS_DIR / name) if name else None


def track_files(item: dict) -> list[Path]:
    """Everything on disk that belongs to this song."""
    found = []
    for kind in ("file", "mp3"):
        path = track_file(item, kind)
        if path and path not in found:
            found.append(path)
    return found


# --------------------------------------------------------------------------- #
# what a finished song is saved as
#
# ComfyUI writes flac, mp3 and opus itself, so those are rendered straight to
# the format asked for. It has no wav encoder at all, so a wav is rendered
# lossless and converted here — the one format that needs ffmpeg.
# --------------------------------------------------------------------------- #
def convert_audio(src: Path, dest: Path, args: tuple = ()) -> bool:
    """Re-encode one file into another. False if ffmpeg cannot or is absent."""
    ffmpeg = bootstrap.find_tool("ffmpeg")
    if not ffmpeg or src == dest:
        return False
    try:
        done = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(src), *args,
             str(dest)], capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.SubprocessError):
        return False
    return (done.returncode == 0 and dest.is_file()
            and dest.stat().st_size > 0)


def available_formats(rendered: list[str]) -> list[str]:
    """What Save as can offer: what this ComfyUI writes, and wav if we can."""
    formats = [f for f in rendered if f]
    if bootstrap.find_tool("ffmpeg") and "wav" not in formats:
        formats.insert(1 if formats else 0, "wav")
    return formats


def render_format(wanted: str) -> str:
    """What to ask ComfyUI for, to end up with what was asked for."""
    return "flac" if wanted == "wav" else wanted


def save_as(master: Path, track_id: str, wanted: str) -> Path:
    """The file the song is kept as, converting only when we have to.

    Everything but wav comes out of ComfyUI already in the right format. A wav
    is made from the lossless render, and if there is no ffmpeg to make it
    with, the song stays as that render — it still plays and downloads, and
    the Engine page offers to install ffmpeg in one press.
    """
    if wanted != "wav" or master.suffix.lower() == ".wav":
        return master
    dest = TRACKS_DIR / f"{track_id}.wav"
    if not convert_audio(master, dest):
        return master
    try:
        master.unlink(missing_ok=True)      # the wav holds everything it did
    except OSError:
        pass
    return dest


def write_library(items: list[dict]) -> None:
    """Replace the library in one step.

    Written beside the real file and moved over it, so a crash or a full disk
    leaves the previous song list intact instead of a half-written one.
    """
    with library_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = LIBRARY_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
        tmp.replace(LIBRARY_PATH)


def add_track(track: dict) -> None:
    with library_lock:
        items = read_library()
        items.insert(0, track)
        write_library(items)


def audio_duration(path: Path) -> float | None:
    """How long the finished song actually is, in seconds.

    YuE2 stops when the song ends, so a track is usually shorter than the
    max_duration it was asked for — the requested value is a ceiling and must
    never be shown as the length. Read it out of the file instead.

    flac and ogg/opus are parsed here because they need no decoder; anything
    else falls back to ffprobe, and if that is missing the front end backfills
    the real duration the first time the track is played.
    """
    for reader in (_duration_wav, _duration_flac, _duration_opus,
                   _duration_ffprobe):
        try:
            seconds = reader(path)
        except (OSError, ValueError, IndexError, ZeroDivisionError,
                subprocess.SubprocessError, __import__("wave").Error):
            continue
        if seconds and seconds > 0:
            return round(seconds, 2)
    return None


def _duration_wav(path: Path) -> float | None:
    if path.suffix.lower() != ".wav":
        return None
    import wave
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        return handle.getnframes() / rate if rate else None


def _duration_flac(path: Path) -> float | None:
    if path.suffix.lower() != ".flac":
        return None
    with open(path, "rb") as fh:
        if fh.read(4) != b"fLaC":
            return None
        fh.seek(8)  # past the STREAMINFO block header and the block sizes
        head = fh.read(18)
    if len(head) < 18:
        return None
    # 20 bits sample rate, 3 channels, 5 bits per sample, 36 bits total samples
    packed = int.from_bytes(head[10:18], "big")
    rate, total = packed >> 44, packed & ((1 << 36) - 1)
    return total / rate if rate and total else None


def _duration_opus(path: Path) -> float | None:
    """Ogg Opus only.

    Opus fixes its granule clock at 48 kHz whatever the source rate. An .ogg
    holding Vorbis counts granules at the stream's own rate instead, so reading
    one as Opus under-reports every length — bail out unless OpusHead is there
    and let ffprobe handle the rest.
    """
    if path.suffix.lower() not in (".opus", ".ogg", ".oga"):
        return None
    with open(path, "rb") as fh:
        head = fh.read(4096)
        fh.seek(max(0, path.stat().st_size - 65536))
        tail = fh.read()
    start = head.find(b"OpusHead")
    if start < 0:
        return None
    pre_skip = int.from_bytes(head[start + 10:start + 12], "little")
    page = tail.rfind(b"OggS")
    if page < 0 or page + 14 > len(tail):
        return None
    granule = int.from_bytes(tail[page + 6:page + 14], "little")
    if granule in (0, 0xFFFFFFFFFFFFFFFF):
        return None
    return max(0, granule - pre_skip) / 48000.0


def _duration_ffprobe(path: Path) -> float | None:
    ffprobe = bootstrap.find_tool("ffprobe")
    if not ffprobe:
        return None
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=20,
    ).stdout.strip()
    return float(out) if out else None


def title_from(style: str, lyrics: str) -> str:
    for line in (lyrics or "").splitlines():
        line = line.strip()
        if line and not line.startswith("["):
            words = line.split()
            return " ".join(words[:6]).strip(" ,.!?-").title() or "Untitled"
    words = (style or "Untitled").split(",")[0].split()
    return " ".join(words[:5]).title() or "Untitled"


def track_title(params: dict) -> str:
    """What to call this song. An instrumental never sings its lyrics box, so
    naming it after that text would describe a song nobody hears."""
    return params.get("title") or title_from(
        params.get("style", ""),
        "" if params.get("instrumental") else params.get("lyrics", ""))


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
                elif mtype in ("execution_success", "execution_error",
                               "execution_interrupted") and pid:
                    ws_progress.pop(pid, None)
        except Exception:
            time.sleep(4)


# --------------------------------------------------------------------------- #
# generation job
# --------------------------------------------------------------------------- #
TERMINAL = ("done", "error", "cancelled")


def run_job(job_id: str, params: dict, built: dict | None = None) -> None:
    def set_state(**kw):
        with jobs_lock:
            # Note when it stopped, so the job list can keep it around long
            # enough to be read rather than measuring from when it started.
            if kw.get("status") in TERMINAL:
                kw.setdefault("ended", time.time())
            jobs[job_id].update(kw)

    wanted = (params.get("format") or "flac").lower()
    params = {**params, "format": render_format(wanted)}

    try:
        set_state(stage="Building the graph", pct=2)
        # The API normally builds this before accepting the job, so a missing
        # node/model is reported to the Create click immediately rather than
        # appearing later on an asynchronous card. Keep the fallback for
        # callers that invoke run_job directly.
        built = built or client.build_prompt(params)
        set_state(stage="Queued in ComfyUI", pct=4, seed=built["seed"])
        prompt_id = client.queue(built["prompt"])
        set_state(prompt_id=prompt_id, stage="Writing the melody plan", pct=6)

        started = time.time()
        last_stage = ""
        unreachable_since = 0.0
        abc_fallback_used = False
        while True:
            time.sleep(1.5)
            # Read the flag under the lock and act outside it: jobs_lock is a
            # plain Lock, so calling set_state() while holding it would wedge
            # this thread and every /api/jobs request behind it.
            with jobs_lock:
                cancelled = bool(jobs[job_id].get("cancelled"))
            if cancelled:
                client.stop(prompt_id)
                set_state(status="cancelled", stage="Cancelled")
                return

            # One history read answers both questions, and a ComfyUI that
            # blinks — a restart, a moment of load — must not throw away a song
            # that is still sitting in its queue.
            try:
                hist = client.history(prompt_id)
            except requests.RequestException:
                unreachable_since = unreachable_since or time.time()
                if time.time() - unreachable_since > 120:
                    set_state(status="error", stage="Failed",
                              error="ComfyUI stopped answering. Check the "
                                    "engine on the Engine page, then make the "
                                    "song again.")
                    return
                set_state(stage="Waiting for ComfyUI to answer")
                continue
            if unreachable_since:
                unreachable_since = 0.0
                # ComfyUI does not keep its queue over a restart, so a
                # prompt it no longer knows about is never going to finish.
                # Waiting out the hour-long timeout would just look like a
                # song that hung.
                if not hist and not client.in_queue(prompt_id):
                    set_state(status="error", stage="Failed",
                              error="ComfyUI restarted, and the song it was "
                                    "working on went with its queue. Press "
                                    "Create to make it again.")
                    return

            err = client.failed(prompt_id, hist)
            if err:
                # ABC planning is useful but not required to render a song.
                # Some Windows/driver combinations fail inside the native
                # YuE2GenerateABC node with OSError 22 even though schema
                # validation and model loading both succeeded. Retry once
                # through YuE2GenerateMusic's supported no-score path rather
                # than throwing the entire song away.
                can_skip_plan = (
                    not abc_fallback_used
                    and err.lower().startswith("yue2generateabc:")
                    and ("errno 22" in err.lower()
                         or "invalid argument" in err.lower())
                    and params.get("use_abc", True)
                    and not params.get("abc")
                    and not params.get("reference_audio"))
                if can_skip_plan:
                    abc_fallback_used = True
                    set_state(stage="Melody planner failed — rendering without "
                                    "a score", pct=6, warning=err)
                    fallback = {**params, "use_abc": False,
                                "seed": built["seed"]}
                    built = client.build_prompt(fallback)
                    prompt_id = client.queue(built["prompt"])
                    set_state(prompt_id=prompt_id)
                    started = time.time()
                    unreachable_since = 0.0
                    continue
                set_state(status="error", error=err, stage="Failed")
                return

            outs = client.outputs(prompt_id, hist)
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
        ext = Path(as_text(item.get("filename"))).suffix.lower()
        if ext not in (".flac", ".mp3", ".opus", ".wav", ".ogg", ".m4a"):
            ext = ".flac"
        TRACKS_DIR.mkdir(parents=True, exist_ok=True)
        dest = TRACKS_DIR / f"{track_id}{ext}"
        partial = dest.with_suffix(dest.suffix + ".part")
        try:
            with client.view(item) as resp:
                resp.raise_for_status()
                with open(partial, "wb") as fh:
                    for chunk in resp.iter_content(1024 * 256):
                        if chunk:
                            fh.write(chunk)
            if partial.stat().st_size == 0:
                raise ComfyError("ComfyUI returned an empty audio file.")
            partial.replace(dest)
        except Exception:
            # A dropped connection must not leave something that looks like a
            # playable track, or an orphan that accumulates forever.
            partial.unlink(missing_ok=True)
            raise

        if wanted == "wav":
            set_state(stage="Saving as wav", pct=96)
        kept = save_as(dest, track_id, wanted)

        track = {
            "id": track_id,
            "title": track_title(params),
            "style": params.get("style", ""),
            "lyrics": params.get("lyrics", ""),
            "instrumental": bool(params.get("instrumental")),
            "seed": built["seed"],
            # duration is what was asked for and is what "reuse settings" loads
            # back into the slider; seconds is how long the song actually came
            # out, and is what gets shown.
            "duration": params.get("duration"),
            "seconds": audio_duration(kept),
            "mode": params.get("mode"),
            "steps": params.get("steps"),
            "cfg": params.get("cfg"),
            "sampler": params.get("sampler"),
            "checkpoint": built["ckpt"],
            "cover": bool(params.get("reference_audio")),
            # The melody plan this song was actually built on, so it can be
            # read, edited and re-rendered.
            "abc": built["abc_text"] or client.preview_text(prompt_id,
                                                            built["abc_node"]),
            "file": kept.name,
            "format": kept.suffix.lstrip(".").lower(),
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
    payload = {
        # Provisional until the live engine has proved that it exposes the
        # nodes and checkpoint needed to construct a generation graph.
        "ready": False,
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
            client.ensure_supported()
            payload["checkpoints"] = client.checkpoints()
            client.pick_checkpoint()
            payload["has_cover_model"] = bool(
                [e for e in client.audio_encoders() if "sheetsage" in e.lower()])
            payload["samplers"], payload["schedulers"] = client.samplers()
            payload["formats"] = available_formats(client.save_formats())
            payload["max_duration"] = client.duration_limit()
        except Exception as exc:  # ComfyUI up but too old / still loading
            payload["schema_error"] = str(exc)
        # The two silent "nothing works" states, named rather than left for
        # the person to infer from an empty dropdown.
        #
        # ComfyUI scans its model folders once, at startup. A checkpoint that
        # landed afterwards is on disk and absent from its list until it is
        # restarted — which reads as a failed download and sends people to
        # re-fetch four gigabytes they already have. checkpoints is the list
        # that decides it: pick_checkpoint() reads that one, so its emptiness
        # is exactly what stops a song being made. "yue2" is the marker both
        # downloaded checkpoints carry (yue2_3b_int8_convrot, yue2_3b_bf16).
        payload["stale_models"] = bool(
            not missing and models_dir and models_dir.is_dir()
            and "checkpoints" in payload
            and not any("yue2" in c.lower() for c in payload["checkpoints"]))
        # And the address can be answered by a different install than the one
        # set up here, which looks exactly like a broken download.
        stats = bootstrap.comfy_stats(cfg["comfy_url"]) or {}
        payload["engine_argv"] = (stats.get("argv") or [""])[0]
        root = client.engine_root()
        payload["engine_mismatch"] = bool(
            root and cfg.get("comfy_dir")
            and not manager.same_install(cfg["comfy_dir"], root))
        payload["ready"] = bool(
            cfg.get("setup_complete") and not missing
            and not payload.get("schema_error")
            and not payload.get("stale_models")
            and not payload.get("engine_mismatch"))
    return jsonify(payload)


@app.post("/api/setup/start")
def api_setup_start():
    if progress.running:
        return jsonify({"ok": False, "error": "Setup is already running."}), 409
    body = json_object()
    mode = as_text(body.get("mode"), "auto")
    if mode not in ("auto", "existing", "managed", "external"):
        # Anything else used to fall through to the managed route, which
        # clones ComfyUI and installs PyTorch — not something a typo should do.
        return jsonify({"ok": False,
                        "error": f"'{mode}' is not a setup route."}), 400
    chosen = as_text(body.get("comfy_dir"))
    cfg.update(settings_from(body, ("comfy_url", "models_dir", "torch_index")
                             + FLAG_SETTINGS))
    client.url = cfg["comfy_url"]
    save_config(cfg)
    progress.__init__()  # reset log and step states
    threading.Thread(target=bootstrap.run_setup,
                     args=(cfg, progress, comfy_proc, chosen, mode),
                     daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/setup/state")
def api_setup_state():
    since = as_cursor(request.args.get("since", 0))
    snap = progress.snapshot(since)
    snap["comfy_tail"] = comfy_proc.tail(12)
    return jsonify(snap)


def _note(msg: str) -> None:
    """An engine action, said in both places it is looked for: the engine
    console (next to ComfyUI's own output) and the setup log."""
    comfy_proc.note(msg)
    progress.log(msg)


def _refresh_schema_when_up() -> None:
    """After a (re)start, throw away the cached schema the moment the engine
    answers.

    ComfyClient caches /object_info for two minutes. Without this, the whole
    point of the restart — a fresh model scan — stays hidden behind the old
    cache, and the page goes on saying there are no checkpoints.
    """
    def wait():
        if bootstrap.wait_for_comfy(cfg["comfy_url"], timeout=900):
            try:
                client.schema(force=True)
            except Exception:
                pass
    threading.Thread(target=wait, daemon=True).start()


def take_over_port(url: str, port: int):
    """Close whatever ComfyUI answers on the port.

    Returns ("manager-reboot", None) when ComfyUI-Manager rebooted it in
    place, ("freed", None) when the port is now empty, or (None, advice) when
    it cannot be done — with advice that names the actual obstacle. "Close it
    yourself" is not advice when the thing to close is a python with no window
    and no tray icon: it sends people hunting through Task Manager.

    Nothing that does not look like ComfyUI is ever stopped: the port may be
    configured wrong, and a wrong address is not a licence to kill whatever
    is at it.
    """
    _note("This ComfyUI was not started here — taking it over.")
    try:
        r = requests.post(f"{url}/manager/reboot", json={}, timeout=5)
        accepted = r.status_code in (200, 201, 204)
    except requests.exceptions.RequestException:
        accepted = True          # the connection dropping *is* the reboot
    if accepted:
        deadline = time.time() + 10
        while time.time() < deadline:
            if not comfy_online(url):
                _note("ComfyUI-Manager took the reboot; waiting for the "
                      "engine to come back.")
                return "manager-reboot", None
            time.sleep(0.5)
        _note("ComfyUI-Manager did not take the reboot; stopping the "
              "process instead.")

    def settled_free() -> bool:
        # A supervisor — ComfyUI Desktop, a launcher script — respawns in
        # under a second, so quiet is only free once it *stays* quiet. Report
        # a port free too early and the managed engine starts into a port that
        # is taken again by the time it binds.
        time.sleep(2.0)
        return not comfy_online(url) and not bootstrap.port_pids(port)

    first_pids: list[int] = []
    denied = False
    for attempt in range(3):
        pids = bootstrap.port_pids(port)
        if attempt == 0:
            first_pids = pids
        if not pids:
            if not comfy_online(url) and settled_free():
                return "freed", None
            if not comfy_online(url):
                _note("It came straight back — something restarted it.")
                continue
            return None, (f"Something answers on port {port} but its process "
                          "could not be found — it may belong to another user "
                          "account. Close it yourself, then press Start the "
                          "engine.")
        for pid in pids:
            cmd = bootstrap.pid_cmdline(pid)
            _note(f"Port {port} is held by pid {pid}"
                  + (f": {cmd[:120]}" if cmd else " (command line unreadable)"))
            if cmd and not any(k in cmd.lower()
                               for k in ("python", "main.py", "comfy")):
                return None, (f"Port {port} is held by something that does "
                              f"not look like ComfyUI ({cmd[:90]}). Close it "
                              "yourself, or point Settings at a different "
                              "address.")
        for pid in pids:
            said = bootstrap.kill_pid(pid)
            _note(f"Stopping pid {pid} — {said or 'no reply'}")
            if "denied" in (said or "").lower():
                denied = True
        deadline = time.time() + 8
        while comfy_online(url) and time.time() < deadline:
            time.sleep(0.5)
        if not comfy_online(url):
            if settled_free():
                return "freed", None
            _note("It came straight back — something restarted it.")
            continue
        _note("Still answering — trying again.")

    now = bootstrap.port_pids(port)
    if denied:
        return None, ("The system refused to stop it (access denied) — it was "
                      "started by another user, or as an administrator. Run "
                      "YuE Studio with the same rights once, or close that "
                      "ComfyUI yourself, then press Start the engine.")
    if now and set(now) != set(first_pids):
        return None, ("It keeps coming back under a new process id — "
                      "something is supervising it (ComfyUI Desktop, or a "
                      "launcher script). Close that application, then press "
                      "Start the engine.")
    return None, ("It would not close. The Engine console shows what was "
                  "tried; close that ComfyUI yourself, then press Start the "
                  "engine.")


def _free_local_port(after: int) -> int:
    """The first port past `after` that nothing is bound to, found by binding."""
    import socket
    for port in range(after + 1, after + 21):
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
    return 0


def foreign_engine_reason() -> str:
    """Why the engine answering the configured address cannot be ours, or "".

    Only judged for a local address, and only when there is a managed install
    to run instead — a remote engine, or an external-mode setup, is the
    user's own and is never second-guessed. A verified-ours engine with an
    empty model list is a models problem, not a port problem: moving would
    bury the real cause, so it never moves.
    """
    if not cfg.get("comfy_dir") or not cfg.get("python"):
        return ""
    host = (urlsplit(cfg["comfy_url"]).hostname or "").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        return ""
    root = client.engine_root()
    if root:
        if manager.same_install(cfg["comfy_dir"], root):
            return ""
        return "it is answered by the ComfyUI in " + root
    models_dir = Path(cfg["models_dir"]) if cfg.get("models_dir") else None
    if (models_dir and models_dir.is_dir()
            and not bootstrap.missing_models(models_dir, cfg)
            and client.checkpoints(force=True) == []):
        return ("it is answered by an engine that does not say where it runs "
                "from and lists no checkpoints, while the model files are on "
                "disk")
    return ""


def relocate_engine(reason: str) -> str:
    """Move to a free port, start the managed install there, keep the choice.

    This is the by-hand recovery — change the address in Settings, press
    Start the engine — done by the app itself, on every launch and on the
    button, once the port has been lost to something else. The new address is
    saved, so later launches go straight to it.
    """
    old_url = cfg["comfy_url"]
    port = _free_local_port(comfy_port(old_url))
    if not port:
        raise RuntimeError("Every port near " + old_url + " is taken — set an "
                           "address by hand in Settings.")
    cfg["comfy_url"] = f"http://127.0.0.1:{port}"
    client.url = cfg["comfy_url"]
    save_config(cfg)
    _note(f"{old_url} is not usable — {reason}. Moving to port {port} "
          "and starting the managed ComfyUI there.")
    comfy_proc.start(cfg["python"], Path(cfg["comfy_dir"]), port, progress,
                     Path(cfg["models_dir"]) if cfg.get("models_dir") else None)
    _refresh_schema_when_up()
    return cfg["comfy_url"]


def _start_managed(port: int) -> None:
    comfy_proc.start(cfg["python"], Path(cfg["comfy_dir"]), port, progress,
                     Path(cfg["models_dir"]) if cfg.get("models_dir") else None)
    _refresh_schema_when_up()


@app.post("/api/comfy/start")
def api_comfy_start():
    if comfy_online(cfg["comfy_url"]):
        reason = foreign_engine_reason()
        if not reason:
            return jsonify({"ok": True, "already": True})
        try:
            moved_to = relocate_engine(reason)
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "moved": True, "comfy_url": moved_to,
                        "reason": reason})
    if not cfg.get("comfy_dir") or not cfg.get("python"):
        if cfg.get("comfy_dir") and not cfg.get("managed", True):
            return jsonify({"ok": False,
                            "error": "YuE Studio does not know which Python "
                                     "that ComfyUI runs on, so it will not "
                                     "start it. Start ComfyUI yourself, then "
                                     "press Recheck."}), 400
        return jsonify({"ok": False,
                        "error": "Run setup first."}), 400
    port = comfy_port(cfg["comfy_url"])
    try:
        _note("Starting ComfyUI…")
        _start_managed(port)
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


@app.post("/api/comfy/restart")
def api_comfy_restart():
    """Stop and start ComfyUI, so it rescans its model folders — the one
    thing only a restart does.

    An engine YuE Studio did not start (an orphan from an earlier run, one
    launched by hand, a ComfyUI Desktop) is taken over rather than declared
    someone else's problem. Each route says which one it took, because "it
    restarted" means something different in each:

        managed         ours: stopped and started again
        started         nothing was there: started
        takeover        someone else's: closed, and ours put in its place
        manager-reboot  ComfyUI-Manager rebooted it in place
        stopped         closed, but this app has no ComfyUI of its own

    A refusal is a 409 whose error names the obstacle, not a shrug.
    """
    url = cfg["comfy_url"]
    port = comfy_port(url)
    can_start = bool(cfg.get("comfy_dir") and cfg.get("python"))

    if comfy_proc.alive():
        if not can_start:
            return jsonify({"ok": False, "error": "Run setup first."}), 400
        _note("Restarting the managed engine…")
        comfy_proc.stop()
        try:
            _start_managed(port)
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "how": "managed"})

    if not comfy_online(url):
        if not can_start:
            return jsonify({"ok": False, "error": "Run setup first."}), 400
        _note("Starting ComfyUI…")
        try:
            _start_managed(port)
        except RuntimeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "how": "started"})

    how, advice = take_over_port(url, port)
    if advice:
        _note(advice)
        return jsonify({"ok": False, "error": advice}), 409
    if how == "manager-reboot":
        _refresh_schema_when_up()
        return jsonify({"ok": True, "how": "manager-reboot"})
    if not can_start:
        return jsonify({"ok": True, "how": "stopped",
                        "note": "Stopped it. YuE Studio has no ComfyUI of its "
                                "own to start — run setup, or start yours "
                                "again yourself."})
    _note("Starting a managed engine in its place…")
    try:
        _start_managed(port)
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "how": "takeover"})


@app.get("/api/comfy/log")
def api_comfy_log():
    """The engine's own console: the visible cue that it is starting, up, or
    saying exactly what failed to load. Clamped, because an unbounded n is a
    way to ask the server for two thousand lines on a 2.5-second poll."""
    n = as_int(request.args.get("n"), 80, 1, 400)
    return jsonify({"lines": comfy_proc.tail(n),
                    "running": comfy_proc.alive(),
                    "online": comfy_online(cfg["comfy_url"])})


TEXT_SETTINGS = ("comfy_url", "comfy_dir", "models_dir", "torch_index")
FLAG_SETTINGS = ("auto_start_comfy", "download_cover_model", "download_bf16")


def settings_from(body: dict, keys: tuple[str, ...]) -> dict:
    """Read the settings a request is allowed to change, as their real types.

    Built in full before anything is applied: a half-applied change that then
    fails leaves the running app pointing at a value it already rejected.
    """
    change: dict = {}
    for key in keys:
        if key not in body:
            continue
        value = body[key]
        if key in FLAG_SETTINGS:
            if isinstance(value, bool):
                change[key] = value
        elif isinstance(value, str):
            change[key] = value
        # Anything else is not something the page sends. Leaving the key alone
        # keeps a setting that was working from being replaced by nonsense.
    if "comfy_url" in change:
        usable = clean_url(change["comfy_url"])
        if usable:
            change["comfy_url"] = usable
        else:
            change.pop("comfy_url")
    return change


@app.get("/api/config")
def api_config_get():
    """The saved settings, and nothing else — no probes, answers in
    milliseconds. The Settings dialog fills itself from this on open; it used
    to be filled by a status call that can take seconds, and a Save pressed
    before that returned wrote the checkboxes' blank defaults over real
    settings. That is how "start ComfyUI with YuE Studio" got switched off by
    someone changing the address."""
    return jsonify({"config": {k: cfg.get(k)
                               for k in (TEXT_SETTINGS + FLAG_SETTINGS)}})


@app.post("/api/config")
def api_config():
    body = json_object()
    cfg.update(settings_from(body, TEXT_SETTINGS + FLAG_SETTINGS))
    client.url = cfg["comfy_url"]
    save_config(cfg)
    return jsonify({"ok": True, "config": cfg})


# --------------------------------------------------------------------------- #
# routes - generation
# --------------------------------------------------------------------------- #
@app.post("/api/generate")
def api_generate():
    params = clean_generate(json_object())
    if not params["style"].strip():
        return jsonify({"error": "Add a style description before generating."}), 400
    if not comfy_online(cfg["comfy_url"]):
        return jsonify({"error": "ComfyUI is not running. Start it from "
                                 "Settings."}), 503

    count = params["count"]
    # Validate and construct every graph before creating job records. This is
    # both a readiness probe and an all-or-nothing batch: an outdated ComfyUI,
    # stale model list, or invalid selected option is returned directly and
    # cannot leave several doomed jobs behind in the queue UI.
    render_params = {**params,
                     "format": render_format(
                         (params.get("format") or "flac").lower())}
    try:
        built_prompts = [client.build_prompt(render_params)
                         for _ in range(count)]
    except ComfyError as exc:
        return jsonify({"error": str(exc)}), 409
    except requests.RequestException:
        return jsonify({"error": "ComfyUI stopped answering while YuE Studio "
                                 "checked whether it was ready. Try again after "
                                 "the Engine page says ready."}), 503

    created = []
    with jobs_lock:
        stale = [k for k, j in jobs.items()
                 if j["status"] != "running"
                 and time.time() - j.get("ended", j["created"]) > 3600]
        for k in stale:
            jobs.pop(k, None)
    for built in built_prompts:
        job_id = uuid.uuid4().hex[:12]
        with jobs_lock:
            jobs[job_id] = {"id": job_id, "status": "running", "pct": 0,
                            "stage": "Starting", "created": time.time(),
                            "title": track_title(params),
                            "style": params.get("style", "")}
        threading.Thread(target=run_job, args=(job_id, dict(params), built),
                         daemon=True).start()
        created.append(job_id)
        time.sleep(0.2)  # keep queue order stable
    return jsonify({"jobs": created})


@app.get("/api/jobs")
def api_jobs():
    """Everything still going, plus whatever stopped in the last two minutes.

    Measured from when a job ended, not from when it began. A song takes
    minutes, so keying this on its start time dropped every real failure out of
    the list the moment it failed — the card vanished and the reason with it.
    """
    cutoff = time.time() - 120
    with jobs_lock:
        active = [j for j in jobs.values()
                  if j["status"] == "running"
                  or j.get("ended", j["created"]) > cutoff]
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
    except requests.RequestException:
        return jsonify({"error": "ComfyUI is not running, so the reference "
                                 "song has nowhere to go. Start it from "
                                 "Settings and try again."}), 503
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
            path = track_file(item)
            if path is None or not path.is_file():
                return jsonify({"error": "Audio file is missing."}), 404
            mime = mimetypes.guess_type(path.name)[0] or "audio/flac"
            return send_file(path, mimetype=mime, conditional=True,
                             download_name=f"{item.get('title') or 'track'}{path.suffix}")
    return jsonify({"error": "Track not found."}), 404


@app.patch("/api/track/<track_id>")
def api_rename(track_id: str):
    body = json_object()
    with library_lock:
        items = read_library()
        for item in items:
            if item["id"] == track_id:
                title = as_text(body.get("title")).strip()
                if title:
                    item["title"] = title[:120]
                if body.get("seconds") is not None:
                    # Sent by the player once the browser has decoded the file,
                    # for formats audio_duration() could not read without
                    # ffprobe.
                    try:
                        seconds = round(float(body["seconds"]), 2)
                    except (TypeError, ValueError):
                        seconds = 0.0
                    if 0 < seconds < 7200:
                        item["seconds"] = seconds
                write_library(items)
                return jsonify({"ok": True, "track": item})
    return jsonify({"error": "Track not found."}), 404


@app.delete("/api/track/<track_id>")
def api_delete(track_id: str):
    with library_lock:
        items = read_library()
        keep = [i for i in items if i["id"] != track_id]
        gone = [i for i in items if i["id"] == track_id]
        write_library(keep)
    # The file goes after the list is settled — deleting it first would leave a
    # library entry pointing at nothing if the write failed.
    for item in gone:
        for path in track_files(item):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# routes - dependencies
# --------------------------------------------------------------------------- #
@app.get("/api/deps")
def api_deps():
    # What ComfyUI itself offers, so the model row can tell a file that has not
    # been downloaded apart from one the engine cannot see. None means it could
    # not be asked, which is not the same as an empty list.
    online = comfy_online(cfg["comfy_url"])
    listed = client.checkpoints() if online else None
    engine_root = client.engine_root() if online else ""
    fresh = request.args.get("fresh") == "1"
    return jsonify({"items": manager.dependencies(cfg, listed, engine_root,
                                                  fresh),
                    "os": __import__("platform").system(),
                    "torch_index": cfg.get("torch_index", "")})


@app.post("/api/deps/<dep_id>/install")
def api_dep_install(dep_id: str):
    body = json_object()
    if dep_id not in manager.INSTALLABLE:
        return jsonify({"error": f"There is nothing called '{dep_id}' to "
                                 "install."}), 400
    if body.get("torch_index") is not None:
        cfg["torch_index"] = as_text(body["torch_index"])
        save_config(cfg)
    try:
        task = manager.install_dependency(dep_id, cfg, body)
        return jsonify({"ok": True, "task": task.view()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400


@app.get("/api/tasks")
def api_tasks():
    since = as_cursor(request.args.get("since", 0))
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
    body = json_object()
    if "token" in body:
        cfg["hf_token"] = as_text(body["token"]).strip()
    if body.get("endpoint") is not None:
        cfg["hf_endpoint"] = (as_text(body["endpoint"]).strip()
                              or manager.DEFAULT_ENDPOINT)
    if body.get("repo"):
        cfg["hf_repo"] = as_text(body["repo"]).strip() or manager.DEFAULT_REPO
    if body.get("models_dir"):
        cfg["models_dir"] = as_text(body["models_dir"]).strip()
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
    body = json_object()
    repo = (as_text(body.get("repo")) or cfg.get("hf_repo")
            or manager.DEFAULT_REPO).strip()
    path = as_text(body.get("path")).strip()
    if not path:
        return jsonify({"error": "Pick a file to download."}), 400
    try:
        task = manager.hf_download(cfg, repo, path,
                                   as_text(body.get("folder")),
                                   as_text(body.get("revision")) or "main")
        return jsonify({"ok": True, "task": task.view()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400


@app.get("/api/hf/local")
def api_hf_local():
    return jsonify({"models": manager.local_models(cfg),
                    "models_dir": cfg.get("models_dir", "")})


@app.delete("/api/hf/local")
def api_hf_delete():
    body = json_object()
    try:
        manager.delete_model(cfg, body.get("folder", ""), body.get("name", ""))
        return jsonify({"ok": True})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 400


# --------------------------------------------------------------------------- #
def engine_trouble() -> list[str]:
    """Why the engine already answering is no use as it stands, or [].

    Only the problems a restart actually cures. A ComfyUI too old to have the
    YuE2 nodes is deliberately not one of them: those nodes are part of
    ComfyUI itself from v0.35.0, so restarting cannot conjure them, and
    treating their absence as a reason to replace would kill a working engine
    once per launch and never fix anything.
    """
    reasons = []
    try:
        client.schema(force=True)
        checkpoints = client.checkpoints()
    except Exception as exc:  # noqa: BLE001
        _note(f"The engine already running would not describe itself "
              f"({exc}) — leaving it alone.")
        return []
    models_dir = Path(cfg["models_dir"]) if cfg.get("models_dir") else None
    weights_here = bool(models_dir and models_dir.is_dir()
                        and not bootstrap.missing_models(models_dir, cfg))
    if weights_here and not any("yue2" in c.lower() for c in checkpoints):
        reasons.append("it started before the model files landed, so it has "
                       "not scanned them")
    return reasons


def ensure_engine_at_boot() -> None:
    """A launch ends with a working engine, without a button being pressed.

    Offline: start the managed one. Online and provably a *different*
    install: step aside onto a free port — YuE Studio's own gentler cure,
    which leaves the other engine running (see relocate_engine). Online, at
    our own address, and useless — a startup scan that ran before the weights
    landed — restart it in place, through the same looks-like-ComfyUI guard
    the Restart button uses; stepping aside there would leave the stale engine
    holding the card and fix nothing. Online and healthy: adopt it, and say
    so, because silence at that point reads as a failure to start.

    An external-mode setup (`managed` false) is the person's own ComfyUI: it
    is told what is wrong and left alone.
    """
    if not (cfg.get("setup_complete") and cfg.get("auto_start_comfy", True)):
        return
    if not (cfg.get("comfy_dir") and cfg.get("python")):
        return
    url = cfg["comfy_url"]
    port = comfy_port(url)
    try:
        if not comfy_online(url):
            _note("Starting ComfyUI from the last setup…")
            _start_managed(port)
            return

        # Something answers — but a launch must not take that on faith.
        reason = foreign_engine_reason()
        if reason:
            relocate_engine(reason)
            return

        trouble = engine_trouble()
        if not trouble:
            _note(f"Adopting the ComfyUI already running at {url}.")
            return
        if not cfg.get("managed", True):
            _note("The engine already running has problems ("
                  + "; ".join(trouble) + ") but it is yours, not YuE Studio's "
                  "— restart it yourself, or press Restart the engine.")
            return
        _note("The engine already running is no use as it stands — "
              + "; ".join(trouble) + ". Replacing it.")
        how, advice = take_over_port(url, port)
        if advice:
            _note(advice)
            return
        if how == "manager-reboot":
            _refresh_schema_when_up()
            return
        _note("Starting a managed engine in its place…")
        _start_managed(port)
    except RuntimeError as exc:
        # The app still comes up; the Engine page explains the rest.
        _note(str(exc))


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TRACKS_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=ws_listener, daemon=True).start()
    # The engine comes up on its own; the page can open while it does.
    threading.Thread(target=ensure_engine_at_boot, daemon=True).start()

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
