# YuE Studio — invariants

## Hard rules (do not revisit)

1. **`web/index.html` stays one file with no build step.** CSS and JS inline.
2. **Never hard-code the ComfyUI API workflow.** `comfy.py` builds the graph
   from `/object_info` and matches input names through candidate lists. Node
   inputs get renamed between ComfyUI releases; a schema read turns that into a
   clear error instead of a silent wrong value.
3. **Never read a dropdown's choices as `spec[0]`.** `/object_info` writes
   combos two ways in the same graph — `[["a","b"], {…}]` for V1 nodes
   (`nodes.py`: CheckpointLoaderSimple, KSampler) and
   `["COMBO", {"options": […]}]` for V3 ones (`comfy_api`: every YuE2 and audio
   node). Go through `ComfyClient.combo_options()`, which handles both; reading
   `spec[0]` reports "no models installed" for half the graph. `format` on
   `SaveAudioAdvanced` is a third shape again — a dynamic combo whose chosen
   option drags in a sibling `quality` input that is not in the top-level
   schema, so `_format_extras()` fills it after `_node()` has run.
4. **Python detection is by execution, never `where python` / PATH lookup.**
   Windows Store stubs resolve on PATH and fail on run. `run.bat` and
   `bootstrap.find_python` both test with `-c "import sys; ..."`. Python is
   installable like Git and ffmpeg — `winget install 9NQ7512CXL7T` for the
   Store's Python install manager then `py install 3.13`, `brew install
   python`, `apt-get install python3 python3-venv` — so do not write that it
   has to be fetched by hand. Anything installed this way is invisible to the
   running process, which read PATH at startup: the follow-up step is marked
   optional in `_PACKAGES` and the user is told to restart.
5. **Model downloads are resumable.** Stream to `<name>.part`, `Range` header on
   retry, atomic `replace()` on completion. Never write straight to the final
   filename. **And never `replace()` a transfer that stopped short** — check
   what arrived against the `Content-Length` the server declared first. A cut
   connection can end `iter_content()` without raising, and promoting that
   leaves a truncated multi-gigabyte model under the real name, where it looks
   downloaded and fails much later inside the YuE nodes as `[Errno 22] Invalid
   argument`. Leaving it as `.part` is what lets the `Range` retry finish it.
6. **Never touch an existing ComfyUI's Python environment.** Dependency install
   runs only in managed mode, only inside `comfy-venv`.
7. **All HuggingFace work goes through the front end.** No CLI step, no manual
   file placement in the docs. Token, endpoint, repo, target folder and delete
   are all API-driven (`/api/hf/*`). The token is returned masked, never in full.
8. **Model deletes are path-checked.** Folder must be in `MODEL_FOLDERS`, name
   must contain no separator, and the resolved path must sit under `models_dir`.
9. **Long work is a Task, never a blocking request.** `manager.spawn()` returns
   immediately; the page polls `/api/tasks`. Cancel sets `task.cancel` and the
   worker checks it between chunks.
10. **Finished audio is copied into `data/tracks/`.** ComfyUI's output folder
    is not treated as storage.
11. **Nothing that does not look like ComfyUI is ever killed.**
    `take_over_port()` reads each holding pid's command line and refuses
    anything without `python` / `main.py` / `comfy` in it, naming it back to
    the person. A wrong address in Settings must not become a licence to close
    whatever is at it.
12. **`settled_free()` sleeps 2.0 s before calling a port free.** A supervisor
    — ComfyUI Desktop, a launcher script — respawns in under a second, so
    quiet is only free once it *stays* quiet. Report it free early and the
    managed engine starts into a port that is taken again by the time it
    binds; the three-attempt loop then reads the changed pid set and says
    "something is supervising it" instead of failing without a reason.
13. **Every (re)start calls `_refresh_schema_when_up()`.** `ComfyClient`
    caches `/object_info` for two minutes, so without it the whole point of
    the restart — a fresh model scan — hides behind the old cache and the
    page goes on saying there are no checkpoints.
14. **`kill_pid()` returns what the system said**, not a boolean: "stopped",
    "already gone", "access denied", "sent SIGKILL". An access-denied *is*
    the diagnosis, and swallowing it turns a one-line answer into an
    unexplained failure. `_pid_gone()` treats a zombie as gone — it answers
    `kill(pid, 0)` while holding no sockets, so counting it alive costs five
    seconds and then reports a SIGKILL that stopped nothing.
15. **`stale_models` keys off `checkpoints`, with the marker `"yue2"`.**
    ComfyUI scans its model folders once, at startup; weights that land after
    that are on disk and absent from its lists until a restart. The list that
    decides it is the one `pick_checkpoint()` reads, because its emptiness is
    what stops a song being made — not `audio_encoders`, which only Cover mode
    needs. `"yue2"` is the substring both downloaded checkpoints carry
    (`yue2_3b_int8_convrot`, `yue2_3b_bf16`). True only when nothing is
    missing from disk *and* the engine's own list has none of them; a file
    that is genuinely absent is a download problem and must not be reported
    as a stale scan.
16. **Two cures for a lost port, and they are not interchangeable.**
    `relocate_engine()` moves YuE Studio to a free port and leaves the other
    engine running — the right answer when the port is held by an install that
    is provably somebody else's (`foreign_engine_reason()`). `take_over_port()`
    closes what is there and puts ours in its place — the right answer for
    **Restart the engine**, and at boot for an engine at our own address that
    has gone stale, where moving aside would leave the old one holding the
    card and fix nothing. `ensure_engine_at_boot()` tries relocation first.
17. **A missing YuE2 node is never a reason to replace an engine.** Those
    nodes are part of ComfyUI itself from v0.35.0, so a restart cannot conjure
    them; treating their absence as a fault to fix would kill a working engine
    once per launch and never fix anything. (The reference implementation this
    was ported from *does* restart on that, because there the nodes come from
    custom-node packs that only load at startup.) `engine_trouble()` lists
    only what a restart actually cures.

18. **A model is complete when its own header says so, never when it clears a
    size in `MODELS`.** `safetensors_size()` reads the length a safetensors
    file declares for itself (u64 header length, then JSON whose
    `data_offsets` end at the true total), so a partial download is caught
    exactly — its header is intact, and what it promises is missing. The
    figures in `MODELS` are rounded approximations kept for progress
    read-outs; gating on one (the old check wanted 95% of it) turns a single
    stale number into a model that can never be seen as present. That is not
    cosmetic: `missing_models()` feeds `ready`, and a false "missing" leaves
    `/api/status` `ready: false` for ever, which makes **Create** a button
    that only reopens Setup. Songs stop, and the page blames the download.

19. **Only *required* weights gate `ready`.** `missing_required_models` is the
    list that clears the Create button, not `missing_models`. The cover
    encoder is fetched by default but read by Cover mode alone, which the page
    already greys out on `has_cover_model` — so counting it took every song
    away over a model no ordinary song would have touched.

20. **The graph needs an audio decoder, not a particular one.**
    `DECODE_NODES` is a preference order: `build_prompt()` takes the tiled
    decoder when it is there and the plain one when it is not, either way
    round. Naming `VAEDecodeAudioTiled` in `REQUIRED_NODES` declared a ComfyUI
    that renders songs perfectly well unable to run YuE2 at all.

21. **`/api/status` sends `recommended_duration`.** The page reads it for
    **Auto** and falls back to 180s when it is absent, so dropping it silently
    changes what every Auto song asks for. It is the engine's own tested
    default and is deliberately *not* `max_duration` — sending the absolute
    15-minute ceiling for every Auto song makes the node allocate for a
    quarter of an hour up front, which is where it has been seen to fall over.

## Validation gate — run after any edit

```bash
python -m py_compile server.py comfy.py bootstrap.py manager.py
python - <<'PY'                       # extract inline JS, then: node --check
import re, pathlib
src = pathlib.Path('web/index.html').read_text()
js = re.findall(r'<script>(.*?)</script>', src, re.S)
pathlib.Path('/tmp/app.js').write_text('\n'.join(js))
PY
node --check /tmp/app.js
```

A missing function declaration in the inline script kills all interactivity
silently — `node --check` is not optional.

`python tests/run.py` runs that gate and everything else (511 checks, about
three minutes); `python tests/run.py gate` is just the block above. Run the
whole suite before pushing. Tests take their own port and their own
`YUE_STUDIO_DATA` directory, so they never touch a real library. Four of them
need `ffmpeg` on PATH and fail without it — that is the machine, not the code.
The last 71 are the browser group and need Playwright (`pip install -r
requirements-dev.txt && python -m playwright install chromium`); without it
that group steps aside and the run stops at 440, which is a short count and
not a pass to compare against.

`python tests/run.py engine` is the group that owns real ComfyUI processes:
takeover, restart, the stale scan and what a launch does to an engine that is
already up. It kills processes it finds on the ports it chose, so do not point
it at a real install.

## What the UI maps onto

Every control in More Options is a real YuE2 or KSampler input — no decorative
sliders. Detail → KSampler steps. Lyric focus → `top_p`. Repetition →
`repetition_penalty`. Melody plan → the `mode` input, with Off dropping the
`YuE2GenerateABC` node. Vocal male/female appends to the style text, because the
model takes vocal type as words, not as a parameter — do not invent a node input
for it. There is deliberately **no** negative-prompt control: the reference
workflow wires KSampler's negative to the same conditioning as positive, so an
"exclude styles" box would do nothing.

## Version floor

ComfyUI **v0.35.0+** (native YuE2 nodes). Required node classes:
`YuE2GenerateMusic`, `YuE2GenerateABC`, `EmptyYuE2LatentAudio`,
`SaveAudioAdvanced`, and either `VAEDecodeAudioTiled` or `VAEDecodeAudio`
(rule 20), plus `SheetSage2AudioToABC` and `AudioEncoderLoader` for cover
mode.

Models live at `Comfy-Org/YuE2` on HuggingFace. URLs are in `bootstrap.MODELS`.

## Graph shape

Mirrors `assets/yue2_full_reference.json`:

```
CheckpointLoaderSimple ─ MODEL ─────────────────► KSampler ─ LATENT ─► VAEDecodeAudioTiled
                       ├ CLIP ─► YuE2GenerateABC ─ abc ─► YuE2GenerateMusic ─┤
                       └ VAE ──────────────────────────────────────────────► SaveAudioAdvanced
YuE2GenerateMusic ─ seconds ─► EmptyYuE2LatentAudio ─ LATENT ─► KSampler
```

Cover mode swaps `YuE2GenerateABC` for `LoadAudio → AudioEncoderLoader →
SheetSage2AudioToABC`. Both feed the same `abc` input. SheetSage has exactly
two modes — `melody` and `melody + chords` (`full`); there is no rhythm-only
mode — and the music node is always set to **match** the chosen transcription
mode, because its doc says to run it in the mode the score was made in. The
UI's "Cover takes" control picks between the two; anything else falls back to
`melody`.

A `PreviewAny` watches whichever ABC source is in play (14 for the generator,
21 for the cover branch) so the score comes back in the prompt's history under
`ui.text` and can be shown and edited. It only observes — `YuE2GenerateMusic`
still reads the score from its generator directly, so a ComfyUI without
`PreviewAny` loses the Score panel and nothing else. A score in the Score box
takes over completely: the generator and its preview are dropped and the text
goes into `abc` as a literal, which is exactly what the node documents
("Connect the ABC generator or supply an edited score").
