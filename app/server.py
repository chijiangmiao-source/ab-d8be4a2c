"""HTTP API + review console (Python standard library only)."""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .model import ModelError
from .store import AuditStore, semantic_fingerprint
from .verifier import illegal_model_result, run_review

STORE_PATH = os.environ.get("STORE_PATH", "data/reviews.json")
HERE = os.path.dirname(os.path.abspath(__file__))

store = AuditStore(STORE_PATH)


def _json_bytes(obj, status=200):
    body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    return status, body


class Handler(BaseHTTPRequestHandler):
    server_version = "InterlockReview/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[http] " + (fmt % args) + "\n")

    def _send(self, status, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(*_json_bytes(obj, status))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._json({"status": "ok", "service": "interlock-review"})
            return
        if path == "/" or path == "/index.html":
            try:
                with open(os.path.join(HERE, "static", "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._json({"error": "page missing"}, 500)
            return
        if path.startswith("/api/reviews/"):
            audit_id = path[len("/api/reviews/"):]
            result = store.get(audit_id)
            if result is None:
                self._json({"error": "not_found", "audit_id": audit_id}, 404)
            else:
                self._json(result)
            return
        self._json({"error": "not_found", "path": path}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/reviews":
            self._json({"error": "not_found", "path": path}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json({"error": "请求体不是合法 JSON"}, 400)
            return
        if not isinstance(payload, dict):
            self._json({"error": "请求体必须为 JSON 对象"}, 400)
            return
        audit_id = payload.get("audit_id")
        if not isinstance(audit_id, str) or not audit_id.strip():
            self._json({"error": "audit_id 必须为非空字符串"}, 400)
            return
        try:
            fingerprint = semantic_fingerprint(payload)
            result = run_review(payload)
        except ModelError as exc:
            self._json(illegal_model_result(audit_id, exc), 422)
            return
        except (KeyError, TypeError) as exc:
            self._json(
                illegal_model_result(audit_id, ModelError(f"请求字段缺失或类型错误: {exc}")),
                422,
            )
            return
        stored, status = store.submit(audit_id, fingerprint, result, payload)
        self._json(stored, 200 if status != "new" else 201)


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"interlock-review listening on {host}:{port} (store={STORE_PATH})",
          flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
