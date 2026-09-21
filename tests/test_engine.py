"""The engine itself: its console, its restart, and the port it lives on.

Everything here runs against real processes. A ComfyUI that is taken over, a
supervisor that puts it back, a port held by something that is not ComfyUI at
all — none of that can be proved with a stub, because what is being tested is
whether the app finds, names and stops the right operating-system process.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import bootstrap  # noqa: E402  (the app's own, for reading pids off a port)
from harness import (Suite, Workspace, comfy, fake_install,  # noqa: E402
                     fake_weights, free_port, port_squatter, studio,
                     supervised_comfy, wait_for)


def status(app_url: str) -> dict:
    return requests.get(f"{app_url}/api/status", timeout=10).json()


def engine_log(app_url: str, n: int = 200) -> dict:
    return requests.get(f"{app_url}/api/comfy/log?n={n}", timeout=10).json()


def run(slow: bool = False) -> Suite:
    s = Suite("engine")

    # -- the console, and the four restart routes ---------------------------
    with Workspace() as ws:
        install = fake_install(ws)
        quiet = f"http://127.0.0.1:{free_port()}"
        with studio(quiet, ws / "data", comfy_dir=str(install),
                    models_dir=str(install / "models"),
                    python=sys.executable) as app:
            log = engine_log(app.url, 120)
            s.check("the console answers with lines, running and online",
                    set(log) >= {"lines", "running", "online"}
                    and isinstance(log["lines"], list)
                    and log["running"] is False and log["online"] is False,
                    str(log)[:90])
            s.check("n is clamped rather than trusted",
                    len(engine_log(app.url, 99999)["lines"]) <= 400
                    and isinstance(engine_log(app.url, -5)["lines"], list)
                    and isinstance(requests.get(
                        f"{app.url}/api/comfy/log?n=nonsense",
                        timeout=10).json()["lines"], list))

            r = requests.post(f"{app.url}/api/comfy/restart", timeout=60)
            s.check("restart with nothing there just starts one",
                    r.ok and r.json().get("how") == "started", str(r.json())[:90])
            s.check("and it comes up managed",
                    wait_for(lambda: (lambda st: st["comfy_online"]
                             and st["comfy_running_managed"])(status(app.url)), 45))
            s.check("what the app did to the engine is in the engine's own log",
                    any(line.startswith("[YuE Studio]")
                        for line in engine_log(app.url)["lines"])
                    and any("Starting server" in line
                            for line in engine_log(app.url)["lines"]),
                    str(engine_log(app.url, 20)["lines"])[-120:])

            port = bootstrap.comfy_port(quiet)
            before = bootstrap.port_pids(port)
            r = requests.post(f"{app.url}/api/comfy/restart", timeout=60)
            s.check("restart on our own engine is the plain managed kind",
                    r.ok and r.json().get("how") == "managed", str(r.json())[:90])
            s.check("and it really is a new process, not the same one",
                    wait_for(lambda: (lambda now: bool(now) and now != before)(
                        bootstrap.port_pids(port)), 45),
                    f"was {before}, now {bootstrap.port_pids(port)}")
            s.check("the engine answers again afterwards",
                    wait_for(lambda: status(app.url)["comfy_online"], 45))

    # -- an orphan on the port is closed and replaced -----------------------
    # The dead end this cures: Start said "already running", Restart said "not
    # started by this app", and the person was sent to find a windowless
    # python in Task Manager.
    with comfy(delay=1) as orphan, Workspace() as ws:
        install = fake_install(ws)
        with studio(orphan.url, ws / "data", comfy_dir=str(install),
                    models_dir=str(install / "models"),
                    python=sys.executable) as app:
            st = status(app.url)
            s.check("before: online, but not ours",
                    st["comfy_online"] and not st["comfy_running_managed"])
            r = requests.post(f"{app.url}/api/comfy/restart", timeout=120)
            s.check("restart closes the orphan and puts a managed one there",
                    r.ok and r.json().get("how") == "takeover",
                    str(r.json())[:90])
            s.check("the orphan process is actually gone",
                    wait_for(lambda: orphan.proc.poll() is not None, 20))
            s.check("and the managed engine comes up in its place",
                    wait_for(lambda: (lambda st: st["comfy_online"]
                             and st["comfy_running_managed"])(status(app.url)), 45))
            st = status(app.url)
            s.check("which sees the checkpoints — no stale scan",
                    st.get("stale_models") is False
                    and any("yue2" in c.lower()
                            for c in st.get("checkpoints") or []),
                    str(st.get("checkpoints"))[:80])

    # -- nothing of our own to start: it is still closed, and said so -------
    with comfy(delay=1) as orphan, Workspace() as ws:
        with studio(orphan.url, ws / "data") as app:
            r = requests.post(f"{app.url}/api/comfy/restart", timeout=60)
            body = r.json()
            s.check("with no install of its own, restart still frees the port",
                    r.ok and body.get("how") == "stopped"
                    and "run setup" in (body.get("note") or "").lower(),
                    str(body)[:100])
            s.check("and the foreign engine is really closed",
                    wait_for(lambda: not engine_log(app.url)["online"], 20))

    # -- a supervisor putting it straight back is diagnosed, not shrugged at -
    with supervised_comfy() as keeper, Workspace() as ws:
        install = fake_install(ws)
        with studio(keeper.url, ws / "data", comfy_dir=str(install),
                    models_dir=str(install / "models"),
                    python=sys.executable) as app:
            r = requests.post(f"{app.url}/api/comfy/restart", timeout=240)
            s.check("a respawning engine is named as supervised",
                    r.status_code == 409
                    and "supervising" in (r.json().get("error") or ""),
                    str(r.json())[:120])
            s.check("the advice reached the engine console too",
                    any("supervising" in line
                        for line in engine_log(app.url)["lines"]))

    # -- something that is not ComfyUI is never stopped ---------------------
    # A forwarded port — docker-proxy, an ssh tunnel — answers the health
    # check while the process holding the socket is nothing of the sort. A
    # wrong address in Settings is not a licence to kill whatever is at it.
    with port_squatter() as squatter, Workspace() as ws:
        install = fake_install(ws)
        with studio(squatter.url, ws / "data", comfy_dir=str(install),
                    models_dir=str(install / "models"),
                    python=sys.executable) as app:
            r = requests.post(f"{app.url}/api/comfy/restart", timeout=60)
            said = (r.json().get("error") or "") if not r.ok else ""
            s.check("a process that is not ComfyUI is refused, by name",
                    r.status_code == 409 and "does not look like ComfyUI" in said
                    and "webthing" in said, str(r.json())[:130])
            s.check("and it is left running",
                    squatter.proc.poll() is None)

    # -- the stale scan, named ----------------------------------------------
    # ComfyUI scans its model folders once, at startup. A checkpoint that
    # landed afterwards is on disk and invisible until a restart, which reads
    # as a failed download and sends people to re-fetch four gigabytes.
    with comfy(delay=1, MOCK_BLANK_CKPT_CALLS="9999") as blind, \
            Workspace() as ws:
        models = ws / "models"
        fake_weights(models)
        with studio(blind.url, ws / "data", models_dir=str(models)) as app:
            st = status(app.url)
            s.check("weights on disk and none in the engine's list is stale",
                    st.get("stale_models") is True and st["comfy_online"],
                    str(st.get("checkpoints")))
    with comfy(delay=1) as seeing, Workspace() as ws:
        models = ws / "models"
        fake_weights(models)
        with studio(seeing.url, ws / "data", models_dir=str(models)) as app:
            s.check("an engine that lists them is not stale",
                    status(app.url).get("stale_models") is False)
    with comfy(delay=1, MOCK_BLANK_CKPT_CALLS="9999") as blind, \
            Workspace() as ws:
        empty = ws / "models"
        (empty / "checkpoints").mkdir(parents=True)
        with studio(blind.url, ws / "data", models_dir=str(empty)) as app:
            st = status(app.url)
            s.check("but a file that is genuinely missing is not called stale",
                    st.get("stale_models") is False and st["missing_models"],
                    str(st["missing_models"]))

    # -- which install is answering the address -----------------------------
    with comfy(delay=1, MOCK_COMFY_ROOT="/opt/somebody-elses/ComfyUI") as other, \
            Workspace() as ws:
        with studio(other.url, ws / "data",
                    comfy_dir="/opt/mine/ComfyUI") as app:
            st = status(app.url)
            s.check("a different install on the address is called out",
                    st.get("engine_mismatch") is True
                    and "somebody-elses" in (st.get("engine_argv") or ""),
                    str(st.get("engine_argv")))
    with comfy(delay=1, MOCK_COMFY_ROOT="/opt/mine/ComfyUI") as ours, \
            Workspace() as ws:
        with studio(ours.url, ws / "data",
                    comfy_dir="/opt/mine/ComfyUI") as app:
            s.check("the right one is not",
                    status(app.url).get("engine_mismatch") is False)

    # -- a launch ends with a working engine, unprompted --------------------
    with Workspace() as ws:
        install = fake_install(ws)
        quiet = f"http://127.0.0.1:{free_port()}"
        with studio(quiet, ws / "data", comfy_dir=str(install),
                    models_dir=str(install / "models"),
                    python=sys.executable, auto_start_comfy=True) as app:
            s.check("a quiet port: the launch starts one, with no clicks",
                    wait_for(lambda: (lambda st: st["comfy_online"]
                             and st["comfy_running_managed"])(status(app.url)), 60))
            st = status(app.url)
            s.check("and it comes up seeing the checkpoints",
                    st["ready"] and st.get("stale_models") is False)

    with Workspace() as ws:
        install = fake_install(ws)
        with comfy(delay=1, MOCK_COMFY_ROOT=str(install)) as healthy:
            with studio(healthy.url, ws / "data", comfy_dir=str(install),
                        models_dir=str(install / "models"),
                        python=sys.executable, auto_start_comfy=True) as app:
                s.check("a healthy engine already there is adopted, and said so",
                        wait_for(lambda: any(
                            "Adopting" in line
                            for line in engine_log(app.url)["lines"]), 40))
                st = status(app.url)
                s.check("adopted means left alone, not killed and replaced",
                        st["comfy_online"] and not st["comfy_running_managed"]
                        and healthy.proc.poll() is None)

    with Workspace() as ws:
        install = fake_install(ws)
        with comfy(delay=1, MOCK_COMFY_ROOT=str(install),
                   MOCK_BLANK_CKPT_CALLS="9999") as stale:
            with studio(stale.url, ws / "data", comfy_dir=str(install),
                        models_dir=str(install / "models"),
                        python=sys.executable, auto_start_comfy=True,
                        managed=True) as app:
                s.check("a stale engine already there is replaced by the launch",
                        wait_for(lambda: (lambda st: st["comfy_running_managed"]
                                 and st["comfy_online"]
                                 and st.get("stale_models") is False)(
                                     status(app.url)), 90))
                lines = "\n".join(engine_log(app.url)["lines"])
                s.check("and the console narrates the whole takeover",
                        "Replacing it" in lines and "Stopping pid" in lines,
                        lines[-160:].replace("\n", " | "))

    # -- an engine the person runs themselves is never taken over -----------
    # External mode: managed false. It is told what is wrong and left alone.
    with Workspace() as ws:
        install = fake_install(ws)
        with comfy(delay=1, MOCK_COMFY_ROOT=str(install),
                   MOCK_BLANK_CKPT_CALLS="9999") as theirs:
            with studio(theirs.url, ws / "data", comfy_dir=str(install),
                        models_dir=str(install / "models"),
                        python=sys.executable, auto_start_comfy=True,
                        managed=False) as app:
                s.check("an external engine's problems are reported, not fixed",
                        wait_for(lambda: any(
                            "it is yours, not YuE Studio's" in line
                            for line in engine_log(app.url)["lines"]), 40))
                s.check("and the process is still theirs to run",
                        theirs.proc.poll() is None
                        and not status(app.url)["comfy_running_managed"])

    return s


if __name__ == "__main__":
    suite = run("--slow" in sys.argv)
    print(f"\n{suite.name}: {suite.passed} passed, {len(suite.failures)} failed")
    sys.exit(1 if suite.failures else 0)
