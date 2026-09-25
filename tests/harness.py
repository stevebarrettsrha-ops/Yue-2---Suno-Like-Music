"""Shared plumbing for the suite: reporting, and disposable servers.

Every test gets its own port and its own data directory. Nothing here touches
the library or config of a real install, and two runs cannot collide.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import requests

# The app's own modules live a directory up. Put that on the path here rather
# than relying on whichever test module happened to be imported first: without
# it a group that only adds tests/ — `python tests/run.py ui` — dies on this
# import before a single check runs.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap                                    # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MOCK = Path(__file__).resolve().parent / "mock_comfy.py"
# The suite talks to servers on this machine; a proxy in the environment would
# swallow every request.
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
CURRENT: "Suite | None" = None


class Suite:
    """Collects checks so one failure does not hide the rest."""

    def __init__(self, name: str) -> None:
        global CURRENT
        self.name = name
        self.passed = 0
        self.failures: list[str] = []
        # The runner reaches for this if the module dies part way through, so
        # the checks that already ran are still reported.
        CURRENT = self

    def check(self, what: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  ok   {what}" + (f" — {detail}" if detail else ""))
        else:
            self.failures.append(what)
            print(f"  FAIL {what}" + (f" — {detail}" if detail else ""))
        return ok

    def equal(self, what: str, got, want) -> bool:
        return self.check(what, got == want, f"got {got!r}, wanted {want!r}")

    def fails_with(self, what: str, fn, expect: type = Exception,
                   contains: str = "") -> bool:
        """The call must raise, and say something useful when it does."""
        try:
            fn()
        except expect as exc:
            return self.check(what, contains.lower() in str(exc).lower(),
                              f"said {str(exc)[:70]!r}")
        except Exception as exc:  # noqa: BLE001
            return self.check(what, False, f"raised {type(exc).__name__}: {exc}")
        return self.check(what, False, "did not raise at all")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- #
# disposable servers
# --------------------------------------------------------------------------- #
class Server:
    """A subprocess that is always cleaned up, however the test ends."""

    def __init__(self, argv: list[str], port: int, ready_path: str,
                 env: dict | None = None, cwd: Path = ROOT) -> None:
        self.argv, self.port, self.ready_path = argv, port, ready_path
        self.env, self.cwd = env or {}, cwd
        self.proc: subprocess.Popen | None = None
        self.url = f"http://127.0.0.1:{port}"
        self.log = Path(tempfile.mkstemp(suffix=".log")[1])

    def __enter__(self) -> "Server":
        self.proc = subprocess.Popen(
            self.argv, cwd=str(self.cwd), env={**os.environ, **self.env},
            stdout=self.log.open("w"), stderr=subprocess.STDOUT,
            start_new_session=True)
        for _ in range(120):
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.argv[-1]} died at startup:\n"
                                   f"{self.log.read_text()[-2000:]}")
            try:
                requests.get(self.url + self.ready_path, timeout=2)
                return self
            except Exception:
                time.sleep(0.25)
        raise RuntimeError(f"{self.argv[-1]} never answered on {self.port}:\n"
                           f"{self.log.read_text()[-2000:]}")

    def __exit__(self, *exc) -> None:
        self.stop()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass

    def tail(self, lines: int = 25) -> str:
        return "\n".join(self.log.read_text().splitlines()[-lines:])


def llm() -> Server:
    """A stand-in chat server for the lyric writer (tests/mock_llm.py)."""
    port = free_port()
    return Server([sys.executable, str(MOCK.with_name("mock_llm.py")), str(port)],
                  port, "/_last")


def comfy(delay: float = 2.0, **env) -> Server:
    """A stand-in ComfyUI. delay is how long a song takes to 'render'."""
    port = free_port()
    return Server([sys.executable, str(MOCK), str(port)], port, "/system_stats",
                  env={"MOCK_DELAY": str(delay), **env})


def safetensors_stub(path: Path, size: int) -> None:
    """A real, complete safetensors file of exactly `size` bytes.

    Completeness is judged from the header a safetensors file carries about
    itself, so a block of zeros no longer stands in for a model — it has no
    header and reads as the truncated download it looks like. This writes a
    genuine one (8-byte length, JSON naming one tensor, then its data) and
    leaves the body sparse, so the file reports gigabytes without consuming
    them.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    head_len = 256                       # fixed, so the arithmetic closes
    count = size - 8 - head_len
    meta = ('{"t":{"dtype":"U8","shape":[%d],"data_offsets":[0,%d]}}'
            % (count, count))
    head = meta.encode() + b" " * (head_len - len(meta))   # JSON ignores the pad
    with path.open("wb") as handle:
        handle.write(len(head).to_bytes(8, "little"))
        handle.write(head)
        handle.truncate(size)


def fake_weights(models_dir: Path) -> None:
    """Drop the model files where missing_models() looks for them."""
    for rel, _url, size, _required in bootstrap.MODELS:
        safetensors_stub(models_dir / rel, size)


