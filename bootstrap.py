"""
bootstrap.py - first-launch setup for YuE Studio.

Responsibilities:
  1. Find a usable Python interpreter (execution test, not PATH lookup).
  2. Find an existing ComfyUI install, or clone a managed one into ./ComfyUI.
  3. Create a venv and install torch + ComfyUI requirements (managed mode only).
  4. Download the YuE2 checkpoint and SheetSage2 audio encoder from HuggingFace,
     resumable, into the right ComfyUI model folders.
  5. Launch ComfyUI headless and wait for it to answer /system_stats.

All long work runs on a worker thread and reports into a Progress object that
the web UI polls.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"

COMFY_REPO = "https://github.com/comfyanonymous/ComfyUI.git"
HF_BASE = "https://huggingface.co/Comfy-Org/YuE2/resolve/main"

# (relative model path, source url, approx bytes, required?)
MODELS = [
    (
        "checkpoints/yue2_3b_int8_convrot.safetensors",
        f"{HF_BASE}/checkpoints/yue2_3b_int8_convrot.safetensors",
        3_960_000_000,
        True,
    ),
    (
        "audio_encoders/sheetsage2_bf16.safetensors",
        f"{HF_BASE}/audio_encoders/sheetsage2_bf16.safetensors",
        1_390_000_000,
        False,  # only needed for Cover mode
    ),
]

# Higher-quality full-precision checkpoint, offered as an option in Settings.
MODEL_BF16 = (
    "checkpoints/yue2_3b_bf16.safetensors",
    f"{HF_BASE}/checkpoints/yue2_3b_bf16.safetensors",
    7_800_000_000,
    False,
)

def clean_url(url: str) -> str:
    """A base URL fit to build requests on.

    No stray whitespace and no trailing slash — appending /system_stats to
    "http://host:8188/" asks for //system_stats, which is a 404, not a health
    check — and never empty.
    """
    return (url or "").strip().rstrip("/") or "http://127.0.0.1:8188"


def comfy_port(url: str) -> int:
    """The port to start ComfyUI on, read out of its URL.

    This used to be int(url.rsplit(":")[-1]), which blew up on a trailing
    slash or a port-less address — typed once into Settings, that config
    stopped the server from booting at all.
    """
    try:
        port = urlsplit(clean_url(url)).port
    except ValueError:
        port = None
    return port or 8188


DEFAULT_CONFIG = {
    "comfy_url": "http://127.0.0.1:8188",
    "comfy_dir": "",       # ComfyUI root (managed or existing)
    "models_dir": "",      # ComfyUI/models
    "python": "",          # interpreter used to run ComfyUI
    "managed": True,       # did we install ComfyUI ourselves?
    "auto_start_comfy": True,
    "torch_index": "",     # e.g. https://download.pytorch.org/whl/cu128 ("" = auto)
    "download_cover_model": True,
    "download_bf16": False,
    "setup_complete": False,
}


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    # Heal a URL saved before it was normalised — a trailing slash in here
    # used to keep the whole app from starting.
    cfg["comfy_url"] = clean_url(cfg.get("comfy_url", ""))
    return cfg


def save_config(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# progress reporting
# --------------------------------------------------------------------------- #
class Progress:
    """Thread-safe setup state that the UI polls."""

    STEPS = [
        ("python", "Check Python"),
        ("comfyui", "Install ComfyUI"),
        ("deps", "Install dependencies"),
        ("models", "Download models"),
        ("launch", "Start ComfyUI"),
    ]

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.lines: list[str] = []
        self.running = False
        self.done = False
        self.error: str | None = None
        self.step = ""
        self.steps = {k: {"label": v, "state": "pending", "detail": ""}
                      for k, v in self.STEPS}

    def log(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self.lines.append(f"[{stamp}] {msg}")
            if len(self.lines) > 4000:
                del self.lines[:2000]
        print(f"[setup] {msg}", flush=True)

    def begin(self, key: str, detail: str = "") -> None:
        with self._lock:
            self.step = key
            self.steps[key]["state"] = "running"
            self.steps[key]["detail"] = detail

    def detail(self, key: str, detail: str) -> None:
        with self._lock:
            self.steps[key]["detail"] = detail

    def finish(self, key: str, detail: str = "") -> None:
        with self._lock:
            self.steps[key]["state"] = "done"
            if detail:
                self.steps[key]["detail"] = detail

    def fail(self, key: str, detail: str) -> None:
        with self._lock:
            self.steps[key]["state"] = "error"
            self.steps[key]["detail"] = detail

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "done": self.done,
                "error": self.error,
                "step": self.step,
                "steps": json.loads(json.dumps(self.steps)),
                "cursor": len(self.lines),
                "lines": self.lines[since:],
            }


# --------------------------------------------------------------------------- #
# python / git discovery
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def find_python(prog: Progress | None = None) -> str:
    """Return a real Python 3.10+ interpreter.

    Windows Store stubs answer `where python` but fail on execution, so every
    candidate is tested by actually running it.
    """
    candidates: list[list[str]] = [[sys.executable]]
    if platform.system() == "Windows":
        candidates += [["py", "-3.12"], ["py", "-3.11"], ["py", "-3.10"],
                       ["py", "-3"], ["python"]]
    else:
        candidates += [["python3.12"], ["python3.11"], ["python3.10"],
                       ["python3"], ["python"]]

    for cand in candidates:
        try:
            out = _run(cand + ["-c", "import sys;print(sys.executable);"
                                     "print('%d.%d' % sys.version_info[:2])"],
                       timeout=25)
        except Exception:
            continue
        if out.returncode != 0:
            continue
        parts = [p.strip() for p in out.stdout.strip().splitlines() if p.strip()]
        if len(parts) < 2 or not parts[0]:
            continue
        try:
            major, minor = (int(x) for x in parts[1].split("."))
        except ValueError:
            continue
        if (major, minor) >= (3, 10):
            if prog:
                prog.log(f"Using Python {parts[1]} at {parts[0]}")
            return parts[0]
    raise RuntimeError(
        "No Python 3.10 or newer found. Install Python from python.org "
        "(tick 'Add to PATH') and run setup again."
    )


def have_git() -> bool:
    return shutil.which("git") is not None


# --------------------------------------------------------------------------- #
# ComfyUI discovery
# --------------------------------------------------------------------------- #
def detect_comfy_dirs() -> list[str]:
    """Common locations for an existing ComfyUI install, portable or Desktop."""
    home = Path.home()
    cands = [
        APP_DIR / "ComfyUI",
        home / "ComfyUI",
        home / "Documents" / "ComfyUI",
        home / "Desktop" / "ComfyUI",
        Path("C:/ComfyUI"),
        Path("C:/ComfyUI_windows_portable/ComfyUI"),
        Path("D:/ComfyUI"),
        Path("D:/ComfyUI_windows_portable/ComfyUI"),
    ]
    appdata = os.environ.get("APPDATA")
    localappdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        cands += [Path(appdata) / "ComfyUI"]
    if localappdata:
        cands += [Path(localappdata) / "Programs" / "@comfyorgcomfyui-electron"
                  / "resources" / "ComfyUI"]
    cands += [home / "AppData" / "Local" / "Programs" / "ComfyUI" / "resources"
              / "ComfyUI"]

    found = []
    for c in cands:
        try:
            if (c / "main.py").exists() or (c / "models").is_dir():
                found.append(str(c))
        except OSError:
            continue
    # de-duplicate, preserve order
    seen, out = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def venv_python(comfy_dir: Path) -> Path:
    venv = comfy_dir.parent / "comfy-venv"
    return venv / ("Scripts/python.exe" if platform.system() == "Windows"
                   else "bin/python")


def _interpreters(comfy_dir: Path) -> list[Path]:
    """Where a ComfyUI install keeps the interpreter it runs on."""
    win = platform.system() == "Windows"
    exe = "Scripts/python.exe" if win else "bin/python"
    cands = [comfy_dir / "venv" / exe,
             comfy_dir / ".venv" / exe,
             comfy_dir.parent / "venv" / exe,
             comfy_dir.parent / ".venv" / exe,
             venv_python(comfy_dir)]
    if win:
        # ComfyUI_windows_portable ships python_embeded next to the ComfyUI
        # folder; the Desktop build keeps a standalone runtime instead.
        cands += [comfy_dir.parent / "python_embeded" / "python.exe",
                  comfy_dir.parent / "python_standalone" / "python.exe"]
    else:
        cands += [comfy_dir.parent / "python_standalone" / "bin" / "python"]
    return cands


def existing_python(comfy_dir: Path) -> str:
    """The interpreter an existing ComfyUI already runs on, if we can find it.

    Tested by running it, never by its path alone — the same rule as
    find_python(). An install whose environment has torch wins outright; a
    working interpreter without torch is the fallback, because it is still that
    install's own environment and ours has no business replacing it.
    """
    fallback = ""
    for cand in _interpreters(comfy_dir):
        try:
            if not cand.exists():
                continue
            out = _run([str(cand), "-c", "import importlib.util as u;"
                                         "print(bool(u.find_spec('torch')))"],
                       timeout=30)
        except Exception:
            continue
        if out.returncode != 0:
            continue
        if out.stdout.strip().splitlines()[-1:] == ["True"]:
            return str(cand)
        fallback = fallback or str(cand)
    return fallback


# --------------------------------------------------------------------------- #
# downloads
# --------------------------------------------------------------------------- #
def download(url: str, dest: Path, prog: Progress, key: str,
             label: str, expected: int = 0) -> None:
    """Resumable streaming download with a .part file and atomic rename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}

    with requests.get(url, headers=headers, stream=True, timeout=60,
                      allow_redirects=True) as r:
        if r.status_code == 416:  # already complete
            part.replace(dest)
            return
        r.raise_for_status()
        # A 206 means the server honoured the Range header and Content-Length
        # covers only what is left; a 200 means it ignored it and is sending the
        # whole file again, so anything already on disk does not count.
        resuming = bool(have) and r.status_code == 206
        mode = "ab" if resuming else "wb"
        if not resuming:
            have = 0
        total = int(r.headers.get("Content-Length", 0)) + have or expected
        got = have
        last = 0.0
        with open(part, mode) as fh:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                fh.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - last > 0.8:
                    last = now
                    pct = (got / total * 100) if total else 0
                    prog.detail(key, f"{label} — {got/1e9:.2f} GB "
                                     f"of {total/1e9:.2f} GB ({pct:.0f}%)")
    part.replace(dest)
    prog.log(f"Downloaded {dest.name} ({dest.stat().st_size/1e9:.2f} GB)")


