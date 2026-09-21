"""Input the front end would never send, and state damaged behind the app.

Nothing here should answer 5xx, leak a stack trace, escape a directory, or
leave the running app worse than it found it.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Suite, Workspace, comfy, studio


def _sane(s: Suite, what: str, r, allow=(200, 400, 403, 404, 409, 503)) -> bool:
    leaked = "Traceback" in r.text or 'File "/' in r.text
    return s.check(what, r.status_code in allow and not leaked,
                   f"HTTP {r.status_code}" + (" and leaked a trace" if leaked else ""))


def run(slow: bool = False) -> Suite:
    s = Suite("hardening")
    with comfy(delay=1) as engine, Workspace() as data, \
            studio(engine.url, data) as app:
        api = app.url

        # -- fields of the wrong type --------------------------------------
        for label, body in [
                ("style as a number", {"style": 12345}),
                ("style as a list", {"style": ["a", "b"]}),
                ("style as null", {"style": None}),
                ("count as a word", {"style": "x", "count": "many"}),
                ("count enormous", {"style": "x", "count": 10000}),
                ("count negative", {"style": "x", "count": -5}),
                ("duration absurd", {"style": "x", "duration": 10 ** 9}),
                ("steps absurd", {"style": "x", "steps": 10 ** 7}),
                ("seed enormous", {"style": "x", "seed": 2 ** 80}),
                ("top_p as a word", {"style": "x", "top_p": "hi"}),
                ("mode invented", {"style": "x", "mode": "banana"}),
                ("two megabytes of lyrics", {"style": "x", "lyrics": "la " * 700_000}),
                ("null bytes", {"style": "a\x00b"}),
                ("emoji and right-to-left text",
                 {"style": "🎵 مرحبا ‮", "lyrics": "𝄞 ñ 漢字"}),
                ("html", {"style": "<img src=x onerror=alert(1)>"})]:
            _sane(s, f"generate survives {label}",
                  requests.post(f"{api}/api/generate", json=body, timeout=30))

        made = requests.post(f"{api}/api/generate",
                             json={"style": "clamp", "duration": 10 ** 9,
                                   "steps": 10 ** 7, "count": 99},
                             timeout=20).json()
        s.check("an absurd count is clamped to four",
                len(made.get("jobs", [])) == 4,
                made.get("error", str(made))[:70])

        # -- settings that cannot be used must not replace ones that work ---
        good = requests.get(f"{api}/api/status", timeout=10).json()["config"]["comfy_url"]
        for junk in (8188, None, [], {"a": 1}, True):
            r = requests.post(f"{api}/api/config", json={"comfy_url": junk},
                              timeout=10)
            now = requests.get(f"{api}/api/status",
                               timeout=10).json()["config"]["comfy_url"]
            s.check(f"a comfy_url of {junk!r} leaves the working address alone",
                    now == good, f"became {now!r}")
        s.check("and the engine is still reachable afterwards",
                requests.get(f"{api}/api/status", timeout=10).json()["comfy_online"])
        requests.post(f"{api}/api/config", json={"torch_index": {"a": 1}}, timeout=10)
        s.check("a torch_index of the wrong type is never written to the config",
                isinstance(json.loads((data / "config.json").read_text())
                           .get("torch_index"), str))

        # -- routes that used to take anything ------------------------------
        r = requests.post(f"{api}/api/setup/start", json={"mode": "banana"},
                          timeout=10)
        s.check("an invented setup route is refused by name",
                r.status_code == 400 and "banana" in r.text)
        s.check("no ComfyUI was cloned by asking",
                not (Path(__file__).resolve().parent.parent / "ComfyUI").exists())
        _sane(s, "installing something that does not exist is refused",
              requests.post(f"{api}/api/deps/nonexistent/install", json={},
                            timeout=20), allow=(400,))
        for path in ("/api/setup/state?since=abc", "/api/tasks?since=abc",
                     "/api/setup/state?since=-9"):
            _sane(s, f"a mangled cursor on {path.split('?')[0]} is tolerated",
                  requests.get(f"{api}{path}", timeout=10))

        # -- paths, in every route that touches one -------------------------
        for path in ("/api/track/../../../etc/passwd",
                     "/api/track/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                     "/api/track/" + "A" * 5000):
            _sane(s, f"a track id of {path[11:30]}… cannot reach a file",
                  requests.get(f"{api}{path}", timeout=10))
        for body in ({"folder": "../../etc", "name": "passwd"},
                     {"folder": "checkpoints", "name": "/etc/passwd"},
                     {"folder": "checkpoints", "name": None},
                     {"folder": "checkpoints", "name": {"a": 1}}):
            _sane(s, f"deleting a model named {str(body)[:40]}… is refused",
                  requests.delete(f"{api}/api/hf/local", json=body, timeout=10))
        _sane(s, "a download path that climbs out is refused",
              requests.post(f"{api}/api/hf/download",
                            json={"repo": "a/b", "path": "../../../tmp/evil"},
                            timeout=10))
        for body in ({"token": 42}, {"endpoint": []}, {"repo": 7}):
            _sane(s, f"huggingface settings survive {body}",
                  requests.post(f"{api}/api/hf/settings", json=body, timeout=10))
        s.check("the token is never handed back in full",
                "token" not in requests.get(f"{api}/api/hf/settings",
                                            timeout=10).json()
                or "hf_" not in requests.get(f"{api}/api/hf/settings",
                                             timeout=10).text)

        # -- bodies that are not JSON at all --------------------------------
        _sane(s, "a body that is not JSON is refused",
              requests.post(f"{api}/api/generate", data=b"\xff\xfe not json",
                            headers={"Content-Type": "application/json"},
                            timeout=10))
        _sane(s, "a form post cannot drive the API",
              requests.post(f"{api}/api/generate", data={"style": "attack"},
                            timeout=10))

        # JSON itself can be valid while still having the wrong top-level
        # shape.  Every write route expects an object; arrays and scalars must
        # be harmless rather than raising on their first `.get()` call.
        for label, method, path in (
                ("generate", requests.post, "/api/generate"),
                ("settings", requests.post, "/api/config"),
                ("rename", requests.patch, "/api/track/missing"),
                ("dependency install", requests.post,
                 "/api/deps/nonexistent/install"),
                ("HF settings", requests.post, "/api/hf/settings"),
                ("HF download", requests.post, "/api/hf/download"),
                ("HF delete", requests.delete, "/api/hf/local")):
            for value in ([], "text", 7, True):
                _sane(s, f"{label} survives JSON {type(value).__name__}",
                      method(f"{api}{path}", json=value, timeout=20))

        # Strings that look boolean are a common hand-written API mistake.
        # They must not silently invert generation or persistent settings.
        requests.post(f"{api}/api/config", json={"auto_start_comfy": False},
                      timeout=10)
        requests.post(f"{api}/api/config", json={"auto_start_comfy": "false"},
                      timeout=10)
        s.check("a string cannot masquerade as a settings boolean",
                requests.get(f"{api}/api/config", timeout=10).json()
                ["config"]["auto_start_comfy"] is False)

        # -- only this machine ----------------------------------------------
        s.equal("a request addressed to somewhere else is refused",
                requests.get(f"{api}/api/library",
                             headers={"Host": "evil.example.com"},
                             timeout=10).status_code, 403)
        for host in ("127.0.0.1", "localhost", None):
            headers = {"Host": f"{host}:{app.port}"} if host else {}
            s.check(f"a request addressed to {host or 'the default'} is served",
                    requests.get(f"{api}/api/library", headers=headers,
                                 timeout=10).status_code == 200)
        s.check("a page cannot be read from another origin",
                "access-control-allow-origin" not in
                {k.lower() for k in requests.get(
                    f"{api}/api/library",
                    headers={"Origin": "https://evil.example.com"},
                    timeout=10).headers})

    # -- state damaged behind the app's back --------------------------------
    for label, config, library in [
            ("a truncated library", None, '[{"id": "a", "title": "Half'),
            ("an empty library", None, ""),
            ("a library that is an object", None, '{"not": "a list"}'),
            ("entries missing their keys", None, '[{"id": "x"}, {"title": "no id"}]'),
            ("ten thousand songs", None,
             json.dumps([{"id": f"i{n}", "title": f"t{n}", "file": f"{n}.flac",
                          "created": n} for n in range(10000)])),
            ("a truncated config", '{"comfy_url": "http', "[]"),
            ("a config that is a list", "[1,2,3]", "[]"),
            ("an empty config", "", "[]"),
            ("a config of wrong types",
             json.dumps({"comfy_url": 8188, "auto_start_comfy": "yes",
                         "models_dir": [], "setup_complete": "maybe"}), "[]")]:
        with Workspace() as data:
            data.mkdir(parents=True, exist_ok=True)
            if config is None:
                (data / "config.json").write_text(json.dumps(
                    {"comfy_url": "http://127.0.0.1:1", "setup_complete": True,
                     "auto_start_comfy": False}))
            else:
                (data / "config.json").write_text(config)
            (data / "library.json").write_text(library)
            try:
                with studio("http://127.0.0.1:1", data) as app:
                    # studio() rewrites config.json, so put the damage back
                    if config is not None:
                        (data / "config.json").write_text(config)
                    ok = all(requests.get(f"{app.url}{p}", timeout=10).status_code
                             == 200 for p in ("/", "/api/status", "/api/library"))
                    s.check(f"{label}: the app still starts and serves", ok)
            except RuntimeError as exc:
                s.check(f"{label}: the app still starts and serves", False,
                        str(exc)[:80])

    with Workspace() as data:
        shutil.rmtree(data, ignore_errors=True)
        try:
            with studio("http://127.0.0.1:1", data) as app:
                s.check("with no data folder at all, the app still starts",
                        requests.get(f"{app.url}/api/status",
                                     timeout=10).status_code == 200)
        except RuntimeError as exc:
            s.check("with no data folder at all, the app still starts", False,
                    str(exc)[:80])
    return s


if __name__ == "__main__":
    suite = run("--slow" in sys.argv)
    print(f"\n{suite.name}: {suite.passed} passed, {len(suite.failures)} failed")
    sys.exit(1 if suite.failures else 0)
