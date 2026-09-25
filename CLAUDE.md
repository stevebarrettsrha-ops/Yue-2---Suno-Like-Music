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

22. **Every child process is read as UTF-8, with `errors="replace"`.**
    `text=True` alone decodes through the locale encoding, which on a Windows
    console is cp1252 — where the partial blocks a progress bar draws
    (`U+258D`, `U+258F`), and `U+23F3`, are undefined bytes that raise. The
    read then dies inside the loop, and because that loop is a daemon thread
    nothing surfaces it: the Engine console freezes mid-start with no error,
    while the engine itself may be running perfectly. Four readers need this —
    `ComfyProcess.start`, `_pip`, `manager.stream` and `_run`.

23. **An engine that stops says so.** `_pump()` ends when the process's output
    does, so it waits for the exit code and writes it into the same console.
    Without that the log simply stops, and a crash looks exactly like a slow
    first start for ever. `engine_note()` carries the same distinction to the
    Engine row, which otherwise reports "ComfyUI is not answering" whether
    nothing was ever started or it died thirty seconds ago. Strip ANSI on the
    way in, or ComfyUI's colours arrive as literal `[32m[INFO][0m`.

24. **The song model is the person's to choose, and the choice sticks.**
    `#ckpt` on the Create page is a real control, not a display: "auto" means
    `pick_checkpoint()`, which prefers int8 because it loads on far more
    cards — so on a machine that has both, auto never reaches bf16 and the
    only way to the full-precision model is to name it. That makes losing the
    choice a silent downgrade, which is why it rides in the draft, is restored
    once the engine's list arrives (the options come from `/api/status`, so
    they are not there yet when the draft is read), and is what **reuse**
    loads back — the library badges every song INT8 or BF16, so re-rendering a
    BF16 song on int8 contradicts the page. The status poll writes to the
    select **only when the engine's list actually changed**: on every pass it
    churns the DOM under an open dropdown and can overwrite a choice being
    made. While the list stands, the dropdown is the truth.

25. **"Ready" is not "the port answered".** ComfyUI replies to
    `/system_stats` before it has finished loading, so a first launch showed
    **Engine ready** while the engine was still coming up — and a song made in
    that window fails inside the nodes for reasons that have nothing to do
    with the song. `engine_starting()` withholds `ready` until ComfyUI prints
    its own "Starting server" banner, and only for a process YuE Studio
    started: an adopted engine was up before us and is never held back. A
    build whose banner is not recognised is released after `STARTING_GRACE`,
    so an unfamiliar ComfyUI cannot be kept from working for ever.

26. **`[Errno 22] Invalid argument` gets its cause attached.** It is Windows'
    answer to several unrelated refusals and the YuE2 nodes raise it for at
    least two — a song longer than the build can allocate for, and a
    checkpoint too large for the card. Alone it sends people to look at their
    lyrics, which is never where the fault is. `explain_failure()` keeps the
    engine's own words and adds the reading: the duration when it is long,
    the two numbers when the model outweighs the card, and — once the
    compatibility retry has failed too — that length and sampling are now
    ruled out rather than repeating advice already tried. The retry's
    `warning` is rendered on the job card: it changes the song's length and
    sampling to get through, and doing that silently left the settings on
    screen disagreeing with the song that came out.

27. **A song is timed out for stalling, never for being slow, and never
    for queueing.** ComfyUI renders one at a time, so `queued_at` and
    `run_since` are different moments and the gap between them belongs to the
    songs ahead. Charging a song for it timed the second of a pair out while
    it had not begun rendering — worse the slower the model, which is exactly
    when people make two. Nothing is owed while it waits (`NEVER_STARTED_LIMIT`
    only catches a prompt ComfyUI has forgotten), and once it is running the
    clock is `STALL_LIMIT` since the last thing ComfyUI *reported* — the node
    it reached or the step it is on. A total-elapsed cap killed healthy
    renders, which is what a big model on a small card looks like: slow, but
    always moving.