# --------------------------------------------------------------------------- #
# ComfyUI process
# --------------------------------------------------------------------------- #
class ComfyProcess:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lines: list[str] = []
        self._lock = threading.Lock()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, python: str, comfy_dir: Path, port: int,
              prog: Progress) -> None:
        """Raises RuntimeError with a sentence a person can act on. A ComfyUI
        folder that has moved, or an interpreter that is gone, is an engine
        that cannot start — never a reason the whole app fails to boot."""
        if self.alive():
            return
        if not (comfy_dir / "main.py").exists():
            raise RuntimeError(
                f"There is no ComfyUI at {comfy_dir} any more — the folder "
                "has moved or been deleted. Run setup again from Settings.")
        cmd = [python, "main.py", "--listen", "127.0.0.1", "--port", str(port),
               "--disable-auto-launch"]
        prog.log("Launching ComfyUI: " + " ".join(cmd))
        creation = 0
        if platform.system() == "Windows":
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(comfy_dir), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                creationflags=creation,
            )
        except OSError as exc:
            raise RuntimeError(
                f"ComfyUI could not be started with {python} — {exc}. "
                "Run setup again from Settings.") from exc
        threading.Thread(target=self._pump, args=(prog,), daemon=True).start()

    def _pump(self, prog: Progress) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip()
            with self._lock:
                self.lines.append(line)
                if len(self.lines) > 2000:
                    del self.lines[:1000]
            if any(k in line for k in ("Error", "Traceback", "error:",
                                       "Starting server", "To see the GUI")):
                prog.log(f"ComfyUI: {line}")

    def tail(self, n: int = 40) -> list[str]:
        with self._lock:
            return self.lines[-n:]

    def stop(self) -> None:
        if self.alive():
            try:
                self.proc.terminate()
                self.proc.wait(timeout=15)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


