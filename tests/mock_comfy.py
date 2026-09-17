"""A stand-in ComfyUI: /object_info shaped exactly like v0.35's, plus a queue."""
import json, threading, time, io, wave, struct, hashlib, base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

V1 = lambda opts: [opts, {}]                       # nodes.py combo
V3 = lambda opts: ["COMBO", {"options": opts}]     # comfy_api combo

SAMPLERS = ["euler", "dpm_2", "dpmpp_2m", "ddim"]
SCHEDULERS = ["normal", "karras", "sgm_uniform", "simple"]

OBJECT_INFO = {
    "CheckpointLoaderSimple": {"input": {"required": {
        "ckpt_name": V1(["yue2_3b_int8_convrot.safetensors", "yue2_3b_bf16.safetensors"])}}},
    "KSampler": {"input": {"required": {
        "model": ["MODEL"],
        "seed": ["INT", {"default": 0, "min": 0, "max": 2**64 - 1}],
        "control_after_generate": V1(["fixed", "increment"]),
        "steps": ["INT", {"default": 20, "min": 1, "max": 10000}],
        "cfg": ["FLOAT", {"default": 8.0}],
        "sampler_name": V1(SAMPLERS),
        "scheduler": V1(SCHEDULERS),
        "positive": ["CONDITIONING"], "negative": ["CONDITIONING"],
        "latent_image": ["LATENT"],
        "denoise": ["FLOAT", {"default": 1.0}]}}},
    "YuE2GenerateABC": {"input": {"required": {
        "clip": ["CLIP"],
        "style": ["STRING", {"multiline": True, "default": ""}],
        "lyrics": ["STRING", {"multiline": True, "default": ""}],
        "seed": ["INT", {"default": 0}],
        "mode": V3(["full", "melody"]),
        "max_abc_tokens": ["INT", {"default": 8192}]}}},
    "YuE2GenerateMusic": {"input": {"required": {
        "clip": ["CLIP"],
        "style": ["STRING", {"multiline": True, "default": ""}],
        "lyrics": ["STRING", {"multiline": True, "default": ""}],
        "seed": ["INT", {"default": 0}],
        "mode": V3(["full", "melody"]),
        "max_duration": ["INT", {"default": 300}],
        "top_p": ["FLOAT", {"default": 0.95}],
        "top_k": ["INT", {"default": 100}],
        "repetition_penalty": ["FLOAT", {"default": 1.2}]},
        # cfg_scale arrived in ComfyUI 4e779e5, after this graph was written:
        # optional, with a default, so a builder that reads the schema keeps
        # working without knowing about it.
        "optional": {"abc": ["STRING", {"forceInput": True}],
                     "cfg_scale": ["FLOAT", {"default": 1.0, "min": 0.0,
                                             "max": 100.0}]}}},
    "EmptyYuE2LatentAudio": {"input": {"required": {
        "seconds": ["FLOAT", {"default": 120.0}],
        "batch_size": ["INT", {"default": 1}]}}},
    "VAEDecodeAudioTiled": {"input": {"required": {
        "samples": ["LATENT"], "vae": ["VAE"],
        "tile_size": ["INT", {"default": 512, "min": 32, "max": 8192}],
        "overlap": ["INT", {"default": 64, "min": 0, "max": 1024}]}}},
    "VAEDecodeAudio": {"input": {"required": {"samples": ["LATENT"], "vae": ["VAE"]}}},
    "SaveAudioAdvanced": {"input": {"required": {
        "audio": ["AUDIO"],
        "filename_prefix": ["STRING", {"default": "audio/ComfyUI"}],
        "format": ["COMBO", {"options": [
            {"key": "flac", "inputs": {}},
            {"key": "mp3", "inputs": {"required": {
                "quality": ["COMBO", {"options": ["V0", "128k", "320k"],
                                      "default": "V0"}]}}},
            {"key": "opus", "inputs": {"required": {
                "quality": ["COMBO", {"options": ["64k", "96k", "128k", "192k", "320k"],
                                      "default": "128k"}]}}}]}]}}},
    "SheetSage2AudioToABC": {"input": {"required": {
        "audio_encoder": ["AUDIO_ENCODER"], "audio": ["AUDIO"],
        "mode": V3(["melody", "full"])}}},
    "AudioEncoderLoader": {"input": {"required": {
        "audio_encoder_name": V3(["sheetsage2_bf16.safetensors"])}}},
    "LoadAudio": {"input": {"required": {
        "audio": V3(["ref.wav"]), "start_time": ["FLOAT", {"default": 0.0}]}}},
    "PreviewAny": {"input": {"required": {"source": ["*", {}]}}},
}

