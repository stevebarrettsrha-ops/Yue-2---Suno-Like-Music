"""A stand-in ComfyUI: /object_info shaped exactly like v0.35's, plus a queue."""
import json, threading, time, io, wave, struct, hashlib, base64
import os, pathlib, shutil, subprocess, tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The schema is not written by hand here: it is a capture of what a real
# ComfyUI answers /object_info with, trimmed to the nodes this graph uses, with
# the model lists filled in as a set-up install would have them. Hand-written
# shapes drifted from the real ones — inputs invented, inputs missed, an INT
# where the real node takes a FLOAT — and a stand-in that disagrees with the
# thing it stands in for is worth very little.
#
#   Refresh it against a real server with:
#     curl -s http://127.0.0.1:8188/object_info > /tmp/all.json
#   and re-trim (see the commit that introduced this file).
OBJECT_INFO = json.loads(
    (pathlib.Path(__file__).with_name("object_info.json")).read_text())

# ComfyUI scans its model folders once and serves the cached list. A checkpoint
# that lands afterwards is invisible until it rescans, which is what a restart
# is for. BLANK_CKPT_CALLS reproduces that: the first N /object_info answers
# carry no checkpoints, the rest carry the real list.
BLANK_CKPT_CALLS = int(os.environ.get("MOCK_BLANK_CKPT_CALLS", "0"))
OBJECT_INFO_CALLS = 0

HISTORY = {}
FORMATS = {}          # which format each prompt asked to be saved as
QUEUE_PENDING = []          # prompt_ids waiting
QUEUE_RUNNING = []          # prompt_ids executing
LOCK = threading.Lock()
WS_CLIENTS = []


def _object_info():
    """The schema, with the checkpoint list withheld for the first N calls."""
    global OBJECT_INFO_CALLS
    with LOCK:
        OBJECT_INFO_CALLS += 1
        withhold = OBJECT_INFO_CALLS <= BLANK_CKPT_CALLS
    if not withhold:
        return OBJECT_INFO
    out = json.loads(json.dumps(OBJECT_INFO))
    spec = out["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"]
    spec[0] = []
    return out


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
FAILED_NODES = set()
DELAY = float(__import__("os").environ.get("MOCK_DELAY", "2"))


