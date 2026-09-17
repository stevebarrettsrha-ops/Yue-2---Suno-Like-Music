"""
manager.py - everything the front end needs to get the machine ready.

Two jobs:

  Dependencies - check what is installed (Python, Git, ComfyUI, PyTorch + CUDA,
  ComfyUI's own requirements, ffmpeg, the model files) and install any of them
  on request, streaming the log back to the page.

  HuggingFace - browse a repo's files, download the ones the person picks into
  the right ComfyUI model folder, show progress, cancel, and delete. Nothing
  here needs a terminal: repo, token, mirror endpoint and target folder are all
  set from the front end.

Long work runs as a Task. The page polls /api/tasks for progress and log lines.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import requests

import bootstrap
from bootstrap import (APP_DIR, MODELS, MODEL_BF16, existing_python,
                       have_git, venv_python)

DEFAULT_ENDPOINT = "https://huggingface.co"
DEFAULT_REPO = "Comfy-Org/YuE2"

# ComfyUI model folders a download can target.
MODEL_FOLDERS = ["checkpoints", "audio_encoders", "vae", "loras",
                 "diffusion_models", "text_encoders", "clip", "audio_vae",
                 "upscale_models"]

# Files worth a one-tap button on the Models page.
CURATED = [
    {"repo": DEFAULT_REPO, "path": "checkpoints/yue2_3b_int8_convrot.safetensors",
     "folder": "checkpoints", "size": 3_960_000_000, "role": "required",
     "note": "The song model. Quantised to int8 — loads on 8 GB cards."},
    {"repo": DEFAULT_REPO, "path": "audio_encoders/sheetsage2_bf16.safetensors",
     "folder": "audio_encoders", "size": 1_390_000_000, "role": "cover",
     "note": "SheetSage2. Only needed to make covers from a reference song."},
    {"repo": DEFAULT_REPO, "path": "checkpoints/yue2_3b_bf16.safetensors",
     "folder": "checkpoints", "size": 7_800_000_000, "role": "optional",
     "note": "Full-precision song model. Better quality, needs more VRAM."},
]


# --------------------------------------------------------------------------- #
# tasks
# --------------------------------------------------------------------------- #
class Task:
    def __init__(self, kind: str, title: str, meta: dict | None = None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.title = title
        self.meta = meta or {}
        self.state = "running"          # running | done | error | cancelled
        self.pct = 0.0
        self.detail = ""
        self.lines: list[str] = []
        self.created = time.time()
        self.cancel = False
        self._lock = threading.Lock()

    def log(self, msg: str) -> None:
        with self._lock:
            self.lines.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            if len(self.lines) > 1200:
                del self.lines[:600]

    def set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def view(self, since: int = 0) -> dict:
        with self._lock:
            return {"id": self.id, "kind": self.kind, "title": self.title,
                    "meta": self.meta, "state": self.state,
                    "pct": round(self.pct, 1), "detail": self.detail,
                    "created": self.created, "cursor": len(self.lines),
                    "lines": self.lines[since:]}


class Tasks:
    def __init__(self) -> None:
        self._items: dict[str, Task] = {}
        self._lock = threading.Lock()

    def add(self, task: Task) -> Task:
        with self._lock:
            self._items[task.id] = task
            # keep the last 40 finished tasks
            finished = sorted((t for t in self._items.values()
                               if t.state != "running"), key=lambda t: t.created)
            for old in finished[:-40]:
                self._items.pop(old.id, None)
        return task

    def get(self, task_id: str) -> Task | None:
        return self._items.get(task_id)

    def list(self) -> list[Task]:
        with self._lock:
            return sorted(self._items.values(), key=lambda t: t.created,
                          reverse=True)

    def running(self, kind: str = "") -> list[Task]:
        return [t for t in self.list()
                if t.state == "running" and (not kind or t.kind == kind)]


TASKS = Tasks()


def spawn(kind: str, title: str, fn, meta: dict | None = None) -> Task:
    task = TASKS.add(Task(kind, title, meta))

    def wrapper():
        try:
            fn(task)
            if task.state == "running":
                task.set(state="done", pct=100)
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            task.log(f"FAILED: {exc}")
            task.set(state="error", detail=str(exc))

    threading.Thread(target=wrapper, daemon=True).start()
    return task


# --------------------------------------------------------------------------- #
# shell helpers
# --------------------------------------------------------------------------- #
def stream(cmd: list[str], task: Task, cwd: str | None = None,
           keep: tuple[str, ...] = ()) -> int:
    task.log("$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        if not keep or line.startswith(keep):
            task.log(line[:220])
        if task.cancel:
            proc.terminate()
            task.set(state="cancelled", detail="Cancelled")
            return 1
    return proc.wait()


def _which(name: str) -> str:
    return shutil.which(name) or ""


def comfy_python(cfg: dict) -> str:
    """The interpreter that runs ComfyUI, if we know it."""
    if cfg.get("python"):
        return cfg["python"]
    if cfg.get("comfy_dir"):
        vp = venv_python(Path(cfg["comfy_dir"]))
        if vp.exists():
            return str(vp)
        if not cfg.get("managed", True):
            return existing_python(Path(cfg["comfy_dir"]))
    return ""


def _probe_torch(python: str) -> dict:
    if not python or not Path(python).exists():
        return {"state": "unknown", "detail": "No Python environment known yet."}
    code = ("import torch,json;"
            "print(json.dumps({'v':torch.__version__,"
            "'cuda':torch.cuda.is_available(),"
            "'dev':(torch.cuda.get_device_name(0) if torch.cuda.is_available() "
            "else '')}))")
    try:
        out = subprocess.run([python, "-c", code], capture_output=True,
                             text=True, timeout=90)
    except Exception as exc:  # noqa: BLE001
        return {"state": "missing", "detail": str(exc)}
    if out.returncode != 0:
        return {"state": "missing", "detail": "PyTorch is not installed."}
    import json as _json
    try:
        d = _json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        return {"state": "unknown", "detail": out.stdout[-160:]}
    if d["cuda"]:
        return {"state": "ok", "detail": f"torch {d['v']} — GPU: {d['dev']}"}
    return {"state": "warn",
            "detail": f"torch {d['v']} — no GPU detected, songs will be slow."}


# --------------------------------------------------------------------------- #
# dependency report
# --------------------------------------------------------------------------- #
def dependencies(cfg: dict) -> list[dict]:
    items: list[dict] = []
    sysname = platform.system()

    # Python
    try:
        py = bootstrap.find_python()
        items.append({"id": "python", "label": "Python 3.10+", "state": "ok",
                      "detail": py, "action": None})
    except Exception as exc:  # noqa: BLE001
        installable = bool(_PACKAGES["python"].get(sysname))
        items.append({"id": "python", "label": "Python 3.10+", "state": "missing",
                      "detail": str(exc),
                      "action": "install" if installable else None,
                      "hint": "Installing it needs a restart of YuE Studio "
                              "before the new Python is on PATH."
                              if installable else
                              "Install from python.org and restart YuE Studio."})

    # Git
    git = _which("git")
    items.append({"id": "git", "label": "Git", "state": "ok" if git else "missing",
                  "detail": git or "Needed to download and update ComfyUI.",
                  "action": None if git else "install"})

    # ComfyUI
    comfy_dir = Path(cfg["comfy_dir"]) if cfg.get("comfy_dir") else None
    if comfy_dir and (comfy_dir / "main.py").exists():
        items.append({"id": "comfyui", "label": "ComfyUI", "state": "ok",
                      "detail": str(comfy_dir), "action": "update"})
    else:
        items.append({"id": "comfyui", "label": "ComfyUI", "state": "missing",
                      "detail": "Not installed yet.", "action": "install"})

    # PyTorch. A ComfyUI we did not install brings its own environment, and
    # that environment is not ours to change — report what we find and leave
    # the Install button off.
    py = comfy_python(cfg)
    ours = bool(cfg.get("managed", True))
    torch = _probe_torch(py)
    torch_detail = torch["detail"]
    torch_action = "install" if torch["state"] != "ok" else "reinstall"
    if not ours:
        torch_action = None
        if not py:
            torch = {"state": "unknown"}
            torch_detail = ("This ComfyUI brings its own Python environment. "
                            "Start it yourself and press Recheck.")
        elif torch["state"] != "ok":
            torch = {"state": "warn"}
            torch_detail = (f"{py} has no working PyTorch. Fix it in that "
                            "install, or let YuE Studio set up its own ComfyUI.")
    items.append({"id": "torch", "label": "PyTorch", "state": torch["state"],
                  "detail": torch_detail, "action": torch_action})

    # ComfyUI requirements
    reqs_state, reqs_detail = "unknown", "Install ComfyUI first."
    if comfy_dir and (comfy_dir / "requirements.txt").exists() and py:
        try:
            out = subprocess.run(
                [py, "-c", "import safetensors,einops,torchsde;print('ok')"],
                capture_output=True, text=True, timeout=90)
            if out.returncode == 0:
                reqs_state, reqs_detail = "ok", "ComfyUI packages installed."
            else:
                reqs_state, reqs_detail = "missing", "Some packages are missing."
        except Exception as exc:  # noqa: BLE001
            reqs_state, reqs_detail = "unknown", str(exc)
    elif not ours:
        reqs_detail = "Managed by your own ComfyUI install."
    items.append({"id": "comfy_reqs", "label": "ComfyUI packages",
                  "state": reqs_state, "detail": reqs_detail,
                  "action": "install" if ours else None})

    # ffmpeg (mp3 export)
    ff = _which("ffmpeg")
    items.append({"id": "ffmpeg", "label": "ffmpeg",
                  "state": "ok" if ff else "warn",
                  "detail": ff or "Optional — needed to save as mp3.",
                  "action": None if ff else "install"})

    # Models
    models_dir = Path(cfg["models_dir"]) if cfg.get("models_dir") else None
    if models_dir and models_dir.is_dir():
        missing = [Path(m[0]).name for m in bootstrap.missing_models(models_dir, cfg)]
        need = Path(MODELS[0][0]).name
        if need in missing:
            items.append({"id": "models", "label": "Model files", "state": "missing",
                          "detail": "The song model has not been downloaded.",
                          "action": "models"})
        elif missing:
            items.append({"id": "models", "label": "Model files", "state": "warn",
                          "detail": "Optional files missing: " + ", ".join(missing),
                          "action": "models"})
        else:
            items.append({"id": "models", "label": "Model files", "state": "ok",
                          "detail": "All files present.", "action": "models"})
    else:
        items.append({"id": "models", "label": "Model files", "state": "unknown",
                      "detail": "Set the models folder first.", "action": "models"})

    # Engine
    online = bootstrap.comfy_online(cfg["comfy_url"])
    items.append({"id": "engine", "label": "Engine",
                  "state": "ok" if online else "missing",
                  "detail": (cfg["comfy_url"] if online
                             else "ComfyUI is not answering."),
                  "action": None if online else "start"})

    for it in items:
        it["os"] = sysname
    return items


# --------------------------------------------------------------------------- #
# dependency installers
# --------------------------------------------------------------------------- #
def install_dependency(dep_id: str, cfg: dict, opts: dict) -> Task:
    labels = {"git": "Install Git", "comfyui": "Install ComfyUI",
              "torch": "Install PyTorch", "comfy_reqs": "Install ComfyUI packages",
              "ffmpeg": "Install ffmpeg", "python": "Install Python"}
    title = labels.get(dep_id, f"Install {dep_id}")

    if dep_id in ("torch", "comfy_reqs") and not cfg.get("managed", True):
        raise RuntimeError(
            "This ComfyUI was not installed by YuE Studio, so its Python "
            "environment is left alone. Install the packages there yourself, "
            "or run setup again and pick a fresh ComfyUI.")

    def run(task: Task) -> None:
        if dep_id in ("git", "ffmpeg", "python"):
            _install_system_package(dep_id, task)
        elif dep_id == "comfyui":
            _install_comfyui(task, cfg)
        elif dep_id == "torch":
            _install_torch(task, cfg, opts)
        elif dep_id == "comfy_reqs":
            _install_reqs(task, cfg)
        else:
            raise RuntimeError(f"Nothing to install for '{dep_id}'.")

    return spawn("dependency", title, run, {"dep": dep_id})


# Each entry is the list of commands to run in order. A command marked
# optional may fail without failing the install: on Windows a tool that has
# just been put on PATH is not visible to this already-running process, so the
# follow-up step often has to wait for a restart.
_PACKAGES: dict[str, dict[str, list[tuple[list[str], bool]]]] = {
    "git": {
        "Windows": [(["winget", "install", "--id", "Git.Git", "-e",
                      "--source", "winget", "--accept-package-agreements",
                      "--accept-source-agreements"], False)],
        "Darwin": [(["brew", "install", "git"], False)],
        "Linux": [(["sudo", "-n", "apt-get", "install", "-y", "git"], False)],
    },
    "ffmpeg": {
        "Windows": [(["winget", "install", "--id", "Gyan.FFmpeg", "-e",
                      "--source", "winget", "--accept-package-agreements",
                      "--accept-source-agreements"], False)],
        "Darwin": [(["brew", "install", "ffmpeg"], False)],
        "Linux": [(["sudo", "-n", "apt-get", "install", "-y", "ffmpeg"], False)],
    },
    "python": {
        # The Store's Python install manager, then a runtime through it. The
        # standalone python.org installer is on its way out — it stops being
        # released with 3.16 — so the manager is the route that keeps working.
        "Windows": [(["winget", "install", "9NQ7512CXL7T",
                      "--accept-package-agreements",
                      "--accept-source-agreements"], False),
                    (["py", "install", "3.13"], True)],
        "Darwin": [(["brew", "install", "python"], False)],
        # python3-venv is separate on Debian and Ubuntu, and without it the
        # ComfyUI environment cannot be created at all.
        "Linux": [(["sudo", "-n", "apt-get", "install", "-y",
                    "python3", "python3-venv", "python3-pip"], False)],
    },
}


def _install_system_package(name: str, task: Task) -> None:
    steps = _PACKAGES[name].get(platform.system())
    if not steps:
        raise RuntimeError(
            f"{name} has to be installed by hand on this system. "
            f"Install it, then press Recheck.")
    tool = steps[0][0][0]
    if not shutil.which(tool):
        raise RuntimeError(
            f"{name} has to be installed by hand on this system — "
            f"'{tool}' is not available. Install {name}, then press Recheck.")

    for cmd, optional in steps:
        if optional and not shutil.which(cmd[0]):
            task.log(f"Skipping '{' '.join(cmd)}' — {cmd[0]} is not on PATH "
                     f"in this process yet.")
            continue
        task.set(detail=f"Running {' '.join(cmd[:3])}…")
        if stream(cmd, task) != 0:
            if optional:
                task.log(f"'{' '.join(cmd[:3])}' did not finish; a restart of "
                         f"YuE Studio may be needed before it will run.")
                continue
            raise RuntimeError(f"The installer for {name} did not finish. "
                               "Install it by hand, then press Recheck.")
    task.set(detail=f"{name} installed. It may need a restart of YuE Studio "
                    "to appear on PATH.")


def _install_comfyui(task: Task, cfg: dict) -> None:
    if not have_git():
        raise RuntimeError("Git is needed first — install it from this page.")
    target = Path(cfg["comfy_dir"]) if cfg.get("comfy_dir") else APP_DIR / "ComfyUI"
    if (target / "main.py").exists():
        task.set(detail="Updating ComfyUI…")
        stream(["git", "-C", str(target), "pull", "--ff-only"], task)
    else:
        task.set(detail="Downloading ComfyUI…")
        if stream(["git", "clone", "--depth", "1", bootstrap.COMFY_REPO,
                   str(target)], task) != 0:
            raise RuntimeError("git clone failed — see the log.")
    cfg["comfy_dir"] = str(target)
    cfg["models_dir"] = cfg.get("models_dir") or str(target / "models")
    bootstrap.save_config(cfg)
    task.set(detail=str(target))


def _install_torch(task: Task, cfg: dict, opts: dict) -> None:
    comfy_dir = Path(cfg.get("comfy_dir") or "")
    if not comfy_dir or not (comfy_dir / "main.py").exists():
        raise RuntimeError("Install ComfyUI first.")
    vpy = venv_python(comfy_dir)
    if not vpy.exists():
        task.set(detail="Creating the Python environment…")
        base = bootstrap.find_python()
        if stream([base, "-m", "venv", str(comfy_dir.parent / "comfy-venv")],
                  task) != 0:
            raise RuntimeError("Could not create the virtual environment.")
    index = (opts.get("torch_index") or cfg.get("torch_index")
             or bootstrap._torch_index())
    cfg["python"] = str(vpy)
    bootstrap.save_config(cfg)
    task.set(detail="Installing PyTorch — this is the big one…")
    stream([str(vpy), "-m", "pip", "install", "--upgrade", "pip", "wheel"], task,
           keep=("Collecting", "Installing", "Successfully", "ERROR"))
    cmd = [str(vpy), "-m", "pip", "install", "torch", "torchaudio", "torchvision"]
    if index:
        cmd += ["--index-url", index]
    if stream(cmd, task, keep=("Collecting", "Downloading", "Installing",
                               "Successfully", "ERROR")) != 0:
        raise RuntimeError("PyTorch install failed. Try a different wheel index "
                           "on this page.")
    task.set(detail=_probe_torch(str(vpy))["detail"])


def _install_reqs(task: Task, cfg: dict) -> None:
    comfy_dir = Path(cfg.get("comfy_dir") or "")
    py = comfy_python(cfg)
    if not py or not (comfy_dir / "requirements.txt").exists():
        raise RuntimeError("Install ComfyUI and PyTorch first.")
    task.set(detail="Installing ComfyUI packages…")
    if stream([py, "-m", "pip", "install", "-r",
               str(comfy_dir / "requirements.txt")], task,
              keep=("Collecting", "Installing", "Successfully", "ERROR")) != 0:
        raise RuntimeError("Package install failed — see the log.")
    task.set(detail="ComfyUI packages installed.")


# --------------------------------------------------------------------------- #
# huggingface
# --------------------------------------------------------------------------- #
def hf_headers(cfg: dict) -> dict:
    token = (cfg.get("hf_token") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def hf_endpoint(cfg: dict) -> str:
    return (cfg.get("hf_endpoint") or DEFAULT_ENDPOINT).rstrip("/")


def hf_list(cfg: dict, repo: str, revision: str = "main") -> dict:
    """List a repo's files. Works for models and datasets."""
    base = hf_endpoint(cfg)
    errors = []
    for kind in ("models", "datasets"):
        url = f"{base}/api/{kind}/{repo}/tree/{revision}?recursive=1"
        try:
            r = requests.get(url, headers=hf_headers(cfg), timeout=30)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            continue
        if r.status_code == 401:
            raise RuntimeError("This repo needs a HuggingFace token. Add one "
                               "on this page, then try again.")
        if r.status_code == 403:
            raise RuntimeError("Your token cannot read this repo. Accept the "
                               "model licence on huggingface.co first.")
        if r.status_code == 404:
            continue
        if r.status_code >= 500:
            raise RuntimeError(f"HuggingFace answered {r.status_code}. It is "
                               "probably busy — try again in a moment.")
        r.raise_for_status()
        files = []
        for entry in r.json():
            if entry.get("type") != "file":
                continue
            size = (entry.get("lfs") or {}).get("size") or entry.get("size") or 0
            files.append({"path": entry["path"], "size": size})
        files.sort(key=lambda f: (-f["size"], f["path"]))
        return {"repo": repo, "kind": kind, "revision": revision, "files": files}
    raise RuntimeError(f"Could not find '{repo}' on {base}. "
                       + ("; ".join(errors) if errors else
                          "Check the spelling, or add a token for a gated repo."))


