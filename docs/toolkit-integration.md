# What to take from the Music Production Toolkit

The **ComfyUI Music Production Toolkit** v3.1.3 (MIT, Johannes Plenio,
<https://github.com/jplenio/ComfyUI-MiniMax-Music-Production-Toolkit>) is a
ComfyUI custom-node pack. It has 984 files and about 450 Python modules
covering YuE2 and MiniMax Music 3 generation, an LLM prompt writer, a "Cover
Studio", restoration and mastering, tagging and FLUX.2 cover art. This note
records what fits YuE Studio, what does not, and in what order to take it.
The analysis was done against the zip committed to `main`.

YuE Studio cannot adopt the pack as-is. CLAUDE.md rule 6 forbids installing
anything into an existing ComfyUI, and the pack's value sits in its node code.
So the rule throughout is to **re-implement the idea in YuE Studio, or port
the pure-Python part**, and never to depend on the nodes.

---

## 1. Lyric writing: done

`lyricist.py`, `/api/llm/*`, `/api/lyrics/write`, the pen button on the Lyrics
card, and **Settings → Lyric writer**.

| | Toolkit | YuE Studio |
|---|---|---|
| Where the model runs | Inside ComfyUI (llama-cpp GGUF node), a local server, or cloud | A local server (Ollama, LM Studio, llama.cpp) or cloud (Claude via the `anthropic` SDK, OpenAI, OpenRouter, any OpenAI-compatible server). No in-ComfyUI mode, because of rule 6. |
| What it writes | `[Style]` as 250–450 words of arrangement prose with timed sections, plus `[Lyrics]`, `[Title]` and `[Image_Prompt]` | A comma-separated style line (what the Styles box and its chips expect), plus lyrics and a title |
| Templates | 12 system-prompt variants and about 280 genre briefs | One system prompt; the brief is free text |
| Long work | A blocking node | A Task: streams, Stop works between chunks, one write at a time (rule 9) |
| Key handling | Process memory, or an opt-in file keyed by API base | The config file, masked like the HF token (rule 7), bound to its address, sent only over https or to this computer |

Kept from the toolkit's prompts (credited in `THIRD_PARTY.md`): style and
lyrics share one section map, repeats are written out, instrumental sections
are tag-only, and nothing but singable words goes under a tag.

**Possible follow-ups:**
- **Genre briefs as starters.** The toolkit's `prompts/user/<genre>/*.txt` are
  short structured briefs (genre, tempo, meter, voice, theme, length). A
  "Start from…" list in the writer could fill `#lyrBrief` from them. It is
  data, not code, and needs the MIT notice.
- **An "arranged style" option.** The toolkit puts a numbered, timed
  arrangement into Style and claims it helps the ABC planner. This has not
  been measured here. Try it as an A/B with a fixed seed before making it a
  default; it would also need the Styles box to stop treating its text as
  chips.

---

## 2. Cover mode compared with Cover Studio

**Today in YuE Studio.** One graph: `LoadAudio → AudioEncoderLoader →
SheetSage2AudioToABC → YuE2GenerateMusic.abc`, with the music `mode` matched to
the transcription mode (`comfy.py` `build_prompt`). A `PreviewAny` returns the
score, which is saved as `track.abc` and can be edited and re-rendered from the
Score panel.

**The toolkit's Cover Studio** is a chain of nodes:

| Stage | What it does | Needs |
|---|---|---|
| Transcription | The same SheetSage2 chain as ours | ComfyUI |
| Instrumental rewrite (`cover_score.py`) | Vocal notes become rests; the lead line moves into the matching `V: Ins` bars, with overlaps replaced and counted | pure Python |
| Deterministic transforms (`cover_transform.py`) | Transpose (re-spelled against the new key, chords and `K:` moved too), tempo (`Q:` only), strip chords. Each result is re-validated and skipped with a warning if it fails. | pure Python |
| "Interpretation Freedom" 0–100 (`cover_profiles.py`) | Interpolates between seven profiles of preserve/allow weights. It is an LLM instruction, **not a YuE2 input**. | pure Python |
| Plan and LLM rewrite (`cover_planner.py`, `cover_transform.py`) | The LLM returns `{abc, changes, warnings}` JSON, which is validated and checked against pinned elements. **If it fails, the deterministic result is used.** At most 2–3 calls. | LLM |
| Lyrics contract (`cover_lyrics_contract.py`) | Instrumental: tags only. New: must not copy the transcript. Original: the word order must match the transcript. | pure Python |
| Original lyrics | faster-whisper transcribes the source, and the words are placed into the score's sections | faster-whisper (~31 packages) plus large-v3 weights |
| Instrumental vocal check (`instrumental_check.py`) | Whisper transcribes each render, re-renders with seed+1 up to 10 times, and keeps the take with the fewest words | faster-whisper plus several renders |

**Gaps this exposes in YuE Studio:**
1. **Python never sees the score before rendering.** SheetSage's ABC flows node
   to node, so nothing can transpose it or turn it into an instrumental. The fix
   is a **two-pass cover**: queue a transcription-only prompt, read the ABC back
   through `PreviewAny`, transform it in Python, then queue the music graph with
   the ABC as a literal. Keep today's single graph when `PreviewAny` is absent.
2. **An Instrumental cover only blanks the lyrics.** The vocal line is still in
   the `V: Vocal` part of the score. The toolkit's instrumental rewrite is the
   real fix, and it needs item 1.
3. **Reference plus a pasted score.** When both are present, SheetSage is
   skipped and `mode` comes from the plan control, but the song is still tagged
   as a cover. Either warn on the page or make the choice explicit.

**Recommendations, in order:**

| # | Take | Size | Fit |
|---|---|---|---|
| 1 | Two-pass cover (transcribe, read back, render) | new code in `comfy.py` and `server.py` | Rules 2, 9, 27: two prompts, and the stall clock must allow for both |
| 2 | Instrumental rewrite (`cover_score.py`) | ~430 lines, stdlib | Port. Makes "Instrumental" mean something for covers |
| 3 | Lyrics-fit warning (`lyrics_fit.py`, `cover_lyrics_contract.py`) | ~260 lines | Port. Cheap "these lines will not fit these notes" hints |
| 4 | LLM rewrite with deterministic fallback | ~750 lines plus prompts | Later, through `lyricist.py`'s client. Label a freedom slider as LLM guidance: "no decorative sliders" applies. |
| 5 | Whisper original lyrics and vocal check | 1,150+ lines plus heavy dependencies | Opt-in extra only: installed into YuE Studio's venv, never ComfyUI's (rule 6), weights downloaded resumably (rule 5) |

---

## 3. ABC melody input: mostly done

**Done:** `yue2_abc.py` (YuE's own helper, vendored unchanged) and
`scores.py`; `/api/abc/inspect`, `/api/abc/strip-chords` (all or vocal-only)
and `/api/abc/compare`; a live check in the Score box, which advises and never
blocks; *Check melody kept*; *Reharmonize…*; and *Fit to score* in the lyric
writer. **Still open:** transpose and tempo tools, uploading an `.abc` file,
and MIDI → ABC.


The Score box already takes a literal score (`comfy.py`: a score in the box
wins and replaces the generator). What is missing is **checking** and
**editing tools**.

**The dialect.** The toolkit's `third_party/yue2_abc.py` is an unchanged copy
of YuE's own `abc_tools.py` (**Apache-2.0**, not MIT; commit `ef1936f`). It
describes a narrow native form:
- Headers `X:1`, `T:`, `M:`, `L:1/2^n`, `Q:1/4=<int>`, then the fixed
  `V: Vocal` and `V: Ins` lines, then `K:`.
- Bodies in groups of 1–4 bars, each a `V: Vocal` line plus a `V: Ins` line with
  equal bar counts.
- Fixed durations; no tuplets, repeats, grace notes or `w:` lines.

The toolkit's `abc_validate.py` (323 lines) wraps it and reports bpm, bars,
duration, notes and chords.

**Proposal:**
1. **`POST /api/abc/check`** (vendor `yue2_abc.py` with its Apache licence, and
   port the validator). Show the result in `#abcNote`, e.g. "88 BPM · 24 bars ·
   1:05 · chords", or the first error with its bar. **Warn, do not block**: the
   test suite's own short scores (`X:1\nK:G\nGABc|`) fail the strict check, and
   it has not been verified that the native node rejects looser ABC.
2. **Score tools**: Transpose ±, Tempo %, Melody only (strip chords), and Move
   vocal line to instrument, via `POST /api/abc/transform`. They rewrite the
   text in the box, so a score in the box still takes over completely.
3. **Upload `.abc`** next to Copy/Clear. If the score has chords and the plan
   control is Melody, suggest Full, since the music mode should match the score.
4. **MIDI → ABC is not in the toolkit.** It would be new work: quantise to the
   native grid and split into Vocal and Ins. Feasible in pure Python, but not
   scoped.

---

## 4. Clean-up and mastering

**Two findings change the plan:**
- **FlashSR is effectively off in the toolkit's own workflows.** Both example
  workflows set `FlashSRHybridCrossover` to `"Original SRC only"`, which passes
  through only the resampled original. FlashSR is also 3.3 GB of weights, a
  torch custom node, and vendored research code with no clear licence.
- **The toolkit's mastering already calls ffmpeg**, using `loudnorm` as its
  BS.1770 meter. YuE Studio already finds and can install ffmpeg
  (`convert_audio()`), so ffmpeg filters are the natural engine here.

| Feature | Toolkit | Recommendation |
|---|---|---|
| Loudness normalise / sample rate (`audio_release_prep.py`) | Measures, then applies a static gain to −14/−12/−10 LUFS capped by true peak, and Kaiser SRC | **ffmpeg** `loudnorm` measure pass, then `volume`, then `aresample`. Quickest win. |
| Compressor (`audio_compressor.py`) | Soft-knee, stereo-linked, HPF sidechain | **ffmpeg** `acompressor` with the toolkit's 12 presets as data |
| Limiter / LUFS master (`audio_limiter.py`, `audio_mastering.py`) | 4× oversampled lookahead; measure/adjust/verify up to 3 passes | **ffmpeg** `acompressor → volume → alimiter`, then re-measure, reusing the toolkit's verify loop |
| Parametric EQ + presets (`eq_config.py`, `eq_presets.json`) | 8 RBJ biquads; 24 manual presets, including "YuE2 – Smooth highs" | **ffmpeg** `equalizer`/shelf/pass filters, with presets converted from the JSON |
| Declip (`audio_declip.py`) | Hermite reconstruction of flat crests | **ffmpeg** `adeclip`, off by default; YuE2 renders are rarely clipped |
| HF / cymbal shimmer repair (`audio_hf_repair.py`) | HF band transient/sustain split, sustain reduced 1–3 dB | **Port** (~120 lines, numpy + scipy). Targets the "fizzy cymbals" typical of AI renders. |
| Auto-EQ (`audio_auto_eq.py`) | Welch spectrum against a reference or a tilt, fitted peak bands | **Port** later (~160 lines, scipy); apply its bands through ffmpeg |
| Artifact reduction (`audio_artifact_reduction.py`) | STFT outlier bins in 3–18 kHz, up to 3 dB | **Port** later, opt-in with A/B; the toolkit itself calls it experimental |
| FlashSR upscaling | One-step diffusion super-resolution | **Skip** |

**Shape in YuE Studio.** Add a **Master** action on a library track as
`POST /api/track/<id>/master`, which `manager.spawn`s a task (rule 9). The task:
- runs ffmpeg with UTF-8 / `errors="replace"` reading (rule 22), and kills it
  when `task.cancel` is set;
- writes a **new** file into `data/tracks/` through a temp file and `replace()`,
  never touching the original (rule 10);
- adds a library entry with `parent`, the settings used, and before/after
  LUFS and true peak.

Phase 1 needs no new Python dependencies. Only the ported DSP (shimmer,
auto-EQ, artifacts) would add numpy and scipy.

---

## 5. Tagging

The toolkit uses mutagen (Vorbis comments plus a FLAC picture, or ID3v2.3 plus
APIC). ffmpeg does the same without new dependencies:
`-metadata title=… artist=… album=… genre=…`, and for artwork
`-i cover.jpg -map 0:a -map 1 -c:a copy -c:v mjpeg -disposition:v attached_pic`.
Apply it on **download/export** from the library entry, and stream-copy the
audio. About 60 lines.

## 6. FLUX.2 cover art

This needs no custom nodes: the toolkit's artwork path is all **native**
ComfyUI:
`UNETLoader`, `CLIPLoader(type="flux2")`, `VAELoader`, `CLIPTextEncode`,
`ConditioningZeroOut`, `CFGGuider(cfg=1)`, `RandomNoise`,
`KSamplerSelect("euler")`, `Flux2Scheduler(steps=4)`, `EmptyFlux2LatentImage`,
`SamplerCustomAdvanced`, `VAEDecode` and `SaveImage`. So it works on an existing
ComfyUI too.
- **Graph:** `build_artwork_prompt()` in `comfy.py`, built from `/object_info`
  (rule 2), with combos read through `combo_options()` (rule 3).
- **Optional nodes:** these nodes grey out Artwork when missing; they never make
  the engine "not ready" (rules 17, 19, 20).
- **Weights:** FLUX.2 Klein 4B, about 16 GB (`flux-2-klein-4b` 7.75 GB,
  `qwen_3_4b` 8.04 GB, `flux2-vae` 0.34 GB), or about 8 GB with the fp8/fp4
  variants. They are optional downloads in `bootstrap.MODELS`, checked by
  `safetensors_size()` (rule 18) and never counted toward `ready`.
- **Output:** copy the image into `data/`, never leaving it in ComfyUI's output
  folder.
- **Prompt:** the lyric writer can supply one; the toolkit's prompts already
  produce an `[Image_Prompt]` section.
- **Not verified:** the minimum ComfyUI version for `Flux2Scheduler` and
  `EmptyFlux2LatentImage`.

---

## 7. Suggested order

| Step | What | New dependencies | Effort |
|---|---|---|---|
| 1 | ~~Lyric writer~~ | `anthropic` | **done** |
| 2 | ~~ABC check, strip chords, compare~~ — transpose and tempo still open | none: vendored `yue2_abc.py` (Apache-2.0) | **mostly done** |
| 3 | Loudness normalise and tag on export | none (ffmpeg) | small |
| 4 | Master presets (compressor, EQ, limiter via ffmpeg) | none | medium |
| 5 | ~~Two-pass cover~~ (*Transcribe the recording*) — instrumental rewrite still open | none | **half done** |
| 6 | FLUX.2 cover art (optional ~8–16 GB download) | none (native nodes) | medium |
| 7 | Shimmer repair, then auto-EQ and artifact reduction | numpy, scipy | medium |
| 8 | ~~LLM reharmonization, checked note for note~~ — freedom-slider cover rewrite still open | none beyond step 1 | **half done** |
| 9 | Whisper original lyrics and vocal check | faster-whisper (opt-in) | large |
| — | FlashSR | — | skip |

## 8. Facts worth keeping

- ComfyUI's YuE2 shares a **24,576-token context** between text, ABC and music,
  and music runs at **25 frames per second**. That puts the hard ceiling near
  16 minutes *minus* whatever the lyrics and score take. This is why
  `recommended_duration` is not the 15-minute maximum (rule 21), and it is worth
  quoting in `explain_failure()` for long requests (rule 26).
- The toolkit's ABC-generator settings are temperature 0.7, top-p 0.9, top-k
  30, repetition penalty 1.005 and window 100, which it describes as the node's
  own defaults. YuE Studio leaves them at the defaults, so the two agree.
- The toolkit notes that **an empty ABC input silently selects `off`** in the
  native node. YuE Studio's *Off* removes the generator on purpose, so no
  change is needed.
- Licences: the toolkit is MIT (notice in `THIRD_PARTY.md`); `yue2_abc.py` and
  the ABC reference docs are Apache-2.0 from YuE; FlashSR has no clear licence.

## 9. How YuE Studio lines up with the YuE2 skill

The skill that defines these workflows is
[`skills/yue2-music`](https://github.com/multimodal-art-projection/YuE/tree/main/skills/yue2-music)
in the YuE repository.

| Skill workflow | In YuE Studio |
|---|---|
| `cot="full"` / `"melody"` / `"off"` | *Melody plan*: Full, Melody, Off |
| Plan, then render (`run_yue2.py plan`) | *Plan first* → *Use* → Create. It uses the native `YuE2GenerateABC` with `PreviewAny`, no custom pack. |
| Cover a recording: SheetSage2 → inspect and correct → strip chords → `melody` | *Transcribe the recording* → edit → *Strip chords* → Create (Melody). A one-pass cover is still available. |
| Cover an ABC melody | Paste it into the Score box → *Strip chords* → Melody |
| Change harmony, style or lyrics, then regenerate | *Render again from its plan*, change one thing, Create |
| `abc_tools.py inspect` / `strip-chords --keep-voice` / `compare --voices --allow-tempo-change` | The live check, *Strip chords* / *Vocal line only*, *Check melody kept*, and Compare (tempo change allowed) |
| Agentic reharmonization under a contract, reviewed | *Reharmonize…* with the sung melody or both melodies fixed. The result is proved with `compare`; a failure is sent back once, then refused. |
| Singable lyric adaptation | *Fit to score*: sung-note counts per section go to the writer, and the answer gets a per-section syllable check |
| Reproducible listening comparison (`listen.py`) | *Compare with…*: two players, full records and marked differences |

**Not covered, and why:**
- **Decoder switching** (`YuE2-Vae` vs `-legacy`) and **cached-latent
  decoding**: ComfyUI's graph does not expose latents between runs.
- **Phoneme sidecars**: YuE2 has no phoneme input. The skill says to keep a
  sidecar outside the model, which remains possible by hand.
- **ASR / PER and SongBench scoring**: these need separate evaluation
  packages.
- **The reviewer-agent pass**: the note-for-note check stands in for its
  hard constraint, but harmonic quality is still judged by listening.
- **`YuE2RenderPlan`**: this node comes from a custom-node pack, not from
  ComfyUI. Rule 6 keeps YuE Studio off such packs, and the native nodes
  already give the same split between plan and render.
