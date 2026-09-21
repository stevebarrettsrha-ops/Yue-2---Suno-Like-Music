# YuE Studio

A local, Suno-style front end for the YuE2 music model. Write a style
description and lyrics, press Create, get a full song with vocals — all on your
own machine, no account, no credits.

It drives ComfyUI in the background. On first launch it installs ComfyUI,
downloads the model files and starts the engine for you.

---

## Running it

**Windows** — double-click `run.bat`
**macOS / Linux** — `./run.sh`

Either way the browser opens at <http://127.0.0.1:7788>.

The setup panel appears on the first run. Pick one of three routes:

| Route | What happens |
|---|---|
| Use an existing ComfyUI | Found installs are listed. Only missing models are downloaded. |
| Install a fresh ComfyUI | Clones ComfyUI into `./ComfyUI`, builds its own venv, installs PyTorch, downloads models. |
| Connect to one I start myself | Set the address and models folder in Settings. Setup only fetches missing models. |

Setup takes a while the first time — mostly PyTorch (about 3 GB) and the model
files. Progress and a live log are shown. It resumes if interrupted: partial
downloads are kept in `.part` files and continued.

### The Engine page

Everything YuE Studio needs is listed there with a state next to it — Python, Git,
ComfyUI, PyTorch, ComfyUI's packages, ffmpeg, the model files, the engine itself.
Anything marked missing has an Install button beside it, and **Install everything
missing** works through them in order. Output streams into the Activity log as
it runs. Nothing needs a terminal. Installing Python is the one step that needs
YuE Studio restarted afterwards, because a program reads PATH once when it
starts.

If PyTorch will not install or your GPU is being ignored, pick a different build
(CUDA 12.8 / 12.4 / 12.1, ROCm, CPU) on the same page and reinstall.

### The engine console, and Restart

The **Engine console** on that page is ComfyUI's own output, live, with YuE
Studio's own actions — taking a port back, stopping a process, adopting an
engine that was already up — written into the same stream, in the order they
happened. One line above it says where things stand: starting, running, online
but started elsewhere, or one of the two states that used to be invisible:

- *the model files are on disk but this ComfyUI started before they landed* —
  ComfyUI reads its model folders once, at startup, so a checkpoint downloaded
  afterwards is there and unseen. It reads as a failed download, and sends
  people to re-fetch four gigabytes they already have.
- *a different ComfyUI is answering at this address* — which looks exactly the
  same from the song that failed.

**Restart the engine** cures both, and it works on an engine YuE Studio did not
start: ComfyUI-Manager's own reboot first, and failing that the process holding
the port is identified and stopped, then YuE Studio's own engine takes its
place. Anything on the port that does not look like ComfyUI is named back to
you and left alone — a wrong address in Settings is not a licence to close
whatever is at it. When it cannot be done, the reason given is the real one:
access denied, or something supervising the engine and putting it straight back
under a new process id.

You rarely need the button. A launch already ends with a working engine:
nothing running gets one started, a healthy one already up is adopted and said
so, and one that started before the model files landed is replaced. A ComfyUI
you run yourself (the external-mode setup) is only ever told what is wrong.

### The Models page

All HuggingFace work happens here:

- **Recommended files** — the three YuE2 files, one button each, with size and
  what they are for.
- **Browse a repo** — type any repo (`Comfy-Org/YuE2` by default), see its files
  with sizes, and download the ones you want. Each file's target folder is worked
  out from its path and can be overridden.
- **Token and mirror** — paste an access token for gated repos, or point at a
  mirror if huggingface.co is slow. The token is stored locally and only ever
  shown as its last four characters.
- **Downloads** — live progress, speed, time left, and a Stop button. Stopping
  keeps the partial file, so starting again resumes.
- **On this machine** — every model file found, with its size, and a Delete
  button. Unfinished downloads are marked.

### Requirements

- Python 3.10 or newer. On Windows `run.bat` offers to install it for you
  through WinGet if it is missing — the Microsoft Store's Python install
  manager, then Python 3.13 through that. (The standalone python.org installer
  stops being released with Python 3.16, so the manager is the route that keeps
  working.) On macOS and Linux the Engine page uses `brew` / `apt-get`.
- Git (only for the managed-install route)
- ComfyUI **v0.35.0 or newer** — YuE2 nodes are built in from that version.
  v0.35.1+ is recommended on AMD cards.
- An NVIDIA GPU with 8 GB+ VRAM for reasonable speed. CPU works but is slow.

### Keeping models somewhere else

Set **Models folder** in Settings to any folder you like — another drive, or a
models library you already share with other tools. Downloads go there, and a
ComfyUI that YuE Studio starts is handed that folder through ComfyUI's own
`--extra-model-paths-config`, so it loads from there too. The file is written to
`data/extra_model_paths.yaml` on every start; edit the setting, not the file.

