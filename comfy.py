"""
comfy.py - talks to a running ComfyUI instance.

The generation graph is built from ComfyUI's own /object_info schema rather than
from a hard-coded API workflow. Node authors rename and add inputs between
releases; reading the schema means a rename shows up as "input not found"
instead of a silent wrong-value bug, and new required inputs get sensible
defaults automatically.

Graph (same shape as the yue2_full workflow):

    CheckpointLoaderSimple ─┬─ MODEL ──────────────┐
                            ├─ CLIP ─┬─ YuE2GenerateABC ─ abc ─┐
                            │        └─ YuE2GenerateMusic ◄────┘
                            └─ VAE ────────────────┐   │
                                                   │   ├─ CONDITIONING ─► KSampler
    EmptyYuE2LatentAudio ◄── seconds ──────────────┼───┘        │
                                                   │            ▼
    SaveAudioAdvanced ◄── VAEDecodeAudioTiled ◄────┴──────── LATENT
"""

from __future__ import annotations

import json
import random
import threading
import time
import uuid

import requests


class ComfyError(RuntimeError):
    pass


class ComfyClient:
    def __init__(self, url: str = "http://127.0.0.1:8188") -> None:
        self.url = url.rstrip("/")
        self.client_id = str(uuid.uuid4())
        self._schema: dict | None = None
        self._schema_at = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # schema
    # ------------------------------------------------------------------ #
    def schema(self, force: bool = False) -> dict:
        with self._lock:
            if force or self._schema is None or time.time() - self._schema_at > 120:
                r = requests.get(f"{self.url}/object_info", timeout=30)
                r.raise_for_status()
                self._schema = r.json()
                self._schema_at = time.time()
            return self._schema

    def node_inputs(self, class_type: str) -> dict:
        info = self.schema().get(class_type)
        if not info:
            raise ComfyError(
                f"This ComfyUI has no '{class_type}' node. YuE2 needs ComfyUI "
                f"v0.35.0 or newer — update ComfyUI and try again."
            )
        spec = info.get("input", {})
        merged = {}
        merged.update(spec.get("required", {}) or {})
        merged.update(spec.get("optional", {}) or {})
        return merged

    REQUIRED_NODES = ("CheckpointLoaderSimple", "KSampler", "YuE2GenerateMusic",
                      "EmptyYuE2LatentAudio", "SaveAudioAdvanced")

    def ensure_supported(self) -> None:
        """Fail with the real cause before anything else can mask it."""
        schema = self.schema()
        missing = [n for n in self.REQUIRED_NODES if n not in schema]
        if missing:
            raise ComfyError(
                "This ComfyUI cannot run YuE2 — it is missing "
                + ", ".join(missing)
                + ". YuE2 needs ComfyUI v0.35.0 or newer. Update ComfyUI, then "
                  "restart it from Settings."
            )

    @staticmethod
    def combo_options(spec) -> list[str]:
        """Read a dropdown's choices out of an /object_info input spec.

        ComfyUI writes combos two ways depending on how the node was declared:

            V1 (nodes.py)     [["a.safetensors", "b.safetensors"], {...}]
            V3 (comfy_api)    ["COMBO", {"options": ["a.safetensors", ...]}]

        Core loaders are still V1 while every YuE2 and audio node is V3, so both
        shapes turn up in the same graph. Reading only the first would silently
        report "no models installed" for half the nodes.
        """
        if not isinstance(spec, (list, tuple)) or not spec:
            return []
        if isinstance(spec[0], (list, tuple)):
            options = spec[0]
        else:
            opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            options = opts.get("options")
        if not isinstance(options, (list, tuple)):
            return []
        # A dynamic combo also keys off "options", but its entries are dicts;
        # those are not plain choices and belong to _dynamic_options().
        return [str(o) for o in options if isinstance(o, (str, int, float))]

    def checkpoints(self) -> list[str]:
        try:
            return self.combo_options(
                self.node_inputs("CheckpointLoaderSimple").get("ckpt_name"))
        except Exception:
            return []

    def pick_checkpoint(self, preferred: str = "") -> str:
        names = self.checkpoints()
        if preferred and preferred in names:
            return preferred
        yue = [n for n in names if "yue" in n.lower()]
        if yue:
            # int8 loads on far more cards; prefer it when both are present.
            int8 = [n for n in yue if "int8" in n.lower()]
            return (int8 or yue)[0]
        if names:
            return names[0]
        raise ComfyError(
            "No checkpoint found in ComfyUI/models/checkpoints. Run setup again "
            "to download the YuE2 model."
        )

    def audio_encoders(self) -> list[str]:
        try:
            return self.combo_options(
                self.node_inputs("AudioEncoderLoader").get("audio_encoder_name"))
        except Exception:
            return []

    def samplers(self) -> tuple[list[str], list[str]]:
        spec = self.node_inputs("KSampler")
        return (self.combo_options(spec.get("sampler_name")),
                self.combo_options(spec.get("scheduler")))

    # ------------------------------------------------------------------ #
    # SaveAudioAdvanced.format is a dynamic combo: choosing "mp3" or "opus"
    # makes ComfyUI expect a sibling "quality" input that does not exist for
    # "flac". Both the option list and that extra input live inside the spec.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _dynamic_options(spec) -> list[dict]:
        if not isinstance(spec, (list, tuple)) or len(spec) < 2:
            return []
        options = (spec[1] or {}).get("options")
        return [o for o in options if isinstance(o, dict)] if isinstance(options, list) else []

    def save_formats(self) -> list[str]:
        try:
            spec = self.node_inputs("SaveAudioAdvanced").get("format")
        except ComfyError:
            return ["flac"]
        keys = [str(o["key"]) for o in self._dynamic_options(spec) if o.get("key")]
        return keys or self.combo_options(spec) or ["flac"]

    def _format_extras(self, spec, chosen: str) -> dict:
        """Defaults for the inputs that the chosen format pulls in (quality)."""
        extras: dict = {}
        for option in self._dynamic_options(spec):
            if str(option.get("key")) != chosen:
                continue
            nested = option.get("inputs") or {}
            for group in ("required", "optional"):
                for name, definition in (nested.get(group) or {}).items():
                    opts = definition[1] if (isinstance(definition, (list, tuple))
                                             and len(definition) > 1
                                             and isinstance(definition[1], dict)) else {}
                    choices = self.combo_options(definition)
                    if "default" in opts:
                        extras[name] = opts["default"]
                    elif choices:
                        extras[name] = choices[0]
        return extras

    # ------------------------------------------------------------------ #
    # graph construction
    # ------------------------------------------------------------------ #
    @staticmethod
    def _match(available: dict, candidates: list[str]) -> str | None:
        for c in candidates:
            if c in available:
                return c
        low = {k.lower(): k for k in available}
        for c in candidates:
            if c.lower() in low:
                return low[c.lower()]
        return None

    def _node(self, class_type: str, wanted: dict) -> dict:
        """Build one API node: requested values by name + schema defaults."""
        spec = self.node_inputs(class_type)
        inputs: dict = {}

        for key, candidates in wanted.items():
            value = candidates["value"]
            name = self._match(spec, candidates["names"])
            if name is None:
                if candidates.get("required"):
                    raise ComfyError(
                        f"{class_type} has no input for '{key}'. This ComfyUI "
                        f"version is not compatible with the YuE Studio graph."
                    )
                continue
            inputs[name] = value

        # Fill remaining scalar inputs with their schema defaults so the prompt
        # validates even if a node gained a new required field.
        for name, definition in spec.items():
            if name in inputs or name == "control_after_generate":
                continue
            if not isinstance(definition, (list, tuple)) or not definition:
                continue
            kind, opts = definition[0], (definition[1] if len(definition) > 1 else {})
            if not isinstance(opts, dict):
                opts = {}
            choices = self.combo_options(definition)
            if choices:                          # enum, either schema version
                inputs[name] = opts.get("default", choices[0])
            elif kind in ("INT", "FLOAT", "STRING", "BOOLEAN"):
                if "default" in opts:
                    inputs[name] = opts["default"]
                elif kind == "STRING":
                    inputs[name] = ""
            # link types (MODEL/CLIP/VAE/LATENT/AUDIO/...) are left alone; if one
            # is required and unlinked ComfyUI reports it clearly.
        return {"class_type": class_type, "inputs": inputs}

    def build_prompt(self, p: dict) -> dict:
        """p: style, lyrics, duration, mode, steps, cfg, sampler, scheduler,
        seed, ckpt, use_abc, instrumental, tile_size, overlap, format,
        reference_audio (filename in ComfyUI/input, optional)."""
        self.ensure_supported()
        seed = int(p.get("seed") or random.randint(0, 2**31 - 1))
        style = (p.get("style") or "").strip()
        lyrics = "" if p.get("instrumental") else (p.get("lyrics") or "").strip()
        duration = int(p.get("duration") or 180)
        mode = p.get("mode") or "full"
        ckpt = self.pick_checkpoint(p.get("ckpt", ""))

        g: dict = {}

        g["15"] = self._node("CheckpointLoaderSimple", {
            "ckpt": {"names": ["ckpt_name"], "value": ckpt, "required": True},
        })

        abc_link = None

        if p.get("reference_audio"):
            # Cover mode: transcribe the reference melody to ABC.
            encoders = self.audio_encoders()
            enc = next((e for e in encoders if "sheetsage" in e.lower()),
                       encoders[0] if encoders else None)
            if not enc:
                raise ComfyError(
                    "Cover mode needs sheetsage2_bf16.safetensors in "
                    "ComfyUI/models/audio_encoders. Enable the cover model in "
                    "Settings and run setup again."
                )
            g["20"] = self._node("LoadAudio", {
                "audio": {"names": ["audio"], "value": p["reference_audio"],
                          "required": True},
            })
            g["18"] = self._node("AudioEncoderLoader", {
                "name": {"names": ["audio_encoder_name"], "value": enc,
                         "required": True},
            })
            g["19"] = self._node("SheetSage2AudioToABC", {
                "encoder": {"names": ["audio_encoder"], "value": ["18", 0],
                            "required": True},
                "audio": {"names": ["audio"], "value": ["20", 0], "required": True},
                "mode": {"names": ["mode"], "value": "melody"},
            })
            abc_link = ["19", 0]
        elif p.get("use_abc", True):
            # Text-to-music with symbolic planning.
            g["23"] = self._node("YuE2GenerateABC", {
                "clip": {"names": ["clip"], "value": ["15", 1], "required": True},
                "style": {"names": ["style", "style_prompt", "prompt", "genre"],
                          "value": style, "required": True},
                "lyrics": {"names": ["lyrics", "lyric", "text"], "value": lyrics,
                           "required": True},
                "seed": {"names": ["seed", "noise_seed"], "value": seed},
                "mode": {"names": ["mode", "abc_mode", "plan_mode"], "value": mode},
                "max_tokens": {"names": ["max_abc_tokens", "max_tokens",
                                         "max_new_tokens"],
                               "value": int(p.get("abc_tokens") or 8192)},
            })
            abc_link = ["23", 0]

        music_wanted = {
            "clip": {"names": ["clip"], "value": ["15", 1], "required": True},
            "style": {"names": ["style", "style_prompt", "prompt", "genre"],
                      "value": style, "required": True},
            "lyrics": {"names": ["lyrics", "lyric", "text"], "value": lyrics,
                       "required": True},
            "seed": {"names": ["seed", "noise_seed"], "value": seed},
            "mode": {"names": ["mode", "abc_mode", "plan_mode"], "value": mode},
            "duration": {"names": ["max_duration", "duration", "max_seconds",
                                   "seconds", "length"], "value": duration},
            "top_p": {"names": ["top_p"], "value": float(p.get("top_p", 0.95))},
            "top_k": {"names": ["top_k"], "value": int(p.get("top_k", 100))},
            "rep": {"names": ["repetition_penalty", "rep_penalty"],
                    "value": float(p.get("repetition_penalty", 1.2))},
        }
        if abc_link:
            music_wanted["abc"] = {"names": ["abc", "abc_notation"],
                                   "value": abc_link}
        g["22"] = self._node("YuE2GenerateMusic", music_wanted)

        g["5"] = self._node("EmptyYuE2LatentAudio", {
            "seconds": {"names": ["seconds", "duration", "length"],
                        "value": ["22", 1], "required": True},
            "batch": {"names": ["batch_size"], "value": 1},
        })

        g["8"] = self._node("KSampler", {
            "model": {"names": ["model"], "value": ["15", 0], "required": True},
            "positive": {"names": ["positive"], "value": ["22", 0], "required": True},
            "negative": {"names": ["negative"], "value": ["22", 0], "required": True},
            "latent": {"names": ["latent_image"], "value": ["5", 0], "required": True},
            "seed": {"names": ["seed", "noise_seed"], "value": seed},
            "steps": {"names": ["steps"], "value": int(p.get("steps") or 32)},
            "cfg": {"names": ["cfg"], "value": float(p.get("cfg") or 1.0)},
            "sampler": {"names": ["sampler_name"], "value": p.get("sampler") or "dpm_2"},
            "scheduler": {"names": ["scheduler"],
                          "value": p.get("scheduler") or "sgm_uniform"},
            "denoise": {"names": ["denoise"], "value": 1.0},
        })

        decode_class = ("VAEDecodeAudioTiled"
                        if p.get("tiled_decode", True)
                        and "VAEDecodeAudioTiled" in self.schema()
                        else "VAEDecodeAudio")
        decode_wanted = {
            "samples": {"names": ["samples"], "value": ["8", 0], "required": True},
            "vae": {"names": ["vae"], "value": ["15", 2], "required": True},
        }
        if decode_class == "VAEDecodeAudioTiled":
            decode_wanted["tile"] = {"names": ["tile_size", "tile"],
                                     "value": int(p.get("tile_size") or 1920)}
            decode_wanted["overlap"] = {"names": ["overlap"],
                                        "value": int(p.get("overlap") or 128)}
        g["17"] = self._node(decode_class, decode_wanted)

        save_spec = self.node_inputs("SaveAudioAdvanced")
        fmt = p.get("format") or "flac"
        if fmt not in self.save_formats():
            fmt = "flac"
        g["10"] = self._node("SaveAudioAdvanced", {
            "audio": {"names": ["audio"], "value": ["17", 0], "required": True},
            "prefix": {"names": ["filename_prefix"], "value": "audio/YuEStudio"},
            "format": {"names": ["format"], "value": fmt},
        })
        # mp3 and opus bring a sibling "quality" input along with them; flac
        # brings none. It lives inside the format option rather than at the top
        # level of the schema, so _node() cannot see it — set it here.
        extras = self._format_extras(save_spec.get("format"), fmt)
        if "quality" in extras and p.get("quality"):
            extras["quality"] = p["quality"]
        g["10"]["inputs"].update(extras)

        return {"prompt": g, "seed": seed, "ckpt": ckpt,
                "abc": bool(abc_link), "decode": decode_class}

    # ------------------------------------------------------------------ #
    # queue / results
    # ------------------------------------------------------------------ #
    def queue(self, prompt: dict) -> str:
        body = {"prompt": prompt, "client_id": self.client_id}
        r = requests.post(f"{self.url}/prompt", json=body, timeout=60)
        if r.status_code >= 400:
            try:
                err = r.json()
            except Exception:
                raise ComfyError(r.text[:500])
            raise ComfyError(_readable_error(err))
        return r.json()["prompt_id"]

    def interrupt(self) -> None:
        try:
            requests.post(f"{self.url}/interrupt", timeout=10)
        except Exception:
            pass

    def history(self, prompt_id: str) -> dict:
        r = requests.get(f"{self.url}/history/{prompt_id}", timeout=20)
        r.raise_for_status()
        return r.json().get(prompt_id) or {}

    def queue_state(self) -> dict:
        try:
            return requests.get(f"{self.url}/queue", timeout=10).json()
        except Exception:
            return {}

    def outputs(self, prompt_id: str) -> list[dict]:
        hist = self.history(prompt_id)
        found = []
        for node_out in (hist.get("outputs") or {}).values():
            for key in ("audio", "audios", "result"):
                for item in node_out.get(key, []) or []:
                    if isinstance(item, dict) and item.get("filename"):
                        found.append(item)
        return found

    def failed(self, prompt_id: str) -> str | None:
        hist = self.history(prompt_id)
        status = hist.get("status") or {}
        if status.get("status_str") == "error":
            for kind, data in status.get("messages", []):
                if kind == "execution_error":
                    return (f"{data.get('node_type')}: "
                            f"{data.get('exception_message')}")
            return "Generation failed inside ComfyUI."
        return None

    def view(self, item: dict):
        params = {"filename": item.get("filename", ""),
                  "subfolder": item.get("subfolder", ""),
                  "type": item.get("type", "output")}
        return requests.get(f"{self.url}/view", params=params, stream=True,
                            timeout=120)

    def upload_audio(self, file_storage) -> str:
        files = {"image": (file_storage.filename, file_storage.stream,
                           file_storage.mimetype or "audio/wav")}
        r = requests.post(f"{self.url}/upload/image", files=files,
                          data={"type": "input", "overwrite": "true"}, timeout=180)
        r.raise_for_status()
        data = r.json()
        name = data.get("name") or file_storage.filename
        sub = data.get("subfolder") or ""
        return f"{sub}/{name}" if sub else name


def _readable_error(err: dict) -> str:
    """Turn ComfyUI's validation payload into one sentence a person can act on."""
    node_errors = err.get("node_errors") or {}
    for node_id, info in node_errors.items():
        for e in info.get("errors", []):
            return (f"{info.get('class_type', 'node ' + str(node_id))}: "
                    f"{e.get('message')} {e.get('details', '')}".strip())
    top = err.get("error") or {}
    if top:
        return f"{top.get('message', 'Rejected by ComfyUI')} " \
               f"{top.get('details', '')}".strip()
    return json.dumps(err)[:400]