def fake_install(root: Path, stale_first_boot: bool = False) -> Path:
    """A pretend ComfyUI checkout whose main.py serves the stand-in engine.

    This is what lets the app really own, stop and restart an engine process
    in a test: the takeover and boot paths run against a live child, not a
    mock of one. Its /system_stats names this folder, as a real ComfyUI's
    argv does, so the app can tell it apart from someone else's install.

    With stale_first_boot the FIRST launch serves an empty checkpoint list —
    an engine that started before the weights landed — and every later launch
    serves the full one, which is exactly what ComfyUI's once-at-startup model
    scan looks like from outside.
    """
    install = root / "ComfyUI"
    install.mkdir(parents=True, exist_ok=True)
    (install / "main.py").write_text(textwrap.dedent(f"""\
        import os, pathlib, runpy, sys
        here = pathlib.Path(__file__).resolve().parent
        port = sys.argv[sys.argv.index("--port") + 1]
        # A real ComfyUI's argv names the main.py it was launched from; the
        # stand-in reads this to answer /system_stats the same way.
        os.environ["MOCK_COMFY_ROOT"] = str(here)
        flag = here / "stale.flag"
        if flag.exists():
            os.environ["MOCK_BLANK_CKPT_CALLS"] = "999999"
            flag.unlink()
            print("model scan found no checkpoints", flush=True)
        else:
            print("model scan found the YuE2 checkpoints", flush=True)
        print("Starting server", flush=True)
        sys.argv = ["mock_comfy.py", port]
        runpy.run_path({str(MOCK)!r}, run_name="__main__")
    """))
    if stale_first_boot:
        (install / "stale.flag").write_text("first boot is a stale scan")
    fake_weights(install / "models")
    return install


def supervised_comfy(delay: float = 1.0) -> Server:
    """A stand-in engine under a parent that restarts it whenever it dies.

    ComfyUI Desktop and every launcher script behave like this, and it is the
    case a plain "stop the process" quietly loses to: the port is free for
    half a second and then taken again under a new pid.
    """
    port = free_port()
    script = Path(tempfile.mkstemp(suffix="_keeper.py")[1])
    script.write_text(textwrap.dedent(f"""\
        import subprocess, sys, time
        while True:
            child = subprocess.Popen([sys.executable, {str(MOCK)!r}, sys.argv[1]])
            child.wait()
            time.sleep(0.3)
    """))
    return Server([sys.executable, str(script), str(port)], port,
                  "/system_stats", env={"MOCK_DELAY": str(delay)})


def port_squatter() -> Server:
    """Something that answers /system_stats but is not ComfyUI at all.

    A forwarded port — docker-proxy, an ssh tunnel, a reverse proxy — looks
    exactly like this: the engine check passes and the process holding the
    socket is nothing of the sort. It must never be stopped, so it is run
    through a link whose name says nothing about python or ComfyUI, the way
    the real ones do.
    """
    port = free_port()
    home = Path(tempfile.mkdtemp(prefix="port-squatter-"))
    (home / "serve.script").write_text(textwrap.dedent("""\
        import sys
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def _reply(self, code):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
            def do_GET(self): self._reply(200)
            def do_POST(self): self._reply(404)
            def log_message(self, *a): pass
        ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])),
                            Handler).serve_forever()
    """))
    link = home / "webthing"
    os.symlink(sys.executable, link)
    return Server([str(link), str(home / "serve.script"), str(port)], port,
                  "/system_stats")


def studio(comfy_url: str, data: Path, **config) -> Server:
    """YuE Studio itself, with its own data folder and a config to match."""
    import json
    data.mkdir(parents=True, exist_ok=True)
    (data / "config.json").write_text(json.dumps({
        "comfy_url": comfy_url, "comfy_dir": "", "models_dir": "", "python": "",
        "managed": True, "auto_start_comfy": False, "torch_index": "",
        "download_cover_model": True, "download_bf16": False,
        "setup_complete": True, **config}))
    port = free_port()
    return Server([sys.executable, "server.py"], port, "/api/status",
                  env={"YUE_STUDIO_PORT": str(port), "YUE_STUDIO_NO_BROWSER": "1",
                       "YUE_STUDIO_DATA": str(data)})


class Workspace:
    """A throwaway data directory, removed when the test finishes."""

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix="yue-test-"))
        return self.path

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def wait_for(condition, timeout: float = 30, step: float = 0.5) -> bool:
    """Poll until it is true, rather than sleeping and hoping."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(step)
    return False


def finish_jobs(app_url: str, timeout: float = 60) -> list[dict]:
    """Wait for every running song to stop running, then report them all."""
    wait_for(lambda: not [j for j in requests.get(f"{app_url}/api/jobs",
                                                  timeout=10).json()
                          if j["status"] == "running"], timeout)
    return requests.get(f"{app_url}/api/jobs", timeout=10).json()