def wav_bytes(seconds=3.0, rate=8000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


_AUDIO: dict = {}


def rendered_audio(fmt="flac"):
    """A render, in whichever format the prompt asked SaveAudioAdvanced for.

    ComfyUI encodes flac, mp3 and opus itself and has no wav encoder at all,
    so a stand-in that always handed back the same thing would hide both which
    formats need converting and which do not.
    """
    if fmt in _AUDIO:
        return _AUDIO[fmt]
    ffmpeg = shutil.which("ffmpeg")
    made = (wav_bytes(), ".wav")
    if ffmpeg and fmt in ("flac", "mp3", "opus"):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = f"{tmp}/a.wav", f"{tmp}/a.{fmt}"
            open(src, "wb").write(wav_bytes())
            done = subprocess.run([ffmpeg, "-y", "-loglevel", "error",
                                   "-i", src, dst], capture_output=True)
            if done.returncode == 0 and os.path.getsize(dst):
                made = (open(dst, "rb").read(), f".{fmt}")
    _AUDIO[fmt] = made
    return made


def asked_format(graph):
    for node in (graph or {}).values():
        if node.get("class_type") == "SaveAudioAdvanced":
            return node.get("inputs", {}).get("format", "flac")
    return "flac"


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


def native_score(graph, preview):
    """A score in YuE2's native two-voice form, as the real nodes write it.

    The planner's seed picks the second note, so plans made with different
    seeds really differ; "full" carries chords and "melody" does not, which is
    the difference between the two modes of both the planner and SheetSage2.
    """
    source = graph.get(str((preview["inputs"].get("source") or ["", 0])[0]), {})
    inputs = source.get("inputs", {})
    full = inputs.get("mode", "full") == "full"
    second = "defgab"[int(inputs.get("seed") or 0) % 6]
    tempo = 76 if source.get("class_type") == "SheetSage2AudioToABC" else 88
    c = (lambda name: f'"{name}"') if full else (lambda name: "")
    return "\n".join([
        "X:1", "T:", "M:4/4", "L:1/32", f"Q:1/4={tempo}",
        'V: Vocal clef=treble name="Vocal Melody" snm="Vocal"',
        'V: Ins clef=treble name="Ins Melody" snm="Inst."',
        "K:G",
        "% verse",
        "V: Vocal",
        f"{c('G')}B8{second}8{c('Em')}c8A8|{c('C')}G16{c('D')}A16|",
        "V: Ins",
        "Z2|",
        "% chorus",
        "V: Vocal",
        f"{c('G')}d8d8{c('C')}e8e8|{c('D')}d32|",
        "V: Ins",
        "z16G16|B32|",
    ]) + "\n"


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
    fail_node = __import__("os").environ.get("MOCK_FAIL_NODE", "")
    graph_has_fail_node = any(n.get("class_type") == fail_node
                              for n in graph.values())
    fail_once = bool(__import__("os").environ.get("MOCK_FAIL_ONCE"))
    should_fail_node = (fail_node and graph_has_fail_node
                        and (not fail_once or fail_node not in FAILED_NODES))
    if fail_after or should_fail_node:
        with LOCK:
            if should_fail_node:
                FAILED_NODES.add(fail_node)
            QUEUE_RUNNING.remove(pid)
            HISTORY[pid] = {"status": {"status_str": "error", "messages": [
                ["execution_error", {
                    "node_type": fail_node or "KSampler",
                    "exception_message": ("[Errno 22] Invalid argument"
                                          if fail_node else "CUDA out of memory")}]]},
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
    outputs = {"10": {"audio": [{"filename": f"{pid}{rendered_audio(FORMATS.get(pid, 'flac'))[1]}",
                                 "subfolder": "audio", "type": "output"}]}}
    for nid, node in graph.items():
        if node["class_type"] == "PreviewAny":
            outputs[nid] = {"text": [native_score(graph, node)]}
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
            self._send(200, _object_info())
        elif p == "/system_stats":
            # argv is how a client tells which ComfyUI is on the port.
            # MOCK_NO_ARGV plays an engine that will not say — ComfyUI Desktop
            # and other wrappers launch in ways that name no main.py.
            system = {"comfyui_version": "0.35.0"}
            if not os.environ.get("MOCK_NO_ARGV"):
                root = os.environ.get("MOCK_COMFY_ROOT", "/opt/ComfyUI")
                system["argv"] = [f"{root}/main.py"]
            body = {"system": system}
            # A card, when the test wants one. Real ComfyUI always lists its
            # devices here; vram_total is what says whether a checkpoint can
            # stay resident while a song renders.
            vram = os.environ.get("MOCK_VRAM")
            if vram:
                body["devices"] = [{"name": "cuda:0 Pretend", "type": "cuda",
                                    "index": 0, "vram_total": int(vram)}]
            self._send(200, body)
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
            name = self.path.split("filename=")[-1].split("&")[0]
            fmt = name.rsplit(".", 1)[-1] if "." in name else "flac"
            data, suffix = rendered_audio(fmt)
            self._send(200, data, "audio/" + suffix.lstrip("."))
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
            FORMATS[pid] = asked_format(graph)
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
            if not isinstance(definition, list) or isinstance(definition[0], list):
                continue
            opts = (definition[1] or {}).get("options") or []
            # A real server types this COMFY_DYNAMICCOMBO_V3, not COMBO; what
            # marks it out is that its options are whole inputs, not values.
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
            elif str(kind).startswith(("COMBO", "COMFY_DYNAMICCOMBO")):
                opts = (definition[1] or {}).get("options") or []
                if opts and isinstance(opts[0], dict):
                    keys = [o["key"] for o in opts]
                    if value not in keys:
                        node_errs.append({"message": "Value not in list",
                                          "details": f"{name}: {value!r} not in {keys}"})
                elif opts and value not in opts:
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
