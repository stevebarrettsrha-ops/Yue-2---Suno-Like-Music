"""The HTTP surface, against a running server and a stand-in ComfyUI."""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Suite, Workspace, comfy, finish_jobs, studio, wait_for


def run(slow: bool = False) -> Suite:
    s = Suite("api")
    with comfy(delay=2) as engine, Workspace() as data, \
            studio(engine.url, data) as app:
        api = app.url

        st = requests.get(f"{api}/api/status", timeout=10).json()
        s.check("status reports a ready engine",
                st["ready"] and st["comfy_online"])
        s.check("status offers the checkpoints and formats the page needs",
                bool(st.get("checkpoints"))
                and {"flac", "mp3", "opus"} <= set(st.get("formats") or [])
                and st.get("has_cover_model"), str(st.get("formats")))

        # -- the setup sheet's steps arrive in the order they happen -------
        # They are shown as a checklist, so their order is the meaning. Flask
        # sorts the keys of any dict it sends, which once listed them
        # alphabetically: Check Python last, Start ComfyUI before the download.
        steps = requests.get(f"{api}/api/setup/state", timeout=10).json()["steps"]
        s.check("setup reports its steps as an ordered list",
                isinstance(steps, list), type(steps).__name__)
        s.equal("and in the order they really run",
                [step["label"] for step in steps] if isinstance(steps, list)
                else list(steps),
                ["Check Python", "Install ComfyUI", "Install dependencies",
                 "Download models", "Start ComfyUI"])

        # -- a song of every kind, end to end ------------------------------
        upload = requests.post(f"{api}/api/upload-reference",
                               files={"file": ("ref.wav", io.BytesIO(b"RIFFWAVE"),
                                               "audio/wav")}, timeout=20).json()
        s.check("a reference song uploads", upload.get("ok"))

        songs = {
            "a plain song": {"style": "roots reggae, 74 BPM",
                             "lyrics": "[verse]\nsunlight on the water"},
            "an instrumental": {"style": "lo-fi jazz", "lyrics": "unused",
                                "instrumental": True},
            "an mp3": {"style": "soul", "lyrics": "hey", "format": "mp3"},
            "no melody plan": {"style": "folk", "lyrics": "hey", "use_abc": False},
            "an edited score": {"style": "folk", "lyrics": "hey",
                                "abc": "X:1\nK:G\nGABc|"},
            "a cover": {"style": "gospel", "lyrics": "hey",
                        "reference_audio": upload.get("name")},
        }
        for label, params in songs.items():
            made = requests.post(f"{api}/api/generate", json=params,
                                 timeout=20).json()
            jobs = finish_jobs(api, 60)
            job = [j for j in jobs if j["id"] == made["jobs"][0]]
            s.check(f"{label} finishes",
                    bool(job) and job[0]["status"] == "done",
                    job[0].get("error", "") if job else "job vanished")

        library = requests.get(f"{api}/api/library", timeout=10).json()
        s.equal("every song reached the library", len(library), len(songs))
        s.check("the cover is marked as one",
                any(t["cover"] for t in library))
        s.check("the instrumental is named from its style",
                any(t["title"] == "Lo-Fi Jazz" for t in library))
        s.check("songs made with a plan keep their score",
                sum(1 for t in library if t["abc"]) >= len(songs) - 1)
        s.check("the song made without a plan has no score",
                any(not t["abc"] for t in library))

        # -- the format asked for is the format kept ------------------------
        import shutil as _shutil
        offered = requests.get(f"{api}/api/status", timeout=10).json()["formats"]
        s.check("the engine's own formats are offered",
                {"flac", "mp3"} <= set(offered), str(offered))
        s.equal("wav is offered exactly when ffmpeg can write one",
                "wav" in offered, bool(_shutil.which("ffmpeg")))

        magic = {"flac": b"fLaC", "wav": b"RIFF", "opus": b"OggS"}
        for fmt in offered:
            requests.post(f"{api}/api/generate",
                          json={"style": f"a {fmt} song", "lyrics": "x",
                                "format": fmt}, timeout=20)
            finish_jobs(api, 60)
            song = requests.get(f"{api}/api/library", timeout=10).json()[0]
            if not s.check(f"a song asked for as {fmt} is saved as one",
                           song.get("file", "").endswith(f".{fmt}")
                           and song.get("format") == fmt,
                           song.get("file", "")):
                continue
            got = requests.get(f"{api}/api/track/{song['id']}", timeout=20)
            head = magic.get(fmt)
            s.check(f"and the {fmt} it serves really is one",
                    got.status_code == 200
                    and (head is None or got.content[:4] == head)
                    and (fmt != "mp3" or got.content[:3] in (b"ID3", b"\xff\xfb")),
                    str(got.content[:4]))
            s.check(f"a {fmt} song is one file, not several",
                    len(list((data / "tracks").glob(f"{song['file'].split('.')[0]}.*")))
                    == 1)
        requests.get(f"{api}/api/library", timeout=10)

        # -- the audio itself ----------------------------------------------
        track = library[0]
        whole = requests.get(f"{api}/api/track/{track['id']}", timeout=20)
        s.check("a track downloads", whole.status_code == 200 and whole.content)
        s.equal("the player can seek into it",
                requests.get(f"{api}/api/track/{track['id']}",
                             headers={"Range": "bytes=1000-1999"},
                             timeout=20).status_code, 206)
        part = requests.get(f"{api}/api/track/{track['id']}",
                            headers={"Range": "bytes=1000-1999"}, timeout=20)
        s.check("the seeked bytes are the right ones",
                part.content == whole.content[1000:2000])

        # -- renaming, backfilled length, deleting --------------------------
        requests.patch(f"{api}/api/track/{track['id']}",
                       json={"title": "Renamed", "seconds": 42.5}, timeout=10)
        after = [t for t in requests.get(f"{api}/api/library", timeout=10).json()
                 if t["id"] == track["id"]][0]
        s.check("a rename and the browser's measured length both stick",
                after["title"] == "Renamed" and after["seconds"] == 42.5)
        requests.delete(f"{api}/api/track/{track['id']}", timeout=10)
        s.check("deleting removes the entry and every file it had",
                not [t for t in requests.get(f"{api}/api/library", timeout=10).json()
                     if t["id"] == track["id"]]
                and not (data / "tracks" / track["file"]).exists()
                and not (track.get("mp3")
                         and (data / "tracks" / track["mp3"]).exists()))

        # -- refusals people should understand ------------------------------
        s.check("a song with no style is refused",
                requests.post(f"{api}/api/generate", json={"style": "  "},
                              timeout=10).json()["error"].startswith("Add a style"))
        s.equal("asking for more than four at once still makes four",
                len(requests.post(f"{api}/api/generate",
                                  json={"style": "many", "count": 99},
                                  timeout=20).json()["jobs"]), 4)
        finish_jobs(api, 90)

    # -- stopping one song must not stop another --------------------------
    with comfy(delay=25) as engine, Workspace() as data, \
            studio(engine.url, data) as app:
        api = app.url
        made = requests.post(f"{api}/api/generate",
                             json={"style": "stop test", "lyrics": "x", "count": 2},
                             timeout=20).json()["jobs"]
        wait_for(lambda: all(j.get("prompt_id") for j in
                             requests.get(f"{api}/api/jobs", timeout=10).json()
                             if j["id"] in made), 20)
        queue = requests.get(f"{engine.url}/queue", timeout=10).json()
        running_ids = [e[1] for e in queue["queue_running"]]
        jobs = {j["id"]: j for j in requests.get(f"{api}/api/jobs", timeout=10).json()}
        running = [i for i in made if jobs[i].get("prompt_id") in running_ids]
        waiting = [i for i in made if i not in running]

        if s.check("one song is rendering and one is queued behind it",
                   len(running) == 1 and len(waiting) == 1):
            requests.post(f"{api}/api/jobs/{waiting[0]}/cancel", timeout=10)
            wait_for(lambda: requests.get(f"{api}/api/jobs", timeout=10).json()
                     and [j for j in requests.get(f"{api}/api/jobs", timeout=10).json()
                          if j["id"] == waiting[0]][0]["status"] != "running", 20)
            jobs = {j["id"]: j for j in
                    requests.get(f"{api}/api/jobs", timeout=10).json()}
            s.equal("stopping the queued song cancels that one",
                    jobs[waiting[0]]["status"], "cancelled")
            s.equal("and leaves the one that was rendering alone",
                    jobs[running[0]]["status"], "running")

            requests.post(f"{api}/api/jobs/{running[0]}/cancel", timeout=10)
            wait_for(lambda: [j for j in
                              requests.get(f"{api}/api/jobs", timeout=10).json()
                              if j["id"] == running[0]][0]["status"] != "running", 30)
            jobs = {j["id"]: j for j in
                    requests.get(f"{api}/api/jobs", timeout=10).json()}
            s.equal("stopping the rendering song stops it too",
                    jobs[running[0]]["status"], "cancelled")

        s.equal("a cancelled song leaves no track behind",
                len(requests.get(f"{api}/api/library", timeout=10).json()), 0)
        t0 = time.time()
        requests.get(f"{api}/api/jobs", timeout=10)
        requests.post(f"{api}/api/generate", json={"style": "still alive"}, timeout=10)
        s.check("the server is still answering after all that",
                time.time() - t0 < 5, f"{time.time()-t0:.2f}s")

    # -- ComfyUI going away mid-song ---------------------------------------
    with Workspace() as data:
        engine = comfy(delay=90).__enter__()
        with studio(engine.url, data) as app:
            api = app.url
            job = requests.post(f"{api}/api/generate",
                                json={"style": "outage", "lyrics": "x"},
                                timeout=20).json()["jobs"][0]
            wait_for(lambda: [j for j in
                              requests.get(f"{api}/api/jobs", timeout=10).json()
                              if j["id"] == job][0].get("prompt_id"), 20)
            engine.stop()
            held = wait_for(lambda: "Waiting for ComfyUI" in
                            ([j for j in requests.get(f"{api}/api/jobs",
                                                      timeout=10).json()
                              if j["id"] == job] or [{}])[0].get("stage", ""), 20)
            s.check("a song holds on while ComfyUI is away, without an error",
                    held)

            # the engine comes back, but its queue did not survive
            again = comfy(delay=2)
            again.port = engine.port
            again.url = engine.url
            again.argv[-1] = str(engine.port)
            try:
                again.__enter__()
                ended = wait_for(lambda: [j for j in
                                          requests.get(f"{api}/api/jobs",
                                                       timeout=10).json()
                                          if j["id"] == job][0]["status"] != "running",
                                 40)
                final = [j for j in requests.get(f"{api}/api/jobs", timeout=10).json()
                         if j["id"] == job][0]
                s.check("when it returns without the song, the reason is plain",
                        ended and "restarted" in (final.get("error") or "").lower(),
                        (final.get("error") or "")[:80])
            finally:
                again.stop()
    return s


if __name__ == "__main__":
    suite = run("--slow" in sys.argv)
    print(f"\n{suite.name}: {suite.passed} passed, {len(suite.failures)} failed")
    sys.exit(1 if suite.failures else 0)