def guess_folder(path: str) -> str:
    head = path.split("/")[0]
    if head in MODEL_FOLDERS:
        return head
    low = path.lower()
    if "encoder" in low or "sheetsage" in low:
        return "audio_encoders"
    if "vae" in low:
        return "vae"
    if "lora" in low:
        return "loras"
    return "checkpoints"


def local_models(cfg: dict) -> list[dict]:
    root = Path(cfg["models_dir"]) if cfg.get("models_dir") else None
    out: list[dict] = []
    if not root or not root.is_dir():
        return out
    for folder in MODEL_FOLDERS:
        d = root / folder
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if f.is_file() and f.suffix in (".safetensors", ".ckpt", ".pt",
                                            ".bin", ".gguf", ".part"):
                out.append({"folder": folder, "name": f.name,
                            "size": f.stat().st_size,
                            "partial": f.suffix == ".part"})
    return out


def hf_download(cfg: dict, repo: str, path: str, folder: str,
                revision: str = "main") -> Task:
    root = Path(cfg["models_dir"]) if cfg.get("models_dir") else None
    if not root:
        raise RuntimeError("Set the ComfyUI models folder before downloading.")
    folder = folder if folder in MODEL_FOLDERS else guess_folder(path)
    dest = root / folder / Path(path).name
    url = f"{hf_endpoint(cfg)}/{repo}/resolve/{revision}/{path}"

    if any(t.meta.get("dest") == str(dest) for t in TASKS.running("download")):
        raise RuntimeError(f"{dest.name} is already downloading.")

    def run(task: Task) -> None:
        task.log(f"{repo} → {folder}/{Path(path).name}")
        _stream_download(url, dest, task, hf_headers(cfg))

    return spawn("download", Path(path).name, run,
                 {"repo": repo, "path": path, "folder": folder,
                  "dest": str(dest), "name": Path(path).name})