HISTORY = {}
QUEUE_PENDING = []          # prompt_ids waiting
QUEUE_RUNNING = []          # prompt_ids executing
LOCK = threading.Lock()
WS_CLIENTS = []


def ws_send(obj):
    """One unmasked text frame to every connected /ws client."""
    data = json.dumps(obj).encode()
    head = bytearray([0x81])
    if len(data) < 126:
        head.append(len(data))
    else:
        head += bytes([126]) + struct.pack(">H", len(data))
    frame = bytes(head) + data
    with LOCK:
        for sock in WS_CLIENTS[:]:
            try:
                sock.sendall(frame)
            except OSError:
                WS_CLIENTS.remove(sock)
INTERRUPTS = []
DELETES = []
DELAY = float(__import__("os").environ.get("MOCK_DELAY", "2"))


def wav_bytes(seconds=3.0, rate=8000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


RUN_LOCK = threading.Lock()
WS_CLIENTS = []


def ws_send(obj):
    """One unmasked text frame to every connected /ws client."""
    data = json.dumps(obj).encode()
    head = bytearray([0x81])
    if len(data) < 126:
        head.append(len(data))
    else:
        head += bytes([126]) + struct.pack(">H", len(data))
    frame = bytes(head) + data
    with LOCK:
        for sock in WS_CLIENTS[:]:
            try:
                sock.sendall(frame)
            except OSError:
                WS_CLIENTS.remove(sock)   # ComfyUI executes one prompt at a time


def execute(pid, graph):
    with RUN_LOCK:                      # wait our turn, like the real queue
        with LOCK:
            if pid not in QUEUE_PENDING:   # deleted while waiting
                return
            QUEUE_PENDING.remove(pid)
            QUEUE_RUNNING.append(pid)
        _execute(pid, graph)


def _execute(pid, graph):
    ws_send({"type": "execution_start", "data": {"prompt_id": pid}})
    steps = max(1, int(DELAY * 2))
    for i in range(steps):
        time.sleep(0.5)
        ws_send({"type": "progress",
                 "data": {"value": i + 1, "max": steps, "prompt_id": pid}})
        with LOCK:
            if pid in INTERRUPTS:
                break
    time.sleep(0)
    fail_after = float(__import__("os").environ.get("MOCK_FAIL_AFTER", "0"))
    if fail_after:
        with LOCK:
            QUEUE_RUNNING.remove(pid)
            HISTORY[pid] = {"status": {"status_str": "error", "messages": [
                ["execution_error", {"node_type": "KSampler",
                                     "exception_message": "CUDA out of memory"}]]},
                            "outputs": {}}
        return
    with LOCK:
        if pid in INTERRUPTS:
            QUEUE_RUNNING.remove(pid)
            HISTORY[pid] = {"status": {"status_str": "error", "messages": [
                ["execution_interrupted", {"node_type": "KSampler",
                                           "exception_message": "interrupted"}]]},
                            "outputs": {}}
            return
    outputs = {"10": {"audio": [{"filename": f"{pid}.wav", "subfolder": "audio",
                                 "type": "output"}]}}
    for nid, node in graph.items():
        if node["class_type"] == "PreviewAny":
            outputs[nid] = {"text": ["X:1\nT:Mock score\nK:C\nCDEF|"]}
    with LOCK:
        QUEUE_RUNNING.remove(pid)
        HISTORY[pid] = {"status": {"status_str": "success", "messages": []},
                        "outputs": outputs}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/ws":
            key = self.headers.get("Sec-WebSocket-Key", "")
            accept = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()).decode()
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            with LOCK:
                WS_CLIENTS.append(self.connection)
            try:
                while self.connection.recv(1024):
                    pass
            except OSError:
                pass
            self.close_connection = True
            return
        if p == "/object_info":
            self._send(200, OBJECT_INFO)
        elif p == "/system_stats":
            self._send(200, {"system": {"comfyui_version": "0.35.0"}})
        elif p.startswith("/history/"):
            pid = p.rsplit("/", 1)[-1]
            with LOCK:
                self._send(200, {pid: HISTORY[pid]} if pid in HISTORY else {})
        elif p == "/queue":
            with LOCK:
                self._send(200, {
                    "queue_running": [[0, pid, {}, {}, []] for pid in QUEUE_RUNNING],
                    "queue_pending": [[0, pid, {}, {}, []] for pid in QUEUE_PENDING]})
        elif p == "/view":
            self._send(200, wav_bytes(), "audio/wav")
        else:
            self._send(404, {"error": "no route " + p})

    def do_POST(self):
        p = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b""
        if p == "/prompt":
            body = json.loads(raw)
            graph = body["prompt"]
            bad = validate(graph)
            if bad:
                self._send(400, {"error": {"type": "prompt_outputs_failed_validation",
                                           "message": "Prompt outputs failed validation",
                                           "details": ""},
                                 "node_errors": bad})
                return
            pid = f"pid{len(HISTORY) + len(QUEUE_PENDING) + len(QUEUE_RUNNING) + 1}"
            with LOCK:
                QUEUE_PENDING.append(pid)
            threading.Thread(target=execute, args=(pid, graph), daemon=True).start()
            self._send(200, {"prompt_id": pid, "number": 1, "node_errors": {}})
        elif p == "/interrupt":
            with LOCK:
                INTERRUPTS.extend(QUEUE_RUNNING)
            self._send(200, {})
        elif p == "/queue":
            body = json.loads(raw) if raw else {}
            with LOCK:
                for pid in body.get("delete", []):
                    DELETES.append(pid)
                    if pid in QUEUE_PENDING:
                        QUEUE_PENDING.remove(pid)
                        HISTORY[pid] = {"status": {"status_str": "error", "messages": [
                            ["execution_interrupted", {"node_type": "-",
                                                       "exception_message": "removed"}]]},
                                        "outputs": {}}
            self._send(200, {})
        elif p == "/upload/image":
            self._send(200, {"name": "ref.wav", "subfolder": "", "type": "input"})
        else:
            self._send(404, {"error": "no route " + p})


