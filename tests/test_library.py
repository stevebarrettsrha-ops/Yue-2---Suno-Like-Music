"""server.py's own state: the song list, job reporting, and track lengths.

These run in-process against a throwaway data directory, so they can reach the
functions the HTTP routes are built from.
"""

from __future__ import annotations

import json
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server                                      # noqa: E402
from harness import Suite, Workspace               # noqa: E402


def _point_at(data: Path) -> None:
    server.DATA_DIR = data
    server.LIBRARY_PATH = data / "library.json"
    server.TRACKS_DIR = data / "tracks"
    server.TRACKS_DIR.mkdir(parents=True, exist_ok=True)


def run(slow: bool = False) -> Suite:
    s = Suite("library")
    with Workspace() as data:
        _point_at(data)

        # -- a finished song and a delete arriving together ----------------
        # Both read the list, edit it and write it back. This drives the real
        # DELETE route, not a copy of it, so it still guards the thing that
        # actually runs. Held apart, whichever wrote second used to win and the
        # other's work simply vanished.
        lost = undone = 0
        rounds = 30
        for r in range(rounds):
            server.write_library([{"id": "victim", "title": "V",
                                   "file": "v.flac", "created": 1}])
            (server.TRACKS_DIR / "v.flac").write_bytes(b"0" * 400_000)
            newcomer = {"id": f"new{r}", "title": "N", "file": "n.flac",
                        "created": 2}

            def delete_victim():
                with server.app.test_client() as client:
                    client.delete("/api/track/victim")

            a = threading.Thread(target=delete_victim)
            b = threading.Thread(target=server.add_track, args=(newcomer,))
            a.start(); b.start(); a.join(); b.join()
            ids = {i["id"] for i in server.read_library()}
            lost += f"new{r}" not in ids
            undone += "victim" in ids
        s.check("a song finishing during a delete is never lost",
                lost == 0, f"{lost} of {rounds} lost")
        s.check("the delete is never undone by that save", undone == 0,
                f"{undone} of {rounds} undone")

        # -- many songs landing at once ------------------------------------
        server.write_library([])
        start = threading.Barrier(24)

        def add(i):
            start.wait()
            server.add_track({"id": f"t{i:03d}", "title": f"song {i}",
                              "file": f"{i}.flac", "created": time.time()})
        threads = [threading.Thread(target=add, args=(i,)) for i in range(24)]
        for t in threads: t.start()
        for t in threads: t.join()
        s.equal("24 songs finishing at once all survive",
                len(server.read_library()), 24)

        # -- the write is all-or-nothing -----------------------------------
        server.write_library([{"id": "keep", "title": "Keep", "file": "k.flac"}])
        before = server.LIBRARY_PATH.read_text()
        try:
            # json.dumps refuses part way through, after the temp file is open
            server.write_library([{"id": "x", "bad": {1, 2}}])
        except TypeError:
            pass
        s.check("a write that fails leaves the old list untouched",
                server.LIBRARY_PATH.read_text() == before)

        # -- a library that is not what we expect --------------------------
        for label, content in [("an object rather than a list", '{"a": "b"}'),
                               ("truncated mid-write", '[{"id": "a", "ti'),
                               ("an empty file", ""),
                               ("entries that are not records", '[1, "two", null]')]:
            server.LIBRARY_PATH.write_text(content)
            s.check(f"{label}: reads as an empty list",
                    server.read_library() == [])
            server.LIBRARY_PATH.write_text(content)
            server.add_track({"id": "saved", "title": "Saved", "file": "s.flac"})
            s.check(f"{label}: a finished song is still saved",
                    [i["id"] for i in server.read_library()] == ["saved"])

        server.LIBRARY_PATH.write_text('[{"title": "no id"}, {"id": "ok"}]')
        s.check("an entry with no id is dropped rather than crashing lookups",
                [i["id"] for i in server.read_library()] == ["ok"])

        # -- a filename out of the library file can only name a track ------
        outside = data / "outside.txt"
        outside.write_text("private")
        for name in ("../outside.txt", "/etc/passwd", "sub/dir/x.flac", "", None):
            path = server.track_file({"file": name})
            ok = path is None or path.parent == server.TRACKS_DIR
            s.check(f"a track file named {name!r} stays inside data/tracks", ok,
                    str(path))

        # -- the HTTP surface over all of that ------------------------------
        server.LIBRARY_PATH.write_text(json.dumps(
            [{"id": "x", "title": "No file"},
             {"id": "evil", "title": "Evil", "file": "../outside.txt"}]))
        with server.app.test_client() as c:
            for method, path in (("GET", "/api/track/x"),
                                 ("PATCH", "/api/track/x"),
                                 ("DELETE", "/api/track/x"),
                                 ("GET", "/api/track/evil"),
                                 ("GET", "/api/track/missing")):
                r = c.open(path, method=method, json={"title": "t"})
                s.check(f"{method} {path} answers without a 500",
                        r.status_code < 500, f"HTTP {r.status_code}")
        s.check("the file outside data/tracks was never touched", outside.exists())

        # -- a song is kept as a wav to play and an mp3 to send ------------
        import shutil as _shutil
        import wave as _wave
        master = data / "master.flac"
        raw = data / "raw.wav"
        with _wave.open(str(raw), "wb") as handle:
            handle.setnchannels(1); handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8000 * 2)      # two seconds
        have_ffmpeg = bool(_shutil.which("ffmpeg"))
        if have_ffmpeg:
            server.convert_audio(raw, master)
            made = server.save_as_wav_and_mp3(master, "song1")
            s.check("a rendered song is kept as both a wav and an mp3",
                    made.get("wav") == "song1.wav" and made.get("mp3") == "song1.mp3",
                    str(made))
            s.check("the wav really is one",
                    (server.TRACKS_DIR / "song1.wav").read_bytes()[:4] == b"RIFF")
            s.check("the mp3 really is one",
                    (server.TRACKS_DIR / "song1.mp3").read_bytes()[:3]
                    in (b"ID3", b"\xff\xfb"))
            s.check("the lossless file it came from is not kept as well",
                    not master.exists())
            s.check("and the length is read back off the wav",
                    abs((server.audio_duration(server.TRACKS_DIR / "song1.wav")
                         or 0) - 2.0) < 0.05)
        else:
            s.check("ffmpeg is present to test conversion", False,
                    "skipped — no ffmpeg on this machine")

        # Without ffmpeg there is nothing to convert with, and the song must
        # still survive as whatever ComfyUI rendered.
        keep = server.shutil.which
        server.shutil.which = lambda name: None
        try:
            kept = data / "kept.flac"
            kept.write_bytes(b"not really a flac, but a file")
            s.equal("with no ffmpeg, nothing is written",
                    server.save_as_wav_and_mp3(kept, "song2"), {})
            s.check("and the rendered song is left alone", kept.exists())
        finally:
            server.shutil.which = keep

        s.check("a file is never converted onto itself",
                server.convert_audio(raw, raw) is False)

        both = {"id": "x", "file": "song1.wav", "mp3": "song1.mp3"}
        s.equal("a song knows about both of its files",
                sorted(p.name for p in server.track_files(both)),
                ["song1.mp3", "song1.wav"])
        s.equal("an older song with just the one still works",
                [p.name for p in server.track_files({"id": "y", "file": "old.flac"})],
                ["old.flac"])

        # -- how long a song is --------------------------------------------
        for rate, samples, want in [(44100, 44100 * 185, 185.0),
                                    (48000, 48000 * 212 + 24000, 212.5),
                                    (96000, 96000 * 30, 30.0)]:
            p = data / f"f{rate}{samples}.flac"
            _write_flac(p, rate, samples)
            s.check(f"a {want}s flac reads as its real length",
                    abs((server.audio_duration(p) or 0) - want) < 0.02,
                    str(server.audio_duration(p)))
        for secs in (180.0, 3.5, 243.75):
            p = data / f"o{secs}.opus"
            _write_opus(p, secs)
            s.check(f"a {secs}s opus reads as its real length",
                    abs((server.audio_duration(p) or 0) - secs) < 0.02,
                    str(server.audio_duration(p)))

        junk = data / "junk.flac"; junk.write_bytes(b"ID3 not really a flac")
        s.check("a file that is not a flac reports no length",
                server.audio_duration(junk) is None)
        vorbis = data / "vorbis.ogg"
        vorbis.write_bytes(_ogg_page(1, 0, 0, b"\x01vorbis" + b"\x00" * 20, 2)
                           + _ogg_page(1, 1, 441000, b"\x00" * 50, 4))
        s.check("an ogg holding vorbis is left for ffprobe, not under-reported",
                server.audio_duration(vorbis) is None)

        # -- a song that fails after minutes must still be readable --------
        now = time.time()
        server.jobs.clear()
        cases = {"failed after 20 seconds": (20, "error"),
                 "failed after five minutes": (300, "error"),
                 "finished after five minutes": (300, "done"),
                 "cancelled after five minutes": (300, "cancelled"),
                 "still running after five minutes": (300, "running")}
        for i, (label, (age, status)) in enumerate(cases.items()):
            row = {"id": f"j{i}", "status": status, "created": now - age,
                   "title": label, "error": "KSampler: out of memory"}
            if status != "running":
                row["ended"] = now - 2
            server.jobs[f"j{i}"] = row
        with server.app.test_client() as c:
            shown = {j["id"] for j in c.get("/api/jobs").get_json()}
        for i, label in enumerate(cases):
            s.check(f"{label} is still shown", f"j{i}" in shown)

        server.jobs.clear()
        server.jobs["old"] = {"id": "old", "status": "error", "created": now - 900,
                              "ended": now - 300, "title": "long finished"}
        with server.app.test_client() as c:
            s.check("a job that ended five minutes ago has gone",
                    not c.get("/api/jobs").get_json())

        # -- an instrumental is named after its style, not unsung lyrics ----
        s.equal("an instrumental takes its name from the style",
                server.track_title({"style": "lo-fi jazz, rainy",
                                    "lyrics": "unused words",
                                    "instrumental": True}), "Lo-Fi Jazz")
        s.equal("a song with words takes its name from the first line",
                server.track_title({"style": "reggae",
                                    "lyrics": "[verse]\nsunlight on the water"}),
                "Sunlight On The Water")
    return s