def _stream_download(url: str, dest: Path, task: Task, headers: dict) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    head = dict(headers)
    if have:
        head["Range"] = f"bytes={have}-"
        task.log(f"Resuming at {have/1e9:.2f} GB")

    with requests.get(url, headers=head, stream=True, timeout=60,
                      allow_redirects=True) as r:
        if r.status_code == 416:
            part.replace(dest)
            task.set(pct=100, detail="Already complete")
            return
        if r.status_code in (401, 403):
            raise RuntimeError("HuggingFace refused the download. Add a token "
                               "with access to this repo and try again.")
        r.raise_for_status()
        # Only a 206 means the server honoured the Range header; on a 200 it is
        # sending the whole file again, so what is already on disk counts for
        # nothing — reset before working out the total, or the bar stops short.
        resuming = bool(have) and r.status_code == 206
        mode = "ab" if resuming else "wb"
        if not resuming:
            have = 0
        total = int(r.headers.get("Content-Length", 0)) + have
        got, last, started = have, 0.0, time.time()
        with open(part, mode) as fh:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if task.cancel:
                    task.set(state="cancelled",
                             detail="Cancelled — partial file kept, "
                                    "downloading again resumes it.")
                    return
                if not chunk:
                    continue
                fh.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - last > 0.7:
                    last = now
                    speed = (got - have) / max(now - started, 0.1)
                    eta = (total - got) / speed if speed > 0 and total else 0
                    task.set(pct=(got / total * 100) if total else 0,
                             detail=f"{got/1e9:.2f} / {total/1e9:.2f} GB · "
                                    f"{speed/1e6:.1f} MB/s · "
                                    f"{int(eta//60)}m {int(eta%60)}s left")
    part.replace(dest)
    task.set(pct=100, detail=f"Saved — {dest.stat().st_size/1e9:.2f} GB")
    task.log(f"Saved to {dest}")


