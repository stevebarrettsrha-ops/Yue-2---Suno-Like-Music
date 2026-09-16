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
   `bootstrap.find_python` both test with `-c "import sys; ..."`.
5. **Model downloads are resumable.** Stream to `<name>.part`, `Range` header on
   retry, atomic `replace()` on completion. Never write straight to the final
   filename.
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
`SaveAudioAdvanced`, plus `SheetSage2AudioToABC` and `AudioEncoderLoader` for
cover mode.

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
SheetSage2AudioToABC`. Both feed the same `abc` input.
