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
import time
from pathlib import Path

import requests

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


def comfy(delay: float = 2.0, **env) -> Server:
    """A stand-in ComfyUI. delay is how long a song takes to 'render'."""
    port = free_port()
    return Server([sys.executable, str(MOCK), str(port)], port, "/system_stats",
                  env={"MOCK_DELAY": str(delay), **env})


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
