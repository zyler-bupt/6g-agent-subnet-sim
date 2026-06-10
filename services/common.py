from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def env_json(name: str, default: Any) -> Any:
    raw = os.environ.get(name)
    if not raw:
        return default
    return json.loads(raw)


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length == 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"error": raw}
        return exc.code, body
    except URLError as exc:
        return 599, {"error": str(exc.reason)}


def run_server(handler_cls: type[BaseHTTPRequestHandler], port: int) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
    print(f"{handler_cls.__name__} listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


class JsonHandler(BaseHTTPRequestHandler):
    routes_owner: Any = None

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        try:
            payload = read_json(self) if method == "POST" else {}
            status, body = self.routes_owner.handle(method, self.path, payload)
            json_response(self, status, body)
        except Exception as exc:  # pragma: no cover - defensive service boundary
            json_response(self, 500, {"error": type(exc).__name__, "detail": str(exc)})

