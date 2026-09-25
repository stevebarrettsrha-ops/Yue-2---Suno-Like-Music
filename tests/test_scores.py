"""Scores: YuE2's native ABC read and edited, plan-first rendering, covers
transcribed before they are rendered, and edits that must keep the melody."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (Suite, Workspace, comfy, finish_jobs, llm,  # noqa: E402
                     studio, wait_for)

import scores  # noqa: E402

NATIVE = "\n".join([
    "X:1", "T:", "M:4/4", "L:1/32", "Q:1/4=88",
    'V: Vocal clef=treble name="Vocal Melody" snm="Vocal"',
    'V: Ins clef=treble name="Ins Melody" snm="Inst."',
    "K:G",
    "% verse", "V: Vocal", '"G"B8d8"Em"c8A8|"C"G16"D"A16|', "V: Ins", "Z2|",
    "% chorus", "V: Vocal", '"G"d8d8"C"e8e8|"D"d32|', "V: Ins", "z16G16|B32|",
]) + "\n"


def unit(s: Suite) -> None:
    f = scores.inspect(NATIVE)
    s.check("a native score is read", f["ok"], f.get("error", ""))
    s.check("with its tempo, key, meter, bars and length",
            (f["bpm"], f["key"], f["meter"], f["measures"]) == (88, "G", "4/4", 4)
            and 10 < f["seconds"] < 12, str(f)[:120])
    s.equal("its sections and the notes sung in each",
            [(x["name"], x["bars"], x["vocal_notes"]) for x in f["sections"]],
            [("verse", 2, 6), ("chorus", 2, 5)])
    s.equal("and its chords, in order of first use", f["chord_names"],
            ["G", "Em", "C", "D"])
    bad = scores.inspect("X:1\nK:G\nGABc|")
    s.check("a score outside the native form says why instead of guessing",
            not bad["ok"] and "native" in bad["error"], bad.get("error", ""))
    s.check("an empty one too", not scores.inspect("  ")["ok"])

    plain = scores.strip_chords(NATIVE)
    s.check("stripping chords leaves none", scores.inspect(plain)["chords"] == 0)
    s.check("and every sounding note where it was",
            scores.compare(NATIVE, plain, "both")["match"])
    vocal = scores.strip_chords(NATIVE, "Vocal")
    s.check("keeping the vocal line silences the instrument line",
            scores.inspect(vocal)["ins_notes"] == 0
            and scores.inspect(vocal)["vocal_notes"] == 6 + 5)
    s.fails_with("an unknown voice is refused",
                 lambda: scores.strip_chords(NATIVE, "Piano"),
                 scores.ScoreError, "voices")

    reharm = NATIVE.replace('"Em"', '"Am7"')
    check = scores.compare(NATIVE, reharm)
    s.check("new chords over the same notes keep the melody",
            check["match"] and check["chords_changed"] == 1, str(check))
    moved = NATIVE.replace("B8d8", "B8e8")
    check = scores.compare(NATIVE, moved)
    s.check("a moved note does not, and says where",
            not check["match"] and "note 2" in " ".join(check["differences"]),
            str(check["differences"]))
    faster = NATIVE.replace("Q:1/4=88", "Q:1/4=96")
    s.check("a new tempo fails the exact check",
            not scores.compare(NATIVE, faster)["match"])
    s.check("unless a tempo change is allowed",
            scores.compare(NATIVE, faster, allow_tempo_change=True)["match"])
    ins = NATIVE.replace("z16G16|", "z16A16|")
    s.check("the instrument line is only held to when asked",
            scores.compare(NATIVE, ins, "Vocal")["match"]
            and not scores.compare(NATIVE, ins, "both")["match"])

    fit = scores.lyric_fit("[Verse]\nThe platform hums a tired tune tonight\n"
                           "[Chorus]\nhome", NATIVE)
    s.equal("lyrics are weighed against the notes of each sung section",
            [(x["section"], x["fit"]) for x in fit],
            [("verse", "tight"), ("chorus", "sparse")])
    s.equal("syllables are counted roughly, silent e and all",
            scores.syllables("the time of rain"), 4)


def run(slow: bool = False) -> Suite:
    s = Suite("scores")
    unit(s)
    with comfy(delay=1) as engine, llm() as model, Workspace() as data, \
            studio(engine.url, data) as app:
        api = app.url
        post = lambda path, body: requests.post(f"{api}{path}", json=body,  # noqa: E731
                                                timeout=20)

        def task(tid: str, timeout: float = 60) -> dict:
            wait_for(lambda: requests.get(f"{api}/api/tasks?id={tid}",
                                          timeout=10).json()["state"] != "running",
                     timeout, 0.3)
            return requests.get(f"{api}/api/tasks?id={tid}", timeout=10).json()

        st = requests.get(f"{api}/api/status", timeout=10).json()
        s.equal("the engine says it can plan and transcribe on their own",
                st.get("can_plan"), {"plan": True, "transcribe": True})

        # -- the score routes ---------------------------------------------
        r = post("/api/abc/inspect", {"abc": NATIVE,
                                      "lyrics": "[Verse]\nla la la\n[Chorus]\nhey"})
        s.check("inspect answers with the facts and a lyric fit",
                r.json().get("ok") and r.json().get("lyric_fit"), r.text[:100])
        r = post("/api/abc/strip-chords", {"abc": NATIVE, "keep": "both"})
        s.check("strip-chords answers with a chord-free score",
                r.status_code == 200 and '"' not in r.json()["abc"].split("K:G", 1)[1])
        r = post("/api/abc/strip-chords", {"abc": "X:1\nK:C\nC|"})
        s.equal("and refuses a score it cannot read", r.status_code, 400)
        r = post("/api/abc/compare", {"before": NATIVE,
                                      "after": NATIVE.replace("B8d8", "B8e8")})
        s.check("compare reports a moved melody note",
                r.status_code == 200 and r.json()["match"] is False)

        # -- plan first, render later ---------------------------------------
        before = requests.get(f"{api}/api/library", timeout=10).json()
        r = post("/api/plan", {"style": "folk, 90 BPM", "lyrics": "[Verse]\nhi",
                               "count": 3, "seed": 40, "mode": "full"})
        s.check("a plan request starts a task", r.status_code == 200, r.text[:100])
        t = task(r.json()["task"]["id"])
        plans = t["meta"]["plans"]
        s.equal("three plans come back, on consecutive seeds",
                [p["seed"] for p in plans], [40, 41, 42])
        s.check("each one a readable native score with its facts",
                all(p["facts"]["ok"] and p["facts"]["chords"] for p in plans))
        s.check("and they differ, so there is something to choose",
                len({p["abc"] for p in plans}) == 3)
        s.equal("planning makes no song", requests.get(
            f"{api}/api/library", timeout=10).json(), before)
        r = post("/api/plan", {"style": "folk", "count": 3, "seed": 40})
        again = task(r.json()["task"]["id"])["meta"]["plans"]
        s.check("the same seeds write the same plans again",
                [p["abc"] for p in again] == [p["abc"] for p in plans])
        s.equal("a plan with no style is refused",
                post("/api/plan", {"style": " "}).status_code, 400)

        chosen = plans[1]
        made = post("/api/generate", {"style": "folk, 90 BPM", "lyrics": "hi",
                                      "abc": chosen["abc"], "mode": "full"}).json()
        finish_jobs(api, 60)
        track = requests.get(f"{api}/api/library", timeout=10).json()[0]
        s.check("the chosen plan renders as the song's score",
                made.get("jobs") and track["abc"] == chosen["abc"].strip())
        s.equal("and the song says its score came from the box",
                track.get("abc_source"), "score")
        s.check("with its sampling recorded for a faithful re-render",
                track.get("top_p") == 0.95 and track.get("repetition_penalty") == 1.2
                and track.get("seed") is not None, str(track)[:160])

        # -- a cover, transcribed first ------------------------------------
        up = requests.post(f"{api}/api/upload-reference",
                           files={"file": ("ref.wav", io.BytesIO(b"RIFFWAVE"),
                                           "audio/wav")}, timeout=20).json()
        s.equal("transcribing needs a recording",
                post("/api/plan", {"kind": "transcribe"}).status_code, 400)
        r = post("/api/plan", {"kind": "transcribe", "count": 5,
                               "reference_audio": up["name"], "cover_mode": "full"})
        t = task(r.json()["task"]["id"])
        got = t["meta"]["plans"]
        s.check("a recording is transcribed once, whatever count is asked",
                len(got) == 1 and got[0]["kind"] == "transcribe", str(len(got)))
        s.check("in the mode asked for — full keeps its chords",
                got[0]["mode"] == "full" and got[0]["facts"]["chords"] > 0)
        melody = post("/api/abc/strip-chords", {"abc": got[0]["abc"]}).json()["abc"]
        post("/api/generate", {"style": "gospel", "lyrics": "hey", "abc": melody,
                               "mode": "melody", "reference_audio": up["name"]})
        finish_jobs(api, 60)
        track = requests.get(f"{api}/api/library", timeout=10).json()[0]
        s.check("the corrected, chord-free score renders as the cover",
                track["cover"] and track["abc"] == melody.strip()
                and track["mode"] == "melody", str(track)[:160])

        # -- the writer as score editor ------------------------------------
        r = post("/api/abc/reharmonize", {"abc": NATIVE})
        s.equal("reharmonizing without a writer says so", r.status_code, 409)
        post("/api/llm/settings", {"provider": "custom",
                                   "base": model.url + "/v1", "model": "reharm"})
        r = post("/api/abc/reharmonize", {"abc": "X:1\nK:C\nC|"})
        s.equal("a score that cannot be read is refused up front",
                r.status_code, 400)
        r = post("/api/abc/reharmonize", {"abc": NATIVE, "ask": "jazz"})
        t = task(r.json()["task"]["id"])
        res = (t.get("meta") or {}).get("result") or {}
        s.check("a reharmonization comes back with new chords",
                t["state"] == "done" and '"Am7"' in res.get("abc", ""),
                t.get("detail", "")[:100])
        s.check("checked to keep the melody note for note",
                res.get("check", {}).get("match")
                and res["check"]["chords_changed"] == 2, str(res.get("check")))
        s.check("with its changes and new style",
                res.get("changes") and "jazz ballad" in res.get("style", ""))

        post("/api/llm/settings", {"model": "reharm-fix"})
        t = task(post("/api/abc/reharmonize", {"abc": NATIVE}).json()["task"]["id"])
        res = (t.get("meta") or {}).get("result") or {}
        s.check("an answer that moved the melody is sent back once, and fixed",
                t["state"] == "done" and res.get("attempts") == 2,
                t.get("detail", "")[:100])
        post("/api/llm/settings", {"model": "reharm-bad"})
        t = task(post("/api/abc/reharmonize", {"abc": NATIVE}).json()["task"]["id"])
        s.check("one that keeps moving it is refused, never handed back",
                t["state"] == "error" and "did not keep the melody" in t["detail"]
                and not (t.get("meta") or {}).get("result"), t.get("detail", "")[:120])

        # -- lyrics written to the score -----------------------------------
        post("/api/llm/settings", {"model": "qwen3:8b"})
        r = post("/api/lyrics/write", {"want": "fit", "abc": "X:1\nK:C\nC|"})
        s.equal("fitting lyrics to an unreadable score is refused", r.status_code, 400)
        r = post("/api/lyrics/write", {"want": "fit", "abc": NATIVE,
                                       "style": "folk", "lyrics": "old words"})
        t = task(r.json()["task"]["id"])
        res = (t.get("meta") or {}).get("result") or {}
        sent = requests.get(model.url + "/_last", timeout=5).json()["body"]
        ask = sent["messages"][-1]["content"]
        s.check("the writer is told each sung section and its note count",
                "[Verse]: 2 bars, 6 sung notes" in ask
                and "[Chorus]: 2 bars, 5 sung notes" in ask
                and "old words" in ask, ask[-400:])
        s.check("and the answer carries a section-by-section fit",
                t["state"] == "done" and res.get("fit")
                and res["fit"][0]["notes"] == 6, str(res.get("fit")))
    return s


if __name__ == "__main__":
    suite = run()
    sys.exit(1 if suite.failures else 0)