def delete_model(cfg: dict, folder: str, name: str) -> None:
    """Remove one model file, and nothing else.

    The folder comes off a fixed list and the name has to be a bare filename,
    so `root/folder/name` cannot climb out of the models tree. What is then
    unlinked is that entry itself, never where it might point: a model file
    that happens to be a symlink loses the link and leaves its target alone.
    Big model folders are very often symlinks onto another drive, so the check
    that the entry really sits in the chosen folder compares the two resolved
    directories rather than matching path text — "/models" is a prefix of
    "/models-elsewhere", and a string test lets a file outside be deleted.
    """
    root = Path(cfg["models_dir"]) if cfg.get("models_dir") else None
    if not root:
        raise RuntimeError("No models folder is set.")
    if (folder not in MODEL_FOLDERS or not name or name in (".", "..")
            or "/" in name or "\\" in name):
        raise RuntimeError("That path is not allowed.")
    entry = root / folder / name
    try:
        if entry.parent.resolve() != (root / folder).resolve():
            raise RuntimeError("That path is outside the models folder.")
    except OSError as exc:
        raise RuntimeError("That path is outside the models folder.") from exc
    if not entry.is_symlink() and not entry.is_file():
        raise RuntimeError("That file is already gone."
                           if not entry.exists() else
                           "That is not a model file.")
    entry.unlink()