The folder wants ComfyUI's usual layout — `checkpoints/`, `audio_encoders/` and
so on — which is what the Models page downloads into anyway.

One case it cannot cover: a ComfyUI you start yourself. Nothing here passes
arguments to a process it did not launch, so set the same folder in that
ComfyUI's own `extra_model_paths.yaml`. The Engine page says when the folder and
the engine disagree.

### When something else holds the port

Another ComfyUI on the configured address — a Desktop install, a stray process
from an earlier session — used to make every song fail with "no checkpoints"
while the files sat on disk. Now, on every launch and on **Start the engine**,
YuE Studio checks that the engine answering is the one it manages; when it
provably is not, it moves itself to the next free port, starts the right
ComfyUI there, and keeps the new address. An engine verified as its own is
never abandoned, and an address you set to a remote machine or an external
setup is never second-guessed.

Moving aside is the gentle cure, and it is the one used whenever the port is
held by an install that is somebody else's: their ComfyUI keeps running. The
other cure is **Restart the engine**, which closes what is on the port and puts
YuE Studio's own engine there. That is the right one when the engine at the
address *is* the managed install and has simply gone stale — moving aside there
would leave the old one holding the graphics card and fix nothing.

### Model files

Downloaded from `Comfy-Org/YuE2` on HuggingFace into your ComfyUI models folder:

```
models/checkpoints/yue2_3b_int8_convrot.safetensors     3.96 GB   required
models/audio_encoders/sheetsage2_bf16.safetensors       1.39 GB   cover mode
models/checkpoints/yue2_3b_bf16.safetensors             7.80 GB   optional, higher quality
```

---

## Using it

**Styles** carries all the musical instruction: genre, vocal type, language,
tempo, instruments, mood. Example:

> English, roots reggae, warm male vocal, 74 BPM, offbeat guitar skank, deep
> bass, organ bubble, sunlit and steady

**Lyrics** holds only words that get sung, with section tags:

```
[Verse]
Morning light across the window
City waking down below

[Chorus]
Run with me into the sunlight
```

Leave lyrics empty, or switch Instrumental on, for a vocal-free track.

**More options**

- *Melody plan* — `full` writes melody and chords before rendering audio
  (best general setting), `melody` plans the tune only and lets the backing
  roam, `off` goes straight from text to audio.
- *Max length* — a ceiling, not a target. Songs often end earlier.
- *Steps / sampler / scheduler* — the diffusion pass. 32 steps with `dpm_2` and
  `sgm_uniform` matches the reference workflow.
- *Length* — real song lengths, 2:00 upwards, as far as the engine allows
  (15:00 with the current YuE2 node). This is a ceiling, not a target: YuE2
  stops when the song is over, so a longer setting never pads a short song, it
  only stops a long one being cut off mid-verse. **Auto** uses the engine's
  tested default (currently 6:00), avoiding the large up-front allocation of
  its absolute 15-minute limit. Pick a longer ceiling explicitly when needed.
- *Seed* — leave blank for random, or fix it to re-roll a song with one change.
- *Tiled decode* — leave on unless you have plenty of VRAM; off is faster.
### Words it cannot say

YuE2 is handed the lyrics as plain text and sings the letters it reads — there
is no pronunciation step, no phoneme conversion and no language setting
anywhere in the graph. A word in a script it was never trained on comes out as
a guess.

The fix is to write the word the way it sounds, in Latin letters, and to name
the language in the style:

```
Style:   Hebrew, roots reggae, warm male vocal, 74 BPM
Lyrics:  [verse]
         shalom, ma nishma            (שלום, מה נשמע)
         le-cha-yim                   hyphens split the syllables
```

Anything still mangled is usually a spelling problem rather than a model
problem: spell it as an English speaker would read it aloud, and break long
words with hyphens to control where the syllables land. The lyrics box says so
by itself when it sees a script the model is unlikely to sing.

- *Save as* — one file per song, in the format you pick. `flac` keeps
  everything, `mp3` is the one to send someone, and `wav` plays anywhere.
  ComfyUI writes flac, mp3 and opus itself; it has no wav encoder, so a wav is
  rendered losslessly and converted by ffmpeg — which is why `wav` only appears
  when ffmpeg is installed, and the Engine page installs it in one press. On
  Windows that press downloads ffmpeg straight into `data\tools` beside the
  app — same drive, no winget, no PATH edit, usable the moment it lands. (It
  used to go through winget, which put it on the system drive and is refused
  entirely on machines where antivirus guards AppData.) Your
  choice is remembered for the next song.

**Score** is the melody and chords the song gets built on, in ABC notation. Leave the
box empty and YuE2 writes it. Press the score button on any finished song and
its notation lands in the box, where you can change the key, tempo, a phrase or
a chord and press Create again — that edited score is then used as-is and the
planning stage is skipped, so the same tune comes back with your change in it.
Clear the box to hand the job back to the model.