def validate(graph):
    """Reject anything ComfyUI itself would reject: unknown inputs, bad enums,
    missing required inputs, dangling links."""
    errs = {}
    for nid, node in graph.items():
        cls = node["class_type"]
        info = OBJECT_INFO.get(cls)
        if not info:
            errs[nid] = {"class_type": cls, "errors": [
                {"message": "Node not found", "details": cls}]}
            continue
        spec = dict(info["input"].get("required", {}))
        spec.update(info["input"].get("optional", {}))
        # A dynamic combo's chosen option brings its own inputs into the schema,
        # exactly as ComfyUI resolves them before validating.
        required_extra = {}
        for name, definition in list(spec.items()):
            if definition[0] != "COMBO":
                continue
            opts = (definition[1] or {}).get("options") or []
            if not (opts and isinstance(opts[0], dict)):
                continue
            for option in opts:
                if option.get("key") != node["inputs"].get(name):
                    continue
                for group in ("required", "optional"):
                    nested = (option.get("inputs") or {}).get(group) or {}
                    spec.update(nested)
                    if group == "required":
                        required_extra.update(nested)
        node_errs = []
        for name, value in node["inputs"].items():
            if name not in spec:
                node_errs.append({"message": "Unknown input", "details": name})
                continue
            definition = spec[name]
            kind = definition[0]
            if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int):
                if str(value[0]) not in graph:
                    node_errs.append({"message": "Link to a node that is not in the "
                                                 "prompt", "details": f"{name}={value}"})
                continue
            if isinstance(kind, list):                       # V1 combo
                if value not in kind:
                    node_errs.append({"message": "Value not in list",
                                      "details": f"{name}: {value!r} not in {kind}"})
            elif kind == "COMBO":
                opts = (definition[1] or {}).get("options") or []
                if opts and isinstance(opts[0], dict):
                    keys = [o["key"] for o in opts]
                    if value not in keys:
                        node_errs.append({"message": "Value not in list",
                                          "details": f"{name}: {value!r} not in {keys}"})
                elif value not in opts:
                    node_errs.append({"message": "Value not in list",
                                      "details": f"{name}: {value!r} not in {opts}"})
            elif kind == "INT" and not isinstance(value, int):
                node_errs.append({"message": "Wrong type", "details": f"{name} INT"})
            elif kind == "FLOAT" and not isinstance(value, (int, float)):
                node_errs.append({"message": "Wrong type", "details": f"{name} FLOAT"})
            elif kind == "STRING" and not isinstance(value, str):
                node_errs.append({"message": "Wrong type", "details": f"{name} STRING"})
        # required inputs that are links must be present
        for name, definition in (info["input"].get("required") or {}).items():
            if name in ("control_after_generate",):
                continue
            if name not in node["inputs"]:
                node_errs.append({"message": "Required input is missing",
                                  "details": name})
        for name in required_extra:
            if name not in node["inputs"]:
                node_errs.append({"message": "Required input is missing",
                                  "details": name})
        if node_errs:
            errs[nid] = {"class_type": cls, "errors": node_errs}
    return errs


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8188
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
