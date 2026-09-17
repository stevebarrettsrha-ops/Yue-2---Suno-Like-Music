# The test suite

```bash
python tests/run.py                # everything, about two minutes
python tests/run.py graph api      # only those
python tests/run.py --list         # what there is
```

Nothing extra is needed for the first seven groups — they use what YuE Studio
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
| `hardening` | Fields of the wrong type, paths that try to climb out, requests addressed elsewhere, and config or library files damaged behind the app's back. |
| `load` | 300 requests at once, 48 songs queued together with deletes landing on top, then what the process looks like afterwards. |
| `ui` | The interface in a real browser — making, playing, sorting, stopping and setting up. Any uncaught script error fails the run. |

## How it stays out of your way

Every test gets a free port and its own data directory through
`YUE_STUDIO_DATA`, so a run never touches a real library or config, and two
runs cannot collide. Servers are started and stopped by `harness.py`, whatever
happens in the test.

`mock_comfy.py` stands in for ComfyUI. It serves `/object_info` shaped the way
the real thing shapes it — V1 combos for the core loaders, V3 for the YuE2 and
audio nodes, and a dynamic combo for `SaveAudioAdvanced.format` — runs one
prompt at a time behind a real queue, speaks the progress websocket, and
validates every prompt the way ComfyUI does. A prompt it accepts is one the
real server would have accepted too.

It is a stand-in, not the real thing: it never loads a model. Inference,
VRAM behaviour and multi-minute renders are the one thing this suite cannot
tell you about.

## Adding a test

Each group is a module with a `run(slow=False)` returning a `Suite`. Use
`s.check(what, ok, detail)`, `s.equal(...)` or `s.fails_with(...)`, and phrase
`what` as the thing a person would want to be true — the output is read by
someone deciding whether to trust a change.
