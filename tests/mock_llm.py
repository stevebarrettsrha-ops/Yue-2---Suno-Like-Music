"""A stand-in chat server: OpenAI-compatible and Anthropic, streamed.

The model name picks the behaviour, so one server covers every case:

  (anything)     a labelled song, with reasoning in <think> the page must not show
  json           the whole answer as one plain JSON body, not a stream
  slow           a word every half second, for Stop and for one-at-a-time
  fail401        the key is refused
  bare           an answer with no TITLE/STYLE/LYRICS labels at all
  claude-refuse  (Anthropic) a refusal

GET /_last answers with the last request's path, headers and body, so a test
can see what was actually sent.
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SONG = ("<think>The person wants a song about trains. Plan: verse, chorus."
        "</think>\n"
        "**TITLE:** Last Train Home\n"
        "**STYLE:** English, indie folk, warm male vocal, 92 BPM, "
        "fingerpicked guitar, brushed drums\n"
        "**LYRICS:**\n"
        "[Verse]\nThe platform hums a tired tune\nI count the lights "
        "beneath the moon\n"
        "[Chorus]\nLast train home, carry me slow\nLast train home, "
        "where the long roads go\n\n\n"
        "[Outro]\n")
BARE = "[Verse]\nOnly words here\n[Chorus]\nNo labels at all\n"
LAST = {}


def pieces(text, size=17):
    return [text[i:i + size] for i in range(0, len(text), size)]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _events(self, chunks, slow=False):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for event, payload in chunks:
                line = (f"event: {event}\n" if event else "") + f"data: {payload}\n\n"
                self.wfile.write(line.encode())
                self.wfile.flush()
                if slow:
                    time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def do_GET(self):
        if self.path == "/_last":
            return self._json(200, LAST)
        LAST.update(path=self.path, headers=dict(self.headers), body=None)
        if self.path.startswith("/v1/models"):
            if self.headers.get("x-api-key"):
                return self._json(200, {"data": [
                    {"type": "model", "id": "claude-opus-5",
                     "display_name": "Claude Opus 5",
                     "created_at": "2026-01-01T00:00:00Z"}],
                    "has_more": False, "first_id": "claude-opus-5",
                    "last_id": "claude-opus-5"})
            return self._json(200, {"object": "list", "data": [
                {"id": "qwen3:8b", "object": "model"},
                {"id": "gemma3:12b", "object": "model"}]})
        self._json(404, {"error": {"message": "no such path"}})

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(size) or b"{}")
        LAST.update(path=self.path, headers=dict(self.headers), body=body)
        model = body.get("model", "")
        if model == "fail401":
            return self._json(401, {"error": {"message": "Incorrect API key"}})
        text = BARE if model == "bare" else SONG
        if self.path.startswith("/v1/chat/completions"):
            return self._openai(model, text)
        if self.path.startswith("/v1/messages"):
            return self._anthropic(model, text)
        self._json(404, {"error": {"message": "no such path"}})

    def _openai(self, model, text):
        if model == "json":
            return self._json(200, {"choices": [
                {"message": {"role": "assistant", "content": text}}]})
        chunks = [("", json.dumps({"choices": [{"delta": {"content": p}}]}))
                  for p in pieces(text if model != "slow" else text * 20)]
        chunks.append(("", "[DONE]"))
        self._events(chunks, slow=model == "slow")

    def _anthropic(self, model, text):
        refuse = model == "claude-refuse"
        msg = {"id": "msg_1", "type": "message", "role": "assistant",
               "content": [], "model": model, "stop_reason": None,
               "stop_sequence": None,
               "usage": {"input_tokens": 10, "output_tokens": 1}}
        chunks = [("message_start", json.dumps({"type": "message_start",
                                                "message": msg})),
                  ("content_block_start", json.dumps({
                      "type": "content_block_start", "index": 0,
                      "content_block": {"type": "text", "text": ""}}))]
        for p in ([] if refuse else pieces(text)):
            chunks.append(("content_block_delta", json.dumps({
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": p}})))
        chunks += [("content_block_stop", json.dumps({
                        "type": "content_block_stop", "index": 0})),
                   ("message_delta", json.dumps({
                       "type": "message_delta",
                       "delta": {"stop_reason": "refusal" if refuse else "end_turn",
                                 "stop_sequence": None},
                       "usage": {"output_tokens": 50}})),
                   ("message_stop", json.dumps({"type": "message_stop"}))]
        self._events(chunks)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