def wait_for_comfy(url: str, timeout: int = 900) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{url}/system_stats", timeout=4)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def comfy_online(url: str) -> bool:
    try:
        return requests.get(f"{url}/system_stats", timeout=3).status_code == 200
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# the setup run
# --------------------------------------------------------------------------- #
def missing_models(models_dir: Path, cfg: dict) -> list[tuple]:
    wanted = [m for m in MODELS if m[3] or
              (m[0].startswith("audio_encoders") and cfg.get("download_cover_model"))]
    if cfg.get("download_bf16"):
        wanted.append(MODEL_BF16)
    return [m for m in wanted if not (models_dir / m[0]).exists()]


def run_setup(cfg: dict, prog: Progress, comfy: ComfyProcess,
              chosen_dir: str = "", mode: str = "auto") -> None:
    """Executed on a worker thread. mode: auto | existing | managed | external."""
    prog.running = True
    prog.done = False
    prog.error = None
    try:
        # ---- 1. python -------------------------------------------------- #
        prog.begin("python")
        if mode == "external":
            prog.finish("python", "Not needed — using a ComfyUI you run yourself")
            py = cfg.get("python") or sys.executable
        else:
            py = find_python(prog)
            cfg["python"] = py
            prog.finish("python", py)

        # ---- 2. comfyui ------------------------------------------------- #
        prog.begin("comfyui")
        if mode == "external":
            url = cfg["comfy_url"]
            prog.log(f"Using ComfyUI already running at {url}")
            if not comfy_online(url):
                raise RuntimeError(
                    f"Nothing is answering at {url}. Start ComfyUI first, or "
                    f"switch to a managed install."
                )
            cfg["managed"] = False
            if not cfg.get("models_dir"):
                raise RuntimeError(
                    "Set the ComfyUI models folder in Settings so the model "
                    "files land in the right place."
                )
            prog.finish("comfyui", url)
        else:
            if chosen_dir:
                comfy_dir = Path(chosen_dir)
                cfg["managed"] = False
                prog.log(f"Using existing ComfyUI at {comfy_dir}")
                # Their install runs on its own interpreter. Recording the one
                # we happened to find on PATH would start ComfyUI without the
                # torch it needs, and would aim any later dependency install at
                # the wrong environment.
                own = existing_python(comfy_dir)
                if own:
                    cfg["python"] = own
                    prog.log(f"That install runs on {own}")
                else:
                    cfg["python"] = ""
                    prog.log("Could not find the Python environment that "
                             "install uses — start ComfyUI yourself and "
                             "YuE Studio will connect to it.")
            else:
                comfy_dir = APP_DIR / "ComfyUI"
                cfg["managed"] = True
                if not (comfy_dir / "main.py").exists():
                    if not have_git():
                        raise RuntimeError(
                            "Git is not installed, so ComfyUI cannot be "
                            "downloaded. Install Git, or point YuE Studio at an "
                            "existing ComfyUI folder in Settings."
                        )
                    prog.detail("comfyui", "Cloning ComfyUI from GitHub…")
                    prog.log("git clone " + COMFY_REPO)
                    res = _run(["git", "clone", "--depth", "1", COMFY_REPO,
                                str(comfy_dir)])
                    if res.returncode != 0:
                        raise RuntimeError("git clone failed: " +
                                           (res.stderr or res.stdout)[-600:])
                else:
                    prog.detail("comfyui", "Updating ComfyUI…")
                    _run(["git", "-C", str(comfy_dir), "pull", "--ff-only"])
            if not (comfy_dir / "main.py").exists():
                raise RuntimeError(f"No main.py in {comfy_dir}. "
                                   "That folder is not a ComfyUI install.")
            cfg["comfy_dir"] = str(comfy_dir)
            cfg["models_dir"] = str(comfy_dir / "models")
            prog.finish("comfyui", str(comfy_dir))

        models_dir = Path(cfg["models_dir"])

        # ---- 3. dependencies -------------------------------------------- #
        prog.begin("deps")
        if mode == "external":
            prog.finish("deps", "Managed by your own ComfyUI install")
        elif cfg["managed"]:
            comfy_dir = Path(cfg["comfy_dir"])
            vpy = venv_python(comfy_dir)
            if not vpy.exists():
                prog.detail("deps", "Creating virtual environment…")
                prog.log(f"Creating venv at {vpy.parent.parent / 'comfy-venv'}")
                res = _run([py, "-m", "venv", str(comfy_dir.parent / "comfy-venv")])
                if res.returncode != 0:
                    raise RuntimeError("venv creation failed: " +
                                       (res.stderr or res.stdout)[-600:])
            index = cfg.get("torch_index") or _torch_index()
            prog.detail("deps", "Installing PyTorch (this takes a while)…")
            _pip(vpy, ["install", "--upgrade", "pip", "wheel"], prog)
            torch_args = ["install", "torch", "torchaudio", "torchvision"]
            if index:
                torch_args += ["--index-url", index]
            _pip(vpy, torch_args, prog)
            prog.detail("deps", "Installing ComfyUI requirements…")
            _pip(vpy, ["install", "-r", str(comfy_dir / "requirements.txt")], prog)
            cfg["python"] = str(vpy)
            prog.finish("deps", "PyTorch and ComfyUI requirements installed")
        else:
            # Existing install: assume its own environment already works.
            prog.finish("deps", "Existing ComfyUI environment left untouched")

        # ---- 4. models --------------------------------------------------- #
        prog.begin("models")
        todo = missing_models(models_dir, cfg)
        if not todo:
            prog.finish("models", "All model files already present")
        else:
            gb = sum(m[2] for m in todo) / 1e9
            prog.log(f"{len(todo)} model file(s) to download, about {gb:.1f} GB")
            for rel, url, size, _req in todo:
                prog.detail("models", f"Downloading {Path(rel).name}…")
                download(url, models_dir / rel, prog, "models",
                         Path(rel).name, size)
            prog.finish("models", "Models ready")

        # ---- 5. launch ---------------------------------------------------- #
        prog.begin("launch")
        url = cfg["comfy_url"]
        if mode == "external" or not cfg.get("auto_start_comfy", True):
            if not comfy_online(url):
                raise RuntimeError(f"ComfyUI is not answering at {url}.")
        else:
            port = comfy_port(url)
            if comfy_online(url):
                prog.log(f"ComfyUI already running on port {port}")
            elif not cfg.get("python"):
                raise RuntimeError(
                    f"Start ComfyUI yourself and make sure it answers at {url}. "
                    "YuE Studio could not work out which Python that install "
                    "uses, so it will not try to start it for you.")
            else:
                comfy.start(cfg["python"], Path(cfg["comfy_dir"]), port, prog)
                prog.detail("launch", "Waiting for ComfyUI to come up "
                                      "(first start loads slowly)…")
                if not wait_for_comfy(url, timeout=900):
                    tail = "\n".join(comfy.tail(25))
                    raise RuntimeError("ComfyUI did not start within 15 minutes."
                                       f"\nLast output:\n{tail}")
        prog.finish("launch", url)

        cfg["setup_complete"] = True
        save_config(cfg)
        prog.done = True
        prog.log("Setup complete. YuE Studio is ready.")
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        prog.error = str(exc)
        if prog.step:
            prog.fail(prog.step, str(exc))
        prog.log(f"FAILED: {exc}")
    finally:
        prog.running = False


def _torch_index() -> str:
    """Pick a sensible torch wheel index for this machine."""
    system = platform.system()
    if system == "Darwin":
        return ""  # CPU/MPS wheels from PyPI
    if _has_nvidia():
        return "https://download.pytorch.org/whl/cu128"
    if system == "Linux":
        return "https://download.pytorch.org/whl/cpu"
    return "https://download.pytorch.org/whl/cpu"


def _has_nvidia() -> bool:
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        return _run(["nvidia-smi"], timeout=20).returncode == 0
    except Exception:
        return False


def _pip(python: Path | str, args: list[str], prog: Progress) -> None:
    cmd = [str(python), "-m", "pip"] + args
    prog.log("pip " + " ".join(args[:4]))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout
    for line in proc.stdout:
        line = line.rstrip()
        if line.startswith(("Collecting", "Downloading", "Installing",
                            "Successfully", "ERROR", "Building")):
            prog.log(line[:200])
    if proc.wait() != 0:
        raise RuntimeError("pip " + " ".join(args[:3]) + " failed — see the log.")