**Cover from audio** uploads a reference song, transcribes it to ABC notation
with SheetSage2, then re-sings it with your style and lyrics. The audio itself
is never fed to the model — only the transcribed score. Once a reference is
attached, a **Cover takes** switch appears in More Options: *Melody only*
transcribes just the tune, *Melody + chords* also carries the harmony across,
which copies more of the original's sound. Covers work best when new lines
match the original phrasing and syllable count.

There is no rhythm-and-beats-only mode — SheetSage transcribes melody, or
melody with chords, and that is all the engine offers. The nearest workflow:
run a cover once, press the score button on the result, and edit the ABC —
the note *timing* in that score is the reference's rhythm, so changing the
pitches while keeping the durations keeps its groove. For a beat *feel*
without the tune, describe it in Styles instead ("120 bpm", "four-on-the-floor",
"halftime", "shuffle") — tempo and groove words are real levers on this model.

`Ctrl` + `Enter` creates from anywhere in the page.

Finished songs land in the workspace on the right and are copied into
`data/tracks/`, so they survive ComfyUI clearing its output folder.

---

## Notes on your uploads

- `yue2_full__1_.json` is kept at `assets/yue2_full_reference.json`. The app
  builds the same graph, so you can open that file in ComfyUI any time to check
  the app's output against the original workflow.
- The ComfyUI Desktop installer you uploaded works as the "existing install"
  route — install it, start it once, then point YuE Studio at it in Settings
  (Desktop usually serves on `http://127.0.0.1:8000`).
- `github.com/multimodal-art-projection/YuE` is the model authors' own
  repository and a separate inference stack. Your workflow uses ComfyUI's
  **native** YuE2 nodes instead, which is why nothing is cloned from that repo.

---

## Troubleshooting

**"This ComfyUI has no 'YuE2GenerateMusic' node"**
ComfyUI is older than v0.35.0. Update it (`git pull` in the ComfyUI folder,
then reinstall requirements), or use the managed install route.

**Setup stops on PyTorch**
The wheel index is guessed from your hardware. Set it by hand in Settings —
`https://download.pytorch.org/whl/cu128` for recent NVIDIA drivers,
`https://download.pytorch.org/whl/cu121` for older ones,
`https://download.pytorch.org/whl/cpu` for no GPU.

**Downloads keep failing**
They resume, so pressing Start setup again picks up where it stopped. You can
also download the files by hand from HuggingFace and drop them in the folders
listed above; setup then skips them.

**Out of memory during generation**
Lower Max length, keep Tiled decode on, and use the int8 checkpoint rather than
bf16.

**Generation never finishes**
The first run of any model loads slowly. Check the Engine console on the Engine
page — that is ComfyUI's own output, live.

**Every song fails with "no checkpoints" and the files are on disk**
ComfyUI scans its model folders once, at startup, so anything downloaded after
it started is invisible to it. The Engine console says so in as many words when
it happens; press **Restart the engine**. Re-downloading will not help.

**Cover songs come out as gibberish**
A cover is only as good as its transcription, so look at the score first:
press the score button on the failed song. If the ABC is already a mess —
noise notes, no recognisable tune — the problem is upstream of the singing,
and the fix is a cleaner reference: a section where the melody is exposed
(a verse or chorus without heavy layering), not an intro, a drop or a wall of
sound. If the score looks right but the singing is garbled, work the model
side: match your lyric lines to the original's phrasing and syllable count,
lower Lyric focus a step, nudge Repetition up slightly, and set a fixed seed
so each retry changes one thing instead of everything. *Melody only* under
Cover takes is the more forgiving setting; *Melody + chords* copies more but
gives a rough transcription more ways to go wrong. This tames gibberish —
it does not abolish it; transcription plus re-synthesis is a lossy trip.

---

## Layout

```
run.sh         Launcher — macOS / Linux
run.bat        Launcher — Windows
server.py      Flask API — setup, jobs, library, audio streaming
bootstrap.py   Python/ComfyUI discovery, installs, model downloads, process control
manager.py     Background tasks — dependency installs and HuggingFace downloads
comfy.py       Builds the YuE2 graph from ComfyUI's /object_info schema, queues it
web/index.html The interface — one file, no build step
assets/        yue2_full_reference.json, the workflow the graph mirrors
data/          config.json, library.json, tracks/ (created on first run)
.venv/         YuE Studio's own packages, built by the launcher on first run
```

Port: set `YUE_STUDIO_PORT` to move off 7788. Set `YUE_STUDIO_NO_BROWSER=1` to
stop it opening a browser tab. Set `YUE_STUDIO_DATA` to keep config, library
and finished tracks somewhere other than `data/`.

## Tests

```bash
python tests/run.py
```

About two minutes, and needs nothing beyond what YuE Studio already installs —
the browser tests want Playwright and step aside without it. See
[tests/README.md](tests/README.md).
