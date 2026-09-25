"""The lyric writer: reading answers, guarding the key, and a real round trip
against a stand-in model that speaks both wire formats."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Suite, Workspace, comfy, llm, studio, wait_for  # noqa: E402

import lyricist  # noqa: E402


class FakeTask:
    def __init__(self) -> None:
        self.cancel = False
        self.meta: dict = {}
        self.lines: list[str] = []
        self.state = "running"
        self.detail = ""

    def log(self, msg: str) -> None:
        self.lines.append(msg)

    def set(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


def unit(s: Suite) -> None:
    # -- reading what a model says ------------------------------------------
    plain = ("TITLE: Paper Moon\nSTYLE: English, synth-pop, 118 BPM\nLYRICS:\n"
             "[Verse]\nline one\n[Chorus]\nline two")
    got = lyricist.parse(plain)
    s.equal("a labelled answer gives its title", got["title"], "Paper Moon")
    s.equal("and its style", got["style"], "English, synth-pop, 118 BPM")
    s.equal("and lyrics with sections kept apart", got["lyrics"],
            "[Verse]\nline one\n\n[Chorus]\nline two")

    fancy = ("```\n<think>plan: two verses</think>\n## **Title:** \"Glass\"\n"
             "**Style:**\nEnglish, dream pop, airy female vocal\n\n"
             "[Lyrics]\n[Intro]\n\n\n\n[Verse]\nwords\n```")
    got = lyricist.parse(fancy)
    s.equal("markdown, fences and quotes are stripped from the title",
            got["title"], "Glass")
    s.equal("a style on the line after its heading is found", got["style"],
            "English, dream pop, airy female vocal")
    s.check("reasoning in <think> never reaches the lyrics",
            "plan" not in got["lyrics"] and got["lyrics"].startswith("[Intro]"),
            repr(got["lyrics"]))
    s.check("runs of blank lines collapse", "\n\n\n" not in got["lyrics"])

    s.equal("an unterminated <think> is hidden too",
            lyricist.visible("<think>still going"), "")
    got = lyricist.parse('{"title":"J","style":"jazz","lyrics":"[Verse]\\nhi"}')
    s.check("a JSON answer is read as one", got["title"] == "J"
            and got["lyrics"] == "[Verse]\nhi", str(got))
    got = lyricist.parse("[Verse]\nno labels\n[Chorus]\nat all", "lyrics")
    s.check("an unlabelled answer is kept as lyrics, with a warning",
            got["lyrics"].startswith("[Verse]") and got["warnings"], str(got))
    got = lyricist.parse("TITLE: x\nSTYLE: y\nLYRICS:\nno tags here")
    s.check("lyrics without section tags are flagged",
            any("tags" in w for w in got["warnings"]), str(got["warnings"]))
    got = lyricist.parse("TITLE: x\nSTYLE: y\nLYRICS:\n[Verse]\nThe style: "
                         "is not a heading here\n[Chorus]\nLyrics: neither")
    s.check("once in the lyrics, a line that looks like a label stays a lyric",
            "The style: is not a heading here" in got["lyrics"]
            and got["style"] == "y", repr(got))

    # -- what the model is asked ---------------------------------------------
    ask = lyricist.clean_request({"want": "nonsense", "duration": "99999",
                                  "vocal": "robot", "brief": 5})
    s.check("a malformed ask is bounded, not rejected",
            ask["want"] == "song" and ask["duration"] == 900
            and ask["vocal"] == "" and ask["brief"] == ""
            and ask["language"] == "English", str(ask))
    prompt = lyricist.user_prompt(lyricist.clean_request({
        "brief": "trains at night", "vocal": "female", "duration": 150,
        "language": "Spanish", "style": "bolero"}))
    s.check("the prompt carries brief, language, voice, style and length",
            all(x in prompt for x in ("trains at night", "Language: Spanish",
                                      "female vocal", "bolero", "150 seconds")),
            prompt)
    prompt = lyricist.user_prompt(lyricist.clean_request({
        "vocal": "instrumental", "language": "French"}))
    s.check("an instrumental asks for tags only and names no language",
            "only section tags" in prompt and "French" not in prompt, prompt)

    # -- the key -------------------------------------------------------------
    cfg: dict = {}
    s.equal("an address with a key in it is refused",
            lyricist.clean_base("http://user:pw@host/v1"), "")
    s.equal("so is one with a query", lyricist.clean_base("http://h/v1?key=1"), "")
    s.equal("a plain one is kept, without its trailing slash",
            lyricist.clean_base("http://127.0.0.1:11434/v1/"),
            "http://127.0.0.1:11434/v1")
    s.fails_with("a bad address is an error, not silently the default",
                 lambda: lyricist.apply_settings(cfg, {"base": "ftp://x"}),
                 lyricist.LyricistError, "not usable")
    lyricist.apply_settings(cfg, {"provider": "openai", "key": "sk-test-1234",
                                  "model": "m"})
    s.equal("a key is bound to the address it was entered for",
            cfg.get("llm_key_base"), "https://api.openai.com/v1")
    view = lyricist.settings_view(cfg)
    s.check("the settings view never carries the key",
            "sk-test-1234" not in str(view) and view["key_hint"] == "…1234",
            str(view)[:120])
    lyricist.apply_settings(cfg, {"base": "https://elsewhere.example/v1"})
    s.check("moving the address drops the key instead of sending it on",
            not cfg.get("llm_key") and lyricist.key_of(cfg) == ("", ""))
    s.fails_with("a key with a line break is refused",
                 lambda: lyricist.apply_settings(cfg, {"key": "ab\ncd"}),
                 lyricist.LyricistError, "printable")
    remote = {"llm_provider": "custom", "llm_base": "http://10.0.0.5:8000/v1",
              "llm_model": "m", "llm_key": "k-123456",
              "llm_key_base": "http://10.0.0.5:8000/v1"}
    s.fails_with("a key is not sent over plain http to another machine",
                 lambda: lyricist.connection(remote), lyricist.LyricistError,
                 "https")
    local = dict(remote, llm_base="http://127.0.0.1:8000/v1",
                 llm_key_base="http://127.0.0.1:8000/v1")
    s.check("but may go to this computer",
            lyricist.connection(local)["key"] == "k-123456")
    s.fails_with("a service that needs a key says so",
                 lambda: lyricist.connection({"llm_provider": "anthropic"}),
                 lyricist.LyricistError, "api key")
    s.fails_with("a server with no model chosen says so",
                 lambda: lyricist.connection({"llm_provider": "ollama"}),
                 lyricist.LyricistError, "model")


def write(api: str, body: dict, timeout: float = 30) -> dict:
    r = requests.post(f"{api}/api/lyrics/write", json=body, timeout=10)
    data = r.json()
    if r.status_code != 200:
        return {"http": r.status_code, **data}
    tid = data["task"]["id"]
    wait_for(lambda: requests.get(f"{api}/api/tasks?id={tid}", timeout=10)
             .json()["state"] != "running", timeout, 0.2)
    return requests.get(f"{api}/api/tasks?id={tid}", timeout=10).json()


def run(slow: bool = False) -> Suite:
    s = Suite("lyrics")
    unit(s)

    with comfy(delay=1) as engine, llm() as model, Workspace() as data, \
            studio(engine.url, data, hf_token="hf_secret_token") as app:
        api = app.url
        st = requests.get(f"{api}/api/status", timeout=10).json()
        s.check("with no writer set up, status says so",
                st.get("lyric_writer") is False, str(st.get("lyric_writer")))
        r = requests.post(f"{api}/api/lyrics/write", json={"brief": "x"},
                          timeout=10)
        s.check("and a write is refused with the reason",
                r.status_code == 409 and "model" in r.json().get("error", ""),
                r.text[:100])

        r = requests.post(f"{api}/api/config", json={"comfy_url": engine.url},
                          timeout=10)
        s.check("saving settings does not hand back the saved HF token",
                "hf_secret_token" not in r.text, r.text[:120])

        # -- a local OpenAI-compatible server ------------------------------
        base = model.url + "/v1"
        r = requests.post(f"{api}/api/llm/settings", json={
            "provider": "custom", "base": base, "model": "qwen3:8b"}, timeout=10)
        s.check("a local server is saved", r.status_code == 200, r.text[:100])
        models = requests.get(f"{api}/api/llm/models", timeout=10).json()
        s.equal("its models are listed", models.get("models"),
                ["gemma3:12b", "qwen3:8b"])
        s.check("status now reports a writer",
                requests.get(f"{api}/api/status", timeout=10)
                .json()["lyric_writer"] is True)

        t = write(api, {"want": "song", "brief": "a late train home",
                        "vocal": "male", "duration": 120})
        res = (t.get("meta") or {}).get("result") or {}
        s.check("a whole song comes back", t.get("state") == "done"
                and res.get("title") == "Last Train Home"
                and res.get("style", "").startswith("English, indie folk")
                and res.get("lyrics", "").startswith("[Verse]"), str(t)[:200])
        s.check("without the model's reasoning in it",
                "<think>" not in str(t) and "Plan:" not in str(res))
        sent = requests.get(model.url + "/_last", timeout=5).json()
        s.check("the model was asked with the brief, voice and length",
                "a late train home" in str(sent["body"])
                and "male vocal" in str(sent["body"])
                and "120 seconds" in str(sent["body"])
                and sent["body"].get("stream") is True, str(sent["body"])[:160])
        s.check("and no key went to a server that was given none",
                "Authorization" not in sent["headers"])

        t = write(api, {"want": "lyrics", "style": "bossa nova, 70 BPM",
                        "brief": "rain"})
        res = (t.get("meta") or {}).get("result") or {}
        s.equal("lyrics-only keeps the style the person wrote",
                res.get("style"), "bossa nova, 70 BPM")
        t = write(api, {"want": "song", "brief": "calm", "vocal": "instrumental"})
        res = (t.get("meta") or {}).get("result") or {}
        s.equal("an instrumental keeps only the section tags",
                res.get("lyrics"), "[Verse]\n\n[Chorus]\n\n[Outro]")
        r = requests.post(f"{api}/api/lyrics/write",
                          json={"want": "polish", "lyrics": "  "}, timeout=10)
        s.equal("polishing nothing is refused up front", r.status_code, 400)

        for name, want in (("json", "a plain JSON answer is read too"),
                           ("bare", "an unlabelled answer still lands")):
            requests.post(f"{api}/api/llm/settings", json={"model": name},
                          timeout=10)
            t = write(api, {"brief": "x"})
            res = (t.get("meta") or {}).get("result") or {}
            s.check(want, t.get("state") == "done"
                    and res.get("lyrics", "").startswith("[Verse]"), str(t)[:160])

        requests.post(f"{api}/api/llm/settings", json={"model": "fail401"},
                      timeout=10)
        t = write(api, {"brief": "x"})
        s.check("a refused key is reported as that",
                t.get("state") == "error" and "refused the API key" in t["detail"],
                t.get("detail", "")[:100])

        # -- one at a time, and Stop ----------------------------------------
        requests.post(f"{api}/api/llm/settings", json={"model": "slow"},
                      timeout=10)
        first = requests.post(f"{api}/api/lyrics/write", json={"brief": "x"},
                              timeout=10).json()["task"]["id"]
        wait_for(lambda: "draft" in (requests.get(
            f"{api}/api/tasks?id={first}", timeout=5).json().get("meta") or {}),
            15, 0.2)
        again = requests.post(f"{api}/api/lyrics/write", json={"brief": "x"},
                              timeout=10)
        s.equal("a second write while one streams is refused",
                again.status_code, 409)
        requests.post(f"{api}/api/tasks/{first}/cancel", timeout=5)
        stopped = wait_for(lambda: requests.get(
            f"{api}/api/tasks?id={first}", timeout=5).json()["state"]
            == "cancelled", 10, 0.2)
        s.check("Stop ends a write part way", stopped)

        # -- Claude, through the SDK, against the stand-in -------------------
        r = requests.post(f"{api}/api/llm/settings", json={
            "provider": "anthropic", "base": model.url,
            "model": "claude-opus-5", "key": "sk-ant-test-9876"}, timeout=10)
        view = requests.get(f"{api}/api/llm/settings", timeout=10).json()
        s.check("a Claude key is saved and shown only by its end",
                view["key_set"] and view["key_hint"] == "…9876"
                and "sk-ant-test" not in str(view), str(view)[:120])
        t = write(api, {"want": "song", "brief": "trains"})
        res = (t.get("meta") or {}).get("result") or {}
        s.check("Claude writes a song through the SDK",
                t.get("state") == "done" and res.get("title") == "Last Train Home",
                str(t)[:200])
        sent = requests.get(model.url + "/_last", timeout=5).json()
        hdr = {k.lower(): v for k, v in sent["headers"].items()}
        s.check("with the key in x-api-key", hdr.get("x-api-key") == "sk-ant-test-9876")
        s.check("and the refusal fallback on for Opus 5",
                sent["body"].get("fallbacks") == "default"
                and lyricist.FALLBACK_BETA in hdr.get("anthropic-beta", ""),
                str(sent["body"].get("fallbacks")) + " " + hdr.get("anthropic-beta", ""))
        models = requests.get(f"{api}/api/llm/models", timeout=10).json()
        s.equal("Claude's models are listed", models.get("models"),
                ["claude-opus-5"])
        requests.post(f"{api}/api/llm/settings", json={"model": "claude-refuse"},
                      timeout=10)
        t = write(api, {"brief": "x"})
        s.check("a refusal is reported, not returned as empty lyrics",
                t.get("state") == "error" and "declined" in t.get("detail", ""),
                t.get("detail", "")[:100])
        sent = requests.get(model.url + "/_last", timeout=5).json()
        s.check("and a model without the fallback is asked without it",
                "fallbacks" not in sent["body"])

        requests.post(f"{api}/api/llm/settings",
                      json={"base": model.url + "/elsewhere"}, timeout=10)
        view = requests.get(f"{api}/api/llm/settings", timeout=10).json()
        s.check("moving Claude's address drops the saved key",
                not view["key_set"], str(view)[:120])
        cfg_file = (data / "config.json").read_text()
        s.check("and it is gone from the config file too",
                "sk-ant-test-9876" not in cfg_file)
    return s


if __name__ == "__main__":
    suite = run()
    sys.exit(1 if suite.failures else 0)
