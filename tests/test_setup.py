"""bootstrap.py and manager.py — finding Python, addresses, and model files."""

from __future__ import annotations

import io
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bootstrap                                   # noqa: E402
import manager                                     # noqa: E402
from harness import Suite, Workspace, free_port    # noqa: E402

# A stand-in interpreter: answers the probes find_python and existing_python run.
STUB = """#!/bin/sh
case "$2" in
  *find_spec*)  echo %(torch)s ;;
  *"import torch"*)
      [ "%(torch)s" = "True" ] || exit 1
      echo '{"v": "2.6.0+cu128", "cuda": true, "dev": "RTX 4090"}' ;;
  *safetensors*) [ "%(torch)s" = "True" ] || exit 1; echo ok ;;
  *) echo unknown ;;
esac
exit 0
"""


def run(slow: bool = False) -> Suite:
    s = Suite("setup")

    # -- addresses people type, and the port we start ComfyUI on -----------
    for raw, url, port in [
            ("http://127.0.0.1:8188/", "http://127.0.0.1:8188", 8188),
            ("http://127.0.0.1:8188", "http://127.0.0.1:8188", 8188),
            ("localhost:9000", "http://localhost:9000", 9000),
            ("  http://a:9000//  ", "http://a:9000", 9000),
            ("https://box.lan:8188/", "https://box.lan:8188", 8188),
            ("http://[::1]:8188/", "http://[::1]:8188", 8188),
            ("http://192.168.1.5", "http://192.168.1.5", 8188)]:
        s.check(f"{raw.strip()!r} is usable", bootstrap.clean_url(raw) == url,
                repr(bootstrap.clean_url(raw)))
        s.check(f"{raw.strip()!r} starts ComfyUI on {port}",
                bootstrap.comfy_port(raw) == port, str(bootstrap.comfy_port(raw)))
    for junk in (8188, None, [], {"a": 1}, True, "", "   ", "not a url",
                 "file:///etc"):
        s.check(f"{junk!r} is refused rather than half-applied",
                bootstrap.clean_url(junk) == "")
    s.check("a port-less address still yields a port to start on",
            bootstrap.comfy_port("http://host") == 8188)

    # -- an existing ComfyUI runs on its own interpreter, not ours ---------
    layouts = [("a venv inside ComfyUI", "venv/bin/python", True),
               ("a uv .venv inside", ".venv/bin/python", True),
               ("a sibling standalone runtime",
                "../python_standalone/bin/python", True),
               ("a sibling venv", "../venv/bin/python", True),
               ("an environment with no torch", "venv/bin/python", False),
               ("no environment we can find", None, False)]
    for label, rel, has_torch in layouts:
        with Workspace() as root:
            comfy_dir = root / "ComfyUI"
            (comfy_dir / "models" / "checkpoints").mkdir(parents=True)
            (comfy_dir / "models" / "audio_encoders").mkdir(parents=True)
            (comfy_dir / "main.py").write_text("# theirs\n")
            (comfy_dir / "requirements.txt").write_text("safetensors\n")
            for rel_model, *_ in bootstrap.MODELS:
                (comfy_dir / "models" / rel_model).write_bytes(b"0")
            theirs = None
            if rel:
                theirs = Path(os.path.normpath(comfy_dir / rel))
                theirs.parent.mkdir(parents=True, exist_ok=True)
                theirs.write_text(STUB % {"torch": "True" if has_torch else "False"})
                theirs.chmod(0o755)

            found = bootstrap.existing_python(comfy_dir)
            s.check(f"{label}: the install's own interpreter is found",
                    found == (str(theirs) if theirs else ""), found or "(none)")

            cfg = {**bootstrap.DEFAULT_CONFIG, "managed": False,
                   "comfy_dir": str(comfy_dir),
                   "models_dir": str(comfy_dir / "models"),
                   "comfy_url": "http://127.0.0.1:1", "python": ""}
            deps = {d["id"]: d for d in manager.dependencies(cfg)}
            s.check(f"{label}: no Install button for an install we did not make",
                    deps["torch"]["action"] is None
                    and deps["comfy_reqs"]["action"] is None)
            for dep in ("torch", "comfy_reqs"):
                s.fails_with(f"{label}: installing {dep} into it is refused",
                             lambda d=dep: manager.install_dependency(d, cfg, {}),
                             RuntimeError, "not installed by YuE Studio")

    # -- a managed install is still ours to set up -------------------------
    with Workspace() as root:
        comfy_dir = root / "ComfyUI"
        (comfy_dir / "models").mkdir(parents=True)
        (comfy_dir / "main.py").write_text("#\n")
        (comfy_dir / "requirements.txt").write_text("safetensors\n")
        cfg = {**bootstrap.DEFAULT_CONFIG, "managed": True,
               "comfy_dir": str(comfy_dir), "models_dir": str(comfy_dir / "models"),
               "comfy_url": "http://127.0.0.1:1"}
        deps = {d["id"]: d for d in manager.dependencies(cfg)}
        s.check("a managed install still offers to install PyTorch",
                deps["torch"]["action"] == "install"
                and deps["comfy_reqs"]["action"] == "install")

    # -- choosing a models folder has to reach ComfyUI ---------------------
    # Setting it used to move only where files were downloaded and looked for;
    # ComfyUI went on reading its own folder, so the setting looked applied
    # while songs still could not find a model.
    with Workspace() as root:
        comfy_dir = root / "ComfyUI"
        (comfy_dir / "models").mkdir(parents=True)
        elsewhere = root / "my-models"
        (elsewhere / "checkpoints").mkdir(parents=True)

        s.check("ComfyUI's own folder needs no extra-paths file",
                bootstrap.model_paths_file(comfy_dir, comfy_dir / "models")
                is None)
        s.check("a folder that does not exist is not written out",
                bootstrap.model_paths_file(comfy_dir, root / "nope") is None)

        written = bootstrap.model_paths_file(comfy_dir, elsewhere)
        s.check("a folder elsewhere is written out for ComfyUI",
                written is not None and written.exists())
        body = written.read_text()
        s.check("it names the chosen folder as the base path",
                elsewhere.as_posix() in body, body[:120])
        s.check("it marks those folders as the default",
                "is_default: true" in body)
        for folder in ("checkpoints", "audio_encoders"):
            s.check(f"it maps {folder}", f"{folder}: {folder}/" in body)
        s.check("every folder a download can target is mapped",
                all(f"{f}: {f}/" in body for f in manager.MODEL_FOLDERS))
        s.check("rewriting it is stable",
                bootstrap.model_paths_file(comfy_dir, elsewhere)
                .read_text() == body)

    # -- two sources of truth that can disagree ----------------------------
    # The model row stats a folder; the song is built from what the engine
    # lists. When those disagree the page used to say "All files present" and
    # the song failed saying there was no model — which reads as a broken
    # download, and sends people to re-fetch four gigabytes they already have.
    with Workspace() as root:
        models = root / "models"
        for folder in ("checkpoints", "audio_encoders"):
            (models / folder).mkdir(parents=True)
        for rel, _u, size, _r in bootstrap.MODELS:
            with (models / rel).open("wb") as handle:
                handle.truncate(size)
        cfg = {**bootstrap.DEFAULT_CONFIG, "models_dir": str(models),
               "comfy_url": "http://127.0.0.1:1"}

        def models_row(listed):
            return {d["id"]: d for d in
                    manager.dependencies(cfg, listed)}["models"]

        s.check("files on disk and the engine lists them — all present",
                models_row(["yue2_3b_int8_convrot.safetensors"])["state"] == "ok")
        row = models_row([])
        s.check("files on disk but the engine lists none — says so",
                row["state"] == "warn" and "different folder" in row["detail"],
                f"{row['state']}: {row['detail'][:80]}")
        s.check("engine unreachable — does not accuse it of anything",
                models_row(None)["state"] == "ok")

        checkpoint = models / bootstrap.MODELS[0][0]
        checkpoint.write_bytes(b"partial download")
        s.check("a truncated checkpoint is missing, not generation-ready",
                Path(bootstrap.missing_models(models, cfg)[0][0]).name
                == checkpoint.name)

    # -- the port answered by a ComfyUI we are not managing ------------------
    s.check("the same folder is the same install",
            manager.same_install("/opt/ComfyUI", "/opt/ComfyUI"))
    s.check("a trailing separator is still the same install",
            manager.same_install("/opt/ComfyUI/", "/opt/ComfyUI"))
    s.check("a different folder is a different install",
            not manager.same_install("/opt/ComfyUI", "/opt/OtherComfy"))
    for a, b, why in [("", "/opt/ComfyUI", "nothing is being managed yet"),
                      ("/opt/ComfyUI", "", "the engine does not report argv")]:
        s.check(f"no complaint when {why}", manager.same_install(a, b))

    with Workspace() as root:
        comfy_dir = root / "ComfyUI"
        (comfy_dir / "models").mkdir(parents=True)
        cfg = {**bootstrap.DEFAULT_CONFIG, "comfy_dir": str(comfy_dir),
               "models_dir": str(comfy_dir / "models"),
               "comfy_url": "http://127.0.0.1:1"}
        s.check("an offline engine is reported missing, not foreign",
                {d["id"]: d for d in
                 manager.dependencies(cfg)}["engine"]["state"] == "missing")

    # A green engine row must say which install answered, or say that it could
    # not tell — "ok" alone reads as "verified", and an unverifiable engine
    # squatting the port looked exactly like a healthy one.
    from unittest.mock import patch
    cfg_live = {**bootstrap.DEFAULT_CONFIG, "comfy_dir": "/opt/ComfyUI",
                "models_dir": "/opt/ComfyUI/models"}
    with patch.object(manager.bootstrap, "comfy_online", lambda url: True):
        def engine_row(root):
            return {d["id"]: d for d in
                    manager.dependencies(cfg_live, ["x.safetensors"],
                                         root)}["engine"]
        row = engine_row("/opt/ComfyUI")
        s.check("a verified engine names its install",
                row["state"] == "ok" and "/opt/ComfyUI" in row["detail"])
        row = engine_row("")
        s.check("an engine with no argv admits it is unverified",
                row["state"] == "ok" and "does not say" in row["detail"],
                row["detail"][:80])
        row = engine_row("/opt/OtherComfy")
        s.check("a foreign engine is still called out",
                row["state"] == "warn" and "/opt/OtherComfy" in row["detail"])

    # -- the torch probe is paid once, not per page view --------------------
    # Importing torch in a subprocess costs whole seconds; the Engine page
    # visiting it every time read as the whole app lagging.
    from unittest.mock import patch as _patch
    calls = []
    real_run = manager.subprocess.run
    def counting_run(*a, **k):
        calls.append(a[0][0])
        class R: returncode = 1; stdout = ""
        return R()
    manager._TORCH_PROBE.clear()
    with _patch.object(manager.subprocess, "run", counting_run):
        first = manager._probe_torch(sys.executable)
        again = manager._probe_torch(sys.executable)
        s.check("the second look is answered from memory",
                len(calls) == 1 and first == again, f"{len(calls)} probe runs")
        manager._probe_torch(sys.executable, fresh=True)
        s.check("Recheck really re-probes", len(calls) == 2)
    manager._TORCH_PROBE.clear()

    # -- ffmpeg comes to the app, not the app to winget ---------------------
    # winget's portable install was refused outright on a real machine
    # ("copy_file: Access is denied" into AppData), and even working it lands
    # on the system drive. So on Windows the app downloads the zip itself into
    # data/tools and keeps only the two executables.
    from unittest.mock import patch
    import io, zipfile as _zip
    with Workspace() as root:
        with patch.object(bootstrap, "TOOLS_DIR", root / "tools"):
            bundled = root / "tools" / "ffmpeg" / "bin"

            def fake_zip(names):
                buf = io.BytesIO()
                with _zip.ZipFile(buf, "w") as zf:
                    for n in names:
                        zf.writestr(n, b"binary")
                return buf.getvalue()

            def served(payload):
                def dl(url, dest, task, headers):
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(payload)
                return dl

            class T:
                cancel = False
                def log(self, m): pass
                def set(self, **kw): self.detail = kw.get("detail", "")

            good = fake_zip(["ffmpeg-7/bin/ffmpeg.exe", "ffmpeg-7/bin/ffprobe.exe",
                             "ffmpeg-7/doc/manual.html"])
            with patch.object(manager, "_stream_download", served(good)):
                manager._install_ffmpeg_download(T())
            s.check("ffmpeg.exe lands in data/tools",
                    (bundled / "ffmpeg.exe").read_bytes() == b"binary")
            s.check("ffprobe.exe comes along",
                    (bundled / "ffprobe.exe").exists())
            s.check("the documentation folder does not",
                    not (root / "tools" / "ffmpeg" / "doc").exists()
                    and not list(bundled.glob("*.html")))
            s.check("the archive is cleaned up",
                    not (root / "tools" / "ffmpeg.zip").exists())

            s.check("find_tool prefers the copy the app fetched",
                    bootstrap.find_tool("ffmpeg") == str(bundled / "ffmpeg.exe"))
            ff = bootstrap.find_tool("ffmpeg")
            cfg_ff = {**bootstrap.DEFAULT_CONFIG, "comfy_url": "http://127.0.0.1:1"}
            row = {d["id"]: d for d in manager.dependencies(cfg_ff)}["ffmpeg"]
            s.check("the Engine page shows where that copy lives",
                    row["state"] == "ok" and ff in row["detail"], row["detail"][:80])

            bad = fake_zip(["ffmpeg-7/README.txt"])
            with patch.object(manager, "_stream_download", served(bad)):
                s.fails_with("an archive without ffmpeg.exe is refused, with "
                             "the folder to fill by hand",
                             lambda: manager._install_ffmpeg_download(T()),
                             RuntimeError, "by hand")

            def refuse(url, dest, task, headers):
                raise RuntimeError("connection reset")
            with patch.object(manager, "_stream_download", refuse):
                s.fails_with("every mirror failing says what to do instead",
                             lambda: manager._install_ffmpeg_download(T()),
                             RuntimeError, "by hand")

    # -- deleting a model file ---------------------------------------------
    with Workspace() as root:
        models = root / "models"
        for folder in ("checkpoints", "audio_encoders"):
            (models / folder).mkdir(parents=True)
        (models / "checkpoints" / "real.safetensors").write_bytes(b"0" * 10)
        outside = root / "outside"; outside.mkdir()
        secret = outside / "secret.safetensors"; secret.write_bytes(b"precious")
        cfg = {"models_dir": str(models)}

        for folder, name, why in [
                ("checkpoints", "../../outside/secret.safetensors", "climbing out"),
                ("checkpoints", "..", "naming the folder above"),
                ("checkpoints", ".", "naming the folder itself"),
                ("../outside", "secret.safetensors", "a folder off the list"),
                ("etc", "passwd", "a folder that is not a model folder"),
                ("checkpoints", "", "an empty name")]:
            s.fails_with(f"deleting refuses {why}",
                         lambda f=folder, n=name: manager.delete_model(cfg, f, n),
                         RuntimeError, "not allowed")
        s.check("the file outside the models folder is untouched", secret.exists())

        manager.delete_model(cfg, "checkpoints", "real.safetensors")
        s.check("a real model file does delete",
                not (models / "checkpoints" / "real.safetensors").exists())
        s.fails_with("deleting something already gone says so",
                     lambda: manager.delete_model(cfg, "checkpoints", "real.safetensors"),
                     RuntimeError, "already gone")

        # a model file that is a symlink loses the link, not the target
        shared = outside / "shared.safetensors"; shared.write_bytes(b"shared")
        link = models / "checkpoints" / "linked.safetensors"
        link.symlink_to(shared)
        manager.delete_model(cfg, "checkpoints", "linked.safetensors")
        s.check("deleting a symlinked model removes the link, keeps the target",
                not link.is_symlink() and shared.exists())

        # big model folders are often symlinked onto another drive
        elsewhere = root / "bigdisk" / "checkpoints"
        elsewhere.mkdir(parents=True)
        (elsewhere / "onbig.safetensors").write_bytes(b"0")
        shutil.rmtree(models / "audio_encoders")
        (models / "audio_encoders").symlink_to(elsewhere, target_is_directory=True)
        manager.delete_model(cfg, "audio_encoders", "onbig.safetensors")
        s.check("a models folder symlinked to another drive still works",
                not (elsewhere / "onbig.safetensors").exists())

    # -- a long pip install has to show that it is doing something ---------
    # pip draws no progress bar when its output is piped, so installing a
    # 2.7 GB PyTorch wheel said nothing at all for minutes and looked hung.
    state: dict = {}
    s.check("a Downloading line is remembered, not shown as progress",
            bootstrap.pip_progress(
                "Downloading torch-2.11.0%2Bcu128-cp312-win_amd64.whl (2753.2 MB)",
                state) is None and state.get("what") == "Downloading torch")
    state["since"] = time.time() - 60
    told = bootstrap.pip_progress("Progress 1288490188 of 2887193395", state)
    s.check("a Progress line becomes something worth reading",
            told is not None and "1.29 of 2.89 GB" in told and "44%" in told,
            told or "(nothing)")
    s.check("and carries a speed and a time left",
            "MB/s" in (told or "") and "left" in (told or ""))
    small = bootstrap.pip_progress("Progress 8000000 of 16000000",
                                   {"what": "numpy", "since": time.time() - 2})
    s.check("a small file is measured in MB, not 0.01 GB",
            "8 of 16 MB" in (small or ""), small or "(nothing)")
    rushed = bootstrap.pip_progress("Progress 1288490188 of 2887193395",
                                    {"what": "torch", "since": time.time()})
    s.check("a rate is not quoted before there is time to measure one",
            "MB/s" not in (rushed or "") and "44%" in (rushed or ""),
            rushed or "(nothing)")

    # Once pip stops downloading it says nothing for minutes while it unpacks.
    # The clock has to name that, or it reads as the last file being stuck.
    phases: dict = {}
    bootstrap.pip_progress("Downloading numpy-2.5.2-cp312-win_amd64.whl (12.5 MB)",
                           phases)
    s.check("while fetching, the clock names the file",
            phases["what"] == "Downloading numpy")
    bootstrap.pip_progress("Installing collected packages: torch, torchvision",
                           phases)
    s.check("once unpacking starts, the clock says so instead",
            "Unpacking" in phases["what"], phases["what"])
    for junk in ("Progress", "Progress x of y", "Collecting scipy", ""):
        s.check(f"pip output {junk!r} is not mistaken for progress",
                bootstrap.pip_progress(junk, {}) is None)

    s.check("a carriage return counts as a line break",
            list(bootstrap.stream_lines(io.StringIO(
                "Progress 1 of 9\rProgress 5 of 9\rdone\n")))
            == ["Progress 1 of 9", "Progress 5 of 9", "done"])

    # -- downloads: resumable, and honest about progress -------------------
    body = bytes(range(256)) * 8000
    for honours_range in (True, False):
        port = free_port()
        served = {"n": 0}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                rng = self.headers.get("Range") if honours_range else None
                start = int(rng.split("=")[1].split("-")[0]) if rng else 0
                chunk = body[start:]
                self.send_response(206 if rng else 200)
                if rng:
                    self.send_header("Content-Range",
                                     f"bytes {start}-{len(body)-1}/{len(body)}")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                sent = 0
                try:
                    while sent < len(chunk):
                        self.wfile.write(chunk[sent:sent + 200_000])
                        sent += 200_000
                        served["n"] += 200_000
                        time.sleep(0.05)
                except OSError:
                    pass

        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        with Workspace() as root:
            dest = root / "model.safetensors"
            part = dest.with_suffix(dest.suffix + ".part")
            part.write_bytes(body[:1_600_000])          # an interrupted attempt
            task = manager.Task("download", "model")
            seen: list[float] = []
            original = task.set
            task.set = lambda **kw: (seen.append(kw.get("pct")), original(**kw))[1]
            manager._stream_download(f"http://127.0.0.1:{port}/m", dest, task, {})
            label = "honouring Range" if honours_range else "ignoring Range"
            s.check(f"a resumed download ({label}) rebuilds the file exactly",
                    dest.read_bytes() == body)
            s.check(f"a resumed download ({label}) never leaves a .part behind",
                    not part.exists())
            percentages = [p for p in seen if p]
            s.check(f"a resumed download ({label}) reports progress that reaches 100",
                    not percentages or max(percentages) >= 99.9,
                    f"peaked at {max(percentages) if percentages else 0:.1f}%")
        srv.shutdown()

    # -- a .part that is already the whole file ----------------------------
    port = free_port()

    class Complete(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(416)
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", port), Complete)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with Workspace() as root:
        for who, call in (("setup", lambda d, p: bootstrap.download(
                               f"http://127.0.0.1:{port}/f", d,
                               bootstrap.Progress(), "models", "f")),
                          ("the models page", lambda d, p: manager._stream_download(
                               f"http://127.0.0.1:{port}/f", d,
                               manager.Task("download", "f"), {}))):
            dest = root / f"{who}.safetensors"
            dest.write_bytes(b"an older copy")
            part = dest.with_suffix(dest.suffix + ".part")
            part.write_bytes(b"the finished download")
            call(dest, part)
            s.check(f"{who}: a complete .part replaces the file it finishes",
                    dest.read_bytes() == b"the finished download"
                    and not part.exists())
    srv.shutdown()

    # -- huggingface answers of every kind ---------------------------------
    port = free_port()
    mode = {"v": "ok"}

    class HF(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            import json as _json
            kind = "models" if "/api/models/" in self.path else "datasets"
            codes = {"gated": 401, "noaccess": 403, "missing": 404,
                     "server-error": 500}
            code = codes.get(mode["v"])
            if mode["v"] == "dataset-only" and kind == "models":
                code = 404
            body = _json.dumps({"error": mode["v"]} if code else [
                {"type": "file", "path": "checkpoints/big.safetensors",
                 "lfs": {"size": 3_960_000_000}},
                {"type": "directory", "path": "checkpoints"},
                {"type": "file", "path": "README.md", "size": 1200}]).encode()
            self.send_response(code or 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", port), HF)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = {"hf_endpoint": f"http://127.0.0.1:{port}"}
    listing = manager.hf_list(cfg, "Comfy-Org/YuE2")
    s.check("a normal repo lists only its files",
            [f["path"] for f in listing["files"]]
            == ["checkpoints/big.safetensors", "README.md"])
    mode["v"] = "dataset-only"
    s.equal("a dataset repo is found after models comes back empty",
            manager.hf_list(cfg, "x/y")["kind"], "datasets")
    for state, says in [("gated", "needs a huggingface token"),
                        ("noaccess", "cannot read this repo"),
                        ("missing", "could not find"),
                        ("server-error", "try again in a moment")]:
        mode["v"] = state
        s.fails_with(f"a {state} repo is explained, not dumped",
                     lambda: manager.hf_list(cfg, "x/y"), RuntimeError, says)
    srv.shutdown()

    s.check("a folder is guessed from the path",
            [manager.guess_folder(p) for p in
             ("checkpoints/a.safetensors", "audio_encoders/e.safetensors",
              "some/vae/x.safetensors", "my_lora.safetensors", "sheetsage2.bin")]
            == ["checkpoints", "audio_encoders", "vae", "loras", "audio_encoders"])
    return s


if __name__ == "__main__":
    suite = run("--slow" in sys.argv)
    print(f"\n{suite.name}: {suite.passed} passed, {len(suite.failures)} failed")
    sys.exit(1 if suite.failures else 0)
