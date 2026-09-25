# The test suite

```bash
python tests/run.py                # everything, about two minutes
python tests/run.py graph api      # only those
python tests/run.py --list         # what there is
```

Nothing extra is needed for the first eight groups — they use what YuE Studio
already depends on. The browser tests want Playwright, and say so and step
aside when it is missing:

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium
```

## What runs, and why

| Group | What it covers |
|---|---|
| `gate` | The checks CLAUDE.md asks for after any edit: every module compiles, the inline script parses, every `$("id")` it reaches for exists, `run.sh` parses. |
| `graph` | The prompt built from `/object_info` — every route a song can take, both combo shapes, the dynamic `format` input, and the message you get when a ComfyUI cannot do the job. |
| `library` | The song list under concurrent writes, damaged `library.json`, track filenames, how long a song really is, and which finished jobs stay visible. |
| `setup` | Addresses people type, finding the interpreter an existing ComfyUI runs on, deleting a model file, resumable downloads, and every answer HuggingFace can give. |
| `api` | The HTTP surface end to end: a song of every kind, playing and seeking, renaming and deleting, stopping the right song, and ComfyUI going away mid-render. |
| `lyrics` | The lyric writer: reading whatever a model answers (labels, markdown, fences, JSON, `<think>` reasoning), the key bound to its address and never sent back, one write at a time, Stop, and a real round trip over both the OpenAI-compatible and Anthropic streams. |
| `engine` | The engine process itself: the console endpoint, all four restart routes, taking a port back from an orphan, refusing to kill what is not ComfyUI, naming a supervisor that puts it straight back, the stale model scan, and what a launch does to an engine that is already up. |
| `hardening` | Fields of the wrong type, paths that try to climb out, requests addressed elsewhere, and config or library files damaged behind the app's back. |
| `load` | 300 requests at once, 48 songs queued together with deletes landing on top, then what the process looks like afterwards. |
| `ui` | The interface in a real browser — making, playing, sorting, stopping, setting up, and writing lyrics with a writer chosen in Settings. Any uncaught script error fails the run. |

## How it stays out of your way

Every test gets a free port and its own data directory through
`YUE_STUDIO_DATA`, so a run never touches a real library or config, and two
runs cannot collide. Servers are started and stopped by `harness.py`, whatever
happens in the test.

`mock_llm.py` stands in for a chat model. The model name picks what it does
— a normal song, a plain JSON body, a slow stream for Stop, a refused key,
a Claude refusal — and `/_last` shows what was actually sent to it.

`mock_comfy.py` stands in for ComfyUI. Its `/object_info` is not written by
hand: `object_info.json` is a capture from a real server, trimmed to the nodes
this graph uses, so the three combo shapes, the dynamic `format` input and
every default are the real ones. It runs one prompt at a time behind a real
queue, speaks the progress websocket, encodes whichever format the prompt asked
for, and validates every prompt the way ComfyUI does — so a prompt it accepts
is one the real server accepts too. That last claim was checked directly:
every graph this app builds was queued against a real ComfyUI 0.36.0 and
accepted.

To refresh the capture against a newer ComfyUI, start one and run:

```bash
python - <<'EOF'
import json, requests, pathlib
NEEDED = ["CheckpointLoaderSimple", "KSampler", "YuE2GenerateABC",
          "YuE2GenerateMusic", "EmptyYuE2LatentAudio", "VAEDecodeAudio",
          "VAEDecodeAudioTiled", "SaveAudioAdvanced", "SheetSage2AudioToABC",
          "AudioEncoderLoader", "LoadAudio", "PreviewAny"]
real = requests.get("http://127.0.0.1:8188/object_info", timeout=60).json()
keep = {n: {k: real[n].get(k, []) for k in ("input", "output", "output_name")}
        for n in NEEDED if n in real}
pathlib.Path("tests/object_info.json").write_text(
    json.dumps(keep, indent=1, sort_keys=True))
EOF
```

Then put the model names back into the three combos that list files
(`ckpt_name`, `audio_encoder_name`, `audio`), which are empty on a machine
with no models installed.

It is a stand-in, not the real thing: it never loads a model. Inference,
VRAM behaviour and multi-minute renders are the one thing this suite cannot
tell you about.

The `engine` group needs real processes rather than a stand-in, because what
it tests is whether the app finds, names and stops the right one.
`harness.fake_install()` builds a pretend ComfyUI checkout whose `main.py`
serves the mock and reports the folder in its `/system_stats` argv, so the app
can own, stop and restart it for real; `supervised_comfy()` wraps that mock in
a parent that puts it back whenever it dies, the way ComfyUI Desktop and
launcher scripts do; and `port_squatter()` answers `/system_stats` from a
process whose command line says nothing about python or ComfyUI, which is what
a forwarded port looks like and what must never be killed.

## Adding a test

Each group is a module with a `run(slow=False)` returning a `Suite`. Use
`s.check(what, ok, detail)`, `s.equal(...)` or `s.fails_with(...)`, and phrase
`what` as the thing a person would want to be true — the output is read by
someone deciding whether to trust a change.