28. **The lyric writer is optional, and its key goes nowhere it was not
    given for.** `lyricist.py` is never on the path to a song: no writer set
    up means the Write button points at Settings and nothing else changes.
    The key follows rule 7 — masked to its last four, never in any response
    (`POST /api/config` once returned the whole config, HF token included; it
    returns the public keys only) — and is saved with `llm_key_base`, the
    address it was typed for. An address that changes without a new key
    drops it, and a key goes only over https or to loopback: a wrong address
    must not become a way to hand someone a paid key. An environment key
    (`ANTHROPIC_API_KEY` …) is read only for its provider's own default
    address. A write is a Task (rule 9) that streams and checks
    `task.cancel` between chunks; one runs at a time, because a second press
    pays for a second answer. Reasoning a local model thinks out loud
    (`<think>…</think>`) is stripped before anything reaches the page.
    Claude goes through the official `anthropic` SDK with the server-side
    refusal fallback on for the models that take it; every other service is
    plain OpenAI-compatible chat completions.

29. **Plan first is the first pass alone, and it makes no audio.**
    `build_plan_prompt()` is `CheckpointLoaderSimple → YuE2GenerateABC →
    PreviewAny`, or `LoadAudio → AudioEncoderLoader → SheetSage2AudioToABC →
    PreviewAny` for a recording, built by the same `_planner()` /
    `_transcriber()` that `build_prompt()` uses, so a plan and a render can
    never drift apart. The chosen plan is rendered by the ordinary
    `/api/generate` as a literal score. Plans run as a Task (rule 9) and are
    waited on until ComfyUI no longer has them anywhere, never for a set
    time (rule 27). Several plans use consecutive seeds, so a set can be made
    again exactly. A recording is transcribed once. Without `PreviewAny`
    (`can_plan`), planning is unavailable and songs are still made in one
    pass. `YuE2RenderPlan` is a custom-node pack's node, not ComfyUI's: do
    not depend on it (rule 6); the native nodes already split plan from
    render.

30. **A score check advises; it never gates.** `yue2_abc.py` is YuE's own
    helper, vendored unchanged (Apache-2.0); edit around it in `scores.py`,
    never in it. It fails closed on anything outside the native dialect, and
    the test suite's own short scores are outside it — so the Score box
    shows the reason and sends the score anyway. What *is* gated is
    `run_reharm()`: a model's reharmonization is used only if
    `scores.compare()` shows every sounding note of the kept voices
    unchanged (pitch, onset and duration after ties). One repair round with
    the exact differences, then a refusal, and the Score box is left as it
    was. Compare events, never text.

31. **A song records what would make it again.** The track keeps its seed,
    score and where the score came from (`abc_source`: planned, transcribed,
    score, none), mode, cover mode and sampling (`top_p`, `top_k`,
    `repetition_penalty`, steps, scheduler). **Render again from its plan**
    loads all of it, and **Compare** shows two songs' records side by side.
    Drop a field and a render stops being reproducible.

32. **Audio in means melody in — never a voice.** YuE2 has no
    audio-reference input; SheetSage2 reads notes. So **Voice** is words (the
    male/female choice and the description both join the style text, like
    rule "What the UI maps onto" says) plus *Sing or hum your melody*, which
    transcribes a recording into the Score box and leaves the song *not* a
    cover. Do not label anything "your voice" or suggest cloning: the page
    says what is taken from audio, and that is the tune. Uploads and browser
    recordings are kept in `data/references/` *before* a copy goes to
    ComfyUI's input folder (rule 10: ComfyUI is not storage), so a ComfyUI
    that is down loses nothing. Reference ids are 12 hex characters and files
    are plain names inside that folder; nothing else is served or deleted.

## Validation gate — run after any edit

```bash
python -m py_compile server.py comfy.py bootstrap.py manager.py lyricist.py \
    scores.py yue2_abc.py
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

`python tests/run.py` runs that gate and everything else (735 checks, about
five minutes); `python tests/run.py gate` is just the block above. Run the
whole suite before pushing. Tests take their own port and their own
`YUE_STUDIO_DATA` directory, so they never touch a real library. Four of them
need `ffmpeg` on PATH and fail without it — that is the machine, not the code.
The last 125 are the browser group and need Playwright (`pip install -r
requirements-dev.txt && python -m playwright install chromium`); without it
that group steps aside and the run stops at 610, which is a short count and
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
