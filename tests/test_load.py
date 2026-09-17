"""Many things at once, and what the process looks like afterwards."""

from __future__ import annotations

import collections
import os
import sys
import threading
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Suite, Workspace, comfy, finish_jobs, studio


def _usage(pid: int) -> tuple[int, int, int]:
    status = Path(f"/proc/{pid}/status").read_text()
    threads = int(status.split("Threads:")[1].split()[0])
    rss = int(status.split("VmRSS:")[1].split()[0])
    return len(os.listdir(f"/proc/{pid}/fd")), threads, rss


def run(slow: bool = False) -> Suite:
    s = Suite("load")
    measurable = Path("/proc/self/status").exists()
    with comfy(delay=1) as engine, Workspace() as data, \
            studio(engine.url, data) as app:
        api, pid = app.url, app.proc.pid
        base = _usage(pid) if measurable else None

        # -- the page polls several endpoints at once, from every tab ------
        codes: collections.Counter = collections.Counter()
        lock = threading.Lock()
        paths = ["/api/status", "/api/library", "/api/jobs", "/api/tasks",
                 "/api/deps", "/api/hf/local"]

        def read(i: int) -> None:
            try:
                r = requests.get(api + paths[i % len(paths)], timeout=30)
                with lock:
                    codes[r.status_code] += 1
            except Exception as exc:  # noqa: BLE001
                with lock:
                    codes[type(exc).__name__] += 1

        threads = [threading.Thread(target=read, args=(i,)) for i in range(300)]
        started = time.time()
        for t in threads: t.start()
        for t in threads: t.join()
        s.check("300 requests at once are all answered",
                codes == collections.Counter({200: 300}), str(dict(codes)))
        s.check("and answered promptly", time.time() - started < 20,
                f"{time.time()-started:.1f}s")

        # -- every song someone could queue, with deletes landing on top ---
        made: list[str] = []
        errors: list[str] = []

        def queue(i: int) -> None:
            try:
                r = requests.post(api + "/api/generate",
                                  json={"style": f"load {i}", "lyrics": "x",
                                        "count": 4}, timeout=30)
                made.extend(r.json()["jobs"]) if r.status_code == 200 \
                    else errors.append(r.text[:60])
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc).__name__)

        threads = [threading.Thread(target=queue, args=(i,)) for i in range(12)]
        for t in threads: t.start()
        for t in threads: t.join()
        s.check("48 songs queued at once are all accepted",
                len(made) == 48 and not errors, f"{len(made)} made, {errors[:2]}")

        stop = threading.Event()

        def delete_as_they_land() -> None:
            while not stop.is_set():
                library = requests.get(api + "/api/library", timeout=20).json()
                if library:
                    requests.delete(api + f"/api/track/{library[-1]['id']}",
                                    timeout=20)
                time.sleep(0.1)

        deleter = threading.Thread(target=delete_as_they_land)
        deleter.start()
        jobs = finish_jobs(api, 240)
        stop.set(); deleter.join()

        states = collections.Counter(j["status"] for j in jobs)
        s.check("every song finishes", states.get("done", 0) == 48,
                str(dict(states)))

        library = requests.get(api + "/api/library", timeout=20).json()
        ids = [t["id"] for t in library]
        listed = {t["file"] for t in library}
        on_disk = {p.name for p in (data / "tracks").glob("*")}
        s.check("no song is listed twice", len(ids) == len(set(ids)),
                f"{len(ids) - len(set(ids))} duplicates")
        s.check("every listed song still has its audio", not listed - on_disk,
                f"{len(listed - on_disk)} missing")
        s.check("no audio is left behind with nothing pointing at it",
                not on_disk - listed, f"{len(on_disk - listed)} orphans")
        try:
            import json
            json.loads((data / "library.json").read_text())
            s.check("the library file is still valid after all of that", True)
        except Exception as exc:  # noqa: BLE001
            s.check("the library file is still valid after all of that", False,
                    str(exc)[:60])

        if measurable:
            time.sleep(5)
            after = _usage(pid)
            s.check("no file descriptors leaked", after[0] <= base[0] + 5,
                    f"{base[0]} -> {after[0]}")
            s.check("worker threads all finished",
                    after[1] <= base[1] + 2, f"{base[1]} -> {after[1]}")
            s.check("memory did not run away",
                    after[2] - base[2] < 250_000,
                    f"{(after[2]-base[2])/1000:.0f} MB more")
    return s


if __name__ == "__main__":
    suite = run("--slow" in sys.argv)
    print(f"\n{suite.name}: {suite.passed} passed, {len(suite.failures)} failed")
    sys.exit(1 if suite.failures else 0)
