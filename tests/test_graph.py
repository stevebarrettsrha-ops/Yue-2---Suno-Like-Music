"""comfy.py — the graph handed to ComfyUI.

The mock validates every prompt the way ComfyUI does: unknown inputs, values
outside a combo's options, missing required inputs and dangling links are all
rejected, so "accepted" here means the real server would have taken it too.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from comfy import ComfyClient, ComfyError          # noqa: E402
from harness import Suite, comfy                   # noqa: E402


def run(slow: bool = False) -> Suite:
    s = Suite("graph")
    with comfy() as mock:
        client = ComfyClient(mock.url)
        schema = copy.deepcopy(client.schema())

        # -- the schema reads that the whole design rests on ---------------
        s.check("checkpoints read from a V1 combo",
                client.checkpoints() == ["yue2_3b_int8_convrot.safetensors",
                                         "yue2_3b_bf16.safetensors"])
        s.check("audio encoders read from a V3 combo",
                client.audio_encoders() == ["sheetsage2_bf16.safetensors"])
        s.check("formats read from a dynamic combo",
                client.save_formats() == ["flac", "mp3", "opus"])
        s.check("int8 checkpoint preferred over bf16",
                "int8" in client.pick_checkpoint())
        s.check("a named checkpoint is honoured",
                client.pick_checkpoint("yue2_3b_bf16.safetensors")
                == "yue2_3b_bf16.safetensors")

        # -- every route a song can take -----------------------------------
        cases = {
            "plan on": dict(style="reggae", lyrics="[verse]\nhi", duration=180),
            "melody plan": dict(style="reggae", lyrics="hi", mode="melody"),
            "plan off": dict(style="folk", lyrics="hi", use_abc=False),
            "instrumental": dict(style="jazz", lyrics="ignored", instrumental=True),
            "edited score": dict(style="folk", lyrics="hi", abc="X:1\nK:C\nCDEF|"),
            "cover": dict(style="soul", lyrics="hi", reference_audio="ref.wav"),
            "cover full": dict(style="soul", lyrics="hi",
                               reference_audio="ref.wav", cover_mode="full"),
            "cover junk mode": dict(style="soul", lyrics="hi",
                                    reference_audio="ref.wav",
                                    cover_mode="beats-only"),
            "mp3": dict(style="pop", lyrics="hi", format="mp3"),
            "opus": dict(style="pop", lyrics="hi", format="opus"),
            "unknown format falls back": dict(style="pop", lyrics="hi", format="wav"),
            "plain decode": dict(style="pop", lyrics="hi", tiled_decode=False),
        }
        built = {}
        for name, params in cases.items():
            try:
                b = client.build_prompt(params)
                client.queue(b["prompt"])
                built[name] = b
                s.check(f"{name}: ComfyUI accepts the prompt", True)
            except ComfyError as exc:
                s.check(f"{name}: ComfyUI accepts the prompt", False, str(exc)[:90])

        if len(built) == len(cases):
            g = lambda n: built[n]["prompt"]                      # noqa: E731
            s.check("plan on wires the generator into the music node",
                    g("plan on")["22"]["inputs"]["abc"] == ["23", 0])
            s.check("plan off sends no score at all",
                    g("plan off")["22"]["inputs"]["abc"] == ""
                    and "23" not in g("plan off"))
            s.check("an edited score goes in as text and drops the generator",
                    g("edited score")["22"]["inputs"]["abc"].startswith("X:1")
                    and "23" not in g("edited score"))
            s.check("instrumental sends no lyrics",
                    g("instrumental")["22"]["inputs"]["lyrics"] == "")
            s.check("cover transcribes the reference instead of planning",
                    g("cover")["22"]["inputs"]["abc"] == ["19", 0]
                    and "23" not in g("cover"))
            s.check("cover runs the melody mode SheetSage transcribes",
                    g("cover")["22"]["inputs"]["mode"] == "melody"
                    and g("cover")["19"]["inputs"]["mode"] == "melody")
            s.check("cover full puts both nodes in full mode",
                    g("cover full")["19"]["inputs"]["mode"] == "full"
                    and g("cover full")["22"]["inputs"]["mode"] == "full")
            s.check("an unknown cover mode falls back to melody",
                    g("cover junk mode")["19"]["inputs"]["mode"] == "melody"
                    and g("cover junk mode")["22"]["inputs"]["mode"] == "melody")
            s.check("mp3 brings the quality its format needs",
                    g("mp3")["10"]["inputs"].get("quality") == "V0")
            s.check("flac brings no quality input",
                    "quality" not in g("plan on")["10"]["inputs"])
            s.check("an unknown format becomes flac",
                    g("unknown format falls back")["10"]["inputs"]["format"] == "flac")
            s.check("tiled decode is used by default",
                    built["plan on"]["decode"] == "VAEDecodeAudioTiled")
            s.check("plain decode when asked",
                    built["plain decode"]["decode"] == "VAEDecodeAudio")
            s.check("KSampler's negative is the same conditioning as positive",
                    g("plan on")["8"]["inputs"]["negative"]
                    == g("plan on")["8"]["inputs"]["positive"] == ["22", 0])
            s.check("the latent takes its length from the music node",
                    g("plan on")["5"]["inputs"]["seconds"] == ["22", 1])

        # -- how long a song may run ---------------------------------------
        # max_duration is a ceiling: the model stops when the song ends, so a
        # bigger number never pads a short song, it only stops a long one being
        # cut off. Auto used to send 240 and cut every song at four minutes.
        s.equal("the ceiling is read from the engine, not assumed",
                client.duration_limit(), 900)
        s.equal("Auto uses the engine's tested default, not its hard ceiling",
                client.duration_default(), 360)
        for asked, sent in ((180, 180), (420, 420), (900, 900), (5000, 900)):
            g = client.build_prompt({"style": "x", "lyrics": "y",
                                     "duration": asked})
            s.equal(f"asking for {asked}s sends {sent}s",
                    g["prompt"]["22"]["inputs"]["max_duration"], sent)

        # -- sampler and scheduler follow the schema, not our preference ----
        ks = schema["KSampler"]["input"]["required"]
        for label, samplers, scheds in [
                ("dpm_2 offered", [["euler", "dpm_2"], {}],
                 [["normal", "sgm_uniform"], {}]),
                ("dpm_2 missing, schema has a default",
                 [["euler", "heun"], {"default": "heun"}],
                 [["normal", "beta"], {"default": "beta"}]),
                ("dpm_2 missing, no default", [["euler", "heun"], {}],
                 [["normal", "beta"], {}]),
                ("V3-shaped combos",
                 ["COMBO", {"options": ["res_multistep"], "default": "res_multistep"}],
                 ["COMBO", {"options": ["beta"]}])]:
            bent = ComfyClient(mock.url)
            bent._schema, bent._schema_at = copy.deepcopy(schema), 9e18
            bent._schema["KSampler"]["input"]["required"]["sampler_name"] = samplers
            bent._schema["KSampler"]["input"]["required"]["scheduler"] = scheds
            inputs = bent.build_prompt({"style": "x"})["prompt"]["8"]["inputs"]
            s.check(f"sampler stays valid when {label}",
                    inputs["sampler_name"] in bent.combo_options(samplers)
                    and inputs["scheduler"] in bent.combo_options(scheds),
                    f"{inputs['sampler_name']}/{inputs['scheduler']}")

        # -- what the user is told when the engine cannot do the job -------
        def without(*classes):
            bent = ComfyClient(mock.url)
            bent._schema = {k: v for k, v in copy.deepcopy(schema).items()
                            if k not in classes}
            bent._schema_at = 9e18
            return bent

        s.fails_with("a ComfyUI without the YuE2 nodes says so",
                     lambda: without("YuE2GenerateMusic").build_prompt({"style": "x"}),
                     ComfyError, "v0.35.0 or newer")
        s.fails_with("a renamed KSampler input is named, not guessed",
                     lambda: _drop_input(schema, mock.url, "KSampler", "model")
                     .build_prompt({"style": "x"}),
                     ComfyError, "no input for 'model'")
        s.fails_with("cover without the SheetSage model",
                     lambda: _blank_combo(schema, mock.url, "AudioEncoderLoader",
                                          "audio_encoder_name")
                     .build_prompt({"style": "x", "reference_audio": "r.wav"}),
                     ComfyError, "sheetsage")

        # -- an empty checkpoint list is not proof the file is missing -----
        # ComfyUI caches its model listing and this client caches the schema
        # on top, so a checkpoint that arrives late, or a ComfyUI that has
        # restarted, reads as "nothing installed" until something rescans.
        with comfy(**{"MOCK_BLANK_CKPT_CALLS": "1"}) as late:
            client_late = ComfyClient(late.url)
            s.check("a checkpoint hidden by a stale listing reads as empty",
                    client_late.checkpoints() == [])
            s.check("and building a song refetches rather than giving up",
                    "int8" in client_late.pick_checkpoint())

        with comfy(**{"MOCK_BLANK_CKPT_CALLS": "9999"}) as none:
            s.fails_with("a ComfyUI that really lists no checkpoints says so",
                         lambda: ComfyClient(none.url).build_prompt({"style": "x"}),
                         ComfyError, "lists no checkpoints")
            s.fails_with("and points at a restart, not at downloading again",
                         lambda: ComfyClient(none.url).build_prompt({"style": "x"}),
                         ComfyError, "start it again")

        # -- either audio decoder will do -----------------------------------
        # build_prompt() takes whichever of the two this ComfyUI has, so
        # demanding the tiled one declared an engine that renders songs
        # perfectly well unable to run YuE2 — ready stayed False and Create
        # only ever reopened Setup.
        no_tiled = without("VAEDecodeAudioTiled")
        b = no_tiled.build_prompt({"style": "x", "lyrics": "y"})
        s.check("a ComfyUI with only the plain decoder is still supported",
                b["decode"] == "VAEDecodeAudio")
        try:
            no_tiled.queue(b["prompt"])
            s.check("and its prompt is accepted", True)
        except ComfyError as exc:
            s.check("and its prompt is accepted", False, str(exc)[:80])

        only_tiled = without("VAEDecodeAudio")
        s.check("asking for untiled output on an engine without it still decodes",
                only_tiled.build_prompt({"style": "x", "tiled_decode": False})
                ["decode"] == "VAEDecodeAudioTiled")

        s.fails_with("a ComfyUI with neither decoder says so",
                     lambda: without("VAEDecodeAudioTiled", "VAEDecodeAudio")
                     .build_prompt({"style": "x"}),
                     ComfyError, "VAEDecodeAudioTiled or VAEDecodeAudio")

        # -- PreviewAny is a nicety, never a requirement --------------------
        no_preview = without("PreviewAny")
        b = no_preview.build_prompt({"style": "x", "lyrics": "y"})
        s.check("without PreviewAny the song still renders, minus the score",
                b["abc_node"] is None and "14" not in b["prompt"])
        try:
            no_preview.queue(b["prompt"])
            s.check("and that prompt is still accepted", True)
        except ComfyError as exc:
            s.check("and that prompt is still accepted", False, str(exc)[:80])

        # -- an unreachable engine must not explode ------------------------
        dead = ComfyClient(f"http://127.0.0.1:{9}")
        s.check("an unreachable ComfyUI reads as empty, not an error",
                dead.checkpoints() == [] and dead.queue_state() == {}
                and dead.is_running("x") is False)
        for call in (lambda: dead.stop("x"), dead.interrupt):
            try:
                call()
                s.check("stopping a song on a dead engine stays quiet", True)
            except Exception as exc:  # noqa: BLE001
                s.check("stopping a song on a dead engine stays quiet", False,
                        type(exc).__name__)
    return s


def _bend(schema, url):
    client = ComfyClient(url)
    client._schema, client._schema_at = copy.deepcopy(schema), 9e18
    return client


def _drop_input(schema, url, node, name):
    client = _bend(schema, url)
    client._schema[node]["input"]["required"].pop(name)
    return client


def _blank_combo(schema, url, node, name):
    client = _bend(schema, url)
    client._schema[node]["input"]["required"][name] = ["COMBO", {"options": []}]
    return client


if __name__ == "__main__":
    suite = run("--slow" in sys.argv)
    print(f"\n{suite.name}: {suite.passed} passed, {len(suite.failures)} failed")
    sys.exit(1 if suite.failures else 0)
