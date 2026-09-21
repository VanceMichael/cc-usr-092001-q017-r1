"""HTTP API 层：JSON 路由、统一错误格式。

所有写接口接收 JSON 请求体，字段与服务层对应；错误返回
{"error": {"code": ..., "message": ...}} 与合适的 HTTP 状态码。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from . import services
from .db import connect
from .services import DomainError

Handler = Callable[[Any, dict[str, Any], re.Match[str] | None], tuple[int, Any]]


def _ok(result: Any) -> tuple[int, Any]:
    return 200, result


def _post(fn: Callable[..., Any]) -> Handler:
    def handle(conn: Any, body: dict[str, Any], match: re.Match[str] | None) -> tuple[int, Any]:
        return _ok(fn(conn, **body))

    return handle


def _trace(conn: Any, body: dict[str, Any], match: re.Match[str]) -> tuple[int, Any]:
    return _ok(services.trace_subject(conn, match.group("ref")))


def _explain(conn: Any, body: dict[str, Any], match: re.Match[str]) -> tuple[int, Any]:
    return _ok(services.explain_settlement(conn, match.group("ref")))


ROUTES: list[tuple[str, str, Handler]] = [
    ("POST", "/farmers", _post(services.create_farmer)),
    ("POST", "/ponds", _post(services.create_pond)),
    ("POST", "/contracts", _post(services.create_contract)),
    ("POST", "/seedling-batches", _post(services.create_seedling_batch)),
    ("POST", "/stockings", _post(services.stock_pond)),
    ("POST", "/inputs", _post(services.record_input)),
    ("POST", "/guidance", _post(services.record_guidance)),
    ("POST", "/harvests", _post(services.create_harvest)),
    ("POST", "/gradings", _post(services.grade_harvest)),
    ("POST", "/inspections", _post(services.record_inspection)),
    ("POST", "/freeze-actions", _post(services.record_freeze_action)),
    ("POST", "/export-orders", _post(services.create_export_order)),
    ("POST", "/allocations", _post(services.allocate_to_order)),
    ("POST", "/delivery-receipts", _post(services.record_delivery)),
    ("POST", "/settlements", _post(services.generate_settlement)),
    ("POST", "/returns", _post(services.record_return)),
    ("GET", r"^/trace/(?P<ref>[^/]+)$", _trace),
    ("GET", r"^/settlements/(?P<ref>[^/]+)$", _explain),
]

_COMPILED: list[tuple[str, re.Pattern[str], Handler]] = [
    (method, re.compile(pattern if pattern.startswith("^") else f"^{pattern}$"), handler)
    for method, pattern, handler in ROUTES
]


def health_payload() -> dict[str, str]:
    """返回可供运行环境探测的服务状态。"""
    return {"status": "ok"}


def make_handler(db_path: str | None) -> type[BaseHTTPRequestHandler]:
    class ApiHandler(BaseHTTPRequestHandler):
        """处理 API 请求，每个请求使用独立数据库连接。"""

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, body: dict[str, Any]) -> None:
            if self.path == "/health" and self.command == "GET":
                self._send_json(200, health_payload())
                return
            for method, pattern, handler in _COMPILED:
                if method != self.command:
                    continue
                match = pattern.match(self.path)
                if match is None:
                    continue
                conn = connect(db_path)
                try:
                    status, payload = handler(conn, body, match)
                finally:
                    conn.close()
                self._send_json(status, payload)
                return
            self._send_json(404, {"error": {"code": "not_found", "message": "接口不存在"}})

        def do_GET(self) -> None:
            self._guarded({})

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(
                    400, {"error": {"code": "invalid_json", "message": "请求体必须是 JSON 对象"}}
                )
                return
            if not isinstance(body, dict):
                self._send_json(
                    400, {"error": {"code": "invalid_json", "message": "请求体必须是 JSON 对象"}}
                )
                return
            self._guarded(body)

        def _guarded(self, body: dict[str, Any]) -> None:
            try:
                self._dispatch(body)
            except DomainError as exc:
                self._send_json(
                    exc.status, {"error": {"code": exc.code, "message": str(exc)}}
                )
            except Exception as exc:  # noqa: BLE001 - 兜底，避免连接悬挂
                self._send_json(
                    500, {"error": {"code": "internal_error", "message": f"内部错误：{exc}"}}
                )

        def log_message(self, format: str, *args: object) -> None:
            return

    return ApiHandler


def make_server(port: int, db_path: str | None = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("0.0.0.0", port), make_handler(db_path))