def _write_flac(path: Path, rate: int, total: int, channels: int = 2,
                bps: int = 16) -> None:
    packed = (rate << 44) | ((channels - 1) << 41) | ((bps - 1) << 36) | total
    body = (struct.pack(">H", 4096) * 2 + b"\x00\x00\x10" + b"\x00\x10\x00"
            + packed.to_bytes(8, "big") + b"\x00" * 16)
    path.write_bytes(b"fLaC" + b"\x80" + len(body).to_bytes(3, "big") + body)


def _ogg_page(serial: int, seq: int, granule: int, payload: bytes,
              header_type: int = 0) -> bytes:
    segments, left = [], len(payload)
    while left >= 255:
        segments.append(255); left -= 255
    segments.append(left)
    return (b"OggS" + bytes([0, header_type]) + struct.pack("<q", granule)
            + struct.pack("<I", serial) + struct.pack("<I", seq)
            + b"\x00\x00\x00\x00" + bytes([len(segments)]) + bytes(segments)
            + payload)


def _write_opus(path: Path, seconds: float, pre_skip: int = 312) -> None:
    granule = int(seconds * 48000) + pre_skip
    head = (b"OpusHead" + bytes([1, 2]) + struct.pack("<H", pre_skip)
            + struct.pack("<I", 48000) + struct.pack("<h", 0) + bytes([0]))
    tags = b"OpusTags" + struct.pack("<I", 4) + b"none" + struct.pack("<I", 0)
    path.write_bytes(_ogg_page(1, 0, 0, head, 2) + _ogg_page(1, 1, 0, tags)
                     + _ogg_page(1, 2, granule // 2, b"\xfc" * 200)
                     + _ogg_page(1, 3, granule, b"\xfc" * 200, 4))


if __name__ == "__main__":
    suite = run("--slow" in sys.argv)
    print(f"\n{suite.name}: {suite.passed} passed, {len(suite.failures)} failed")
    sys.exit(1 if suite.failures else 0)
