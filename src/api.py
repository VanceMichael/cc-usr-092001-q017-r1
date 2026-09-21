"""HTTP 路由与 JSON 序列化。

路径形如 /v1/freezes/{freeze_id}/retest；每个写请求在一个
BEGIN IMMEDIATE 事务内调用领域服务，领域异常映射为对应状态码。
"""

import json
import re
from http.server import BaseHTTPRequestHandler

from . import db
from .domain import masters, produce, quality, settlement, trading, trace
from .domain.common import (Conflict, DomainError, Forbidden, NotFound,
                            check_external_sequence, now_iso)


def _json_default(value):
    return str(value)


def _ingest_event(conn, payload: dict) -> dict:
    for field in ("event_id", "source_ref", "source_sequence", "occurred_at",
                  "payload_digest"):
        if payload.get(field) in (None, ""):
            raise DomainError(f"缺少必填字段：{field}", 400)
    check_external_sequence(conn, payload["source_ref"],
                            int(payload["source_sequence"]))
    conn.execute(
        "INSERT INTO event_log(event_id, source_ref, source_sequence, "
        "subject_ref, action, occurred_at, received_at, payload_digest, "
        "result_ref) VALUES (?,?,?,?,?,?,?,?,?)",
        (payload["event_id"], payload["source_ref"],
         int(payload["source_sequence"]), payload.get("subject_ref"),
         payload.get("action"), payload["occurred_at"], now_iso(),
         payload["payload_digest"], payload.get("result_ref")))
    return {"event_id": payload["event_id"],
            "received_at": now_iso(),
            "stored_occurred_at": payload["occurred_at"]}


# (method, regex, handler 名)；handler 接收 (conn, payload, path_params)。
def _build_routes() -> list[tuple[str, re.Pattern, str]]:
    p = lambda s: re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", s) + "$")
    return [
        ("POST", p("/v1/events"), "event.ingest"),

        ("POST", p("/v1/persons"), "masters.person"),
        ("POST", p("/v1/bases"), "masters.base"),
        ("POST", p("/v1/farmers"), "masters.farmer"),
        ("POST", p("/v1/water-bodies"), "masters.water_body"),
        ("POST", p("/v1/ponds"), "masters.pond"),
        ("POST", p("/v1/seed-lots"), "masters.seed_lot"),
        ("POST", p("/v1/stockings"), "masters.stocking"),
        ("POST", p("/v1/materials"), "masters.material"),
        ("POST", p("/v1/applications"), "masters.application"),
        ("POST", p("/v1/guidance"), "masters.guidance"),
        ("POST", p("/v1/certifications"), "masters.certification"),

        ("POST", p("/v1/inspections"), "quality.inspection"),
        ("POST", p("/v1/freezes"), "quality.freeze"),
        ("GET", p("/v1/freezes/(?P<freeze_id>[^/]+)"), "quality.freeze_detail"),
        ("POST", p("/v1/freezes/(?P<freeze_id>[^/]+)/retest"),
         "quality.retest"),
        ("POST", p("/v1/freezes/(?P<freeze_id>[^/]+)/release"),
         "quality.release"),
        ("POST", p("/v1/freezes/(?P<freeze_id>[^/]+)/destroy"),
         "quality.destroy"),

        ("POST", p("/v1/harvests"), "produce.harvest"),
        ("POST", p("/v1/grades"), "produce.grade"),
        ("POST", p("/v1/splits"), "produce.split"),
        ("POST", p("/v1/market-samples"), "produce.market_sample"),

        ("POST", p("/v1/export-orders"), "trading.create_order"),
        ("GET", p("/v1/export-orders/(?P<order_id>[^/]+)/availability"),
         "trading.availability"),
        ("POST", p("/v1/allocations"), "trading.allocate"),
        ("POST", p("/v1/allocations/release"), "trading.release_allocation"),
        ("POST", p("/v1/shipments"), "trading.ship"),
        ("POST", p("/v1/receipts"), "trading.receipt"),
        ("POST", p("/v1/returns"), "trading.register_return"),

        ("POST", p("/v1/contracts"), "settlement.create_contract"),
        ("POST", p("/v1/settlements/generate"), "settlement.generate"),
        ("POST", p("/v1/settlements/(?P<settlement_id>[^/]+)/confirm"),
         "settlement.confirm"),
        ("POST", p("/v1/settlements/(?P<settlement_id>[^/]+)/reverse"),
         "settlement.reverse"),
        ("GET", p("/v1/settlements/(?P<settlement_id>[^/]+)/explain"),
         "settlement.explain"),
        ("GET", p("/v1/farmers/(?P<farmer_id>[^/]+)/balance"),
         "settlement.farmer_balance"),

        ("GET", p("/v1/samples/{sample_id}/trace"),
         "trace.trace_sample"),
        ("GET", p("/v1/batches/{batch_id}/trace"), "trace.trace_batch"),
        ("GET", p("/v1/consistency"), "trace.consistency_report"),
    ]


ROUTES = _build_routes()

_MODULES = {
    "event": type("M", (), {"ingest": staticmethod(_ingest_event)}),
    "masters": masters,
    "quality": quality,
    "produce": produce,
    "trading": trading,
    "settlement": settlement,
    "trace": trace,
}

# GET 查询处理器不接收请求体。
GET_HANDLERS = {
    "quality.freeze_detail": lambda fn, conn, payload, params:
        fn(conn, params["freeze_id"]),
    "trading.availability": lambda fn, conn, payload, params:
        fn(conn, params["order_id"]),
    "settlement.explain": lambda fn, conn, payload, params:
        fn(conn, params["settlement_id"]),
    "settlement.farmer_balance": lambda fn, conn, payload, params:
        fn(conn, params["farmer_id"]),
    "trace.trace_sample": lambda fn, conn, payload, params:
        fn(conn, params["sample_id"]),
    "trace.trace_batch": lambda fn, conn, payload, params:
        fn(conn, params["batch_id"]),
    "trace.consistency_report": lambda fn, conn, payload, params: fn(conn),
}


def dispatch(method: str, path: str, raw_body: bytes) -> tuple[int, dict]:
    if method == "GET" and path == "/health":
        return 200, {"status": "ok"}
    for route_method, pattern, name in ROUTES:
        if route_method != method:
            continue
        match = pattern.match(path)
        if not match:
            continue
        params = match.groupdict()
        module_name, func_name = name.split(".")
        func = getattr(_MODULES[module_name], func_name)

        payload: dict = {}
        if method == "POST":
            if not raw_body:
                raise DomainError("请求体不能为空", 400)
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise DomainError("请求体不是合法 JSON", 400)
            if not isinstance(payload, dict):
                raise DomainError("请求体必须是 JSON 对象", 400)

        conn = db.connect()
        try:
            # executescript 会隐式提交，故建表必须在显式事务之外。
            db.initialize(conn)
            if method == "GET":
                result = GET_HANDLERS[name](func, conn, payload, params)
            else:
                # 路径中的资源标识注入请求体（同名时以路径为准）。
                for key, value in params.items():
                    payload[key] = value
                with db.transaction(conn):
                    result = func(conn, payload)
        finally:
            conn.close()
        return 200, result

    raise NotFound(f"未找到路由：{method} {path}")


class Handler(BaseHTTPRequestHandler):
    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False,
                          default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        if method == "GET" and self.path == "/health":
            self._send(200, {"status": "ok"})
            return
        try:
            status, result = dispatch(method, self.path,
                                      self._read_body() if method == "POST"
                                      else b"")
        except DomainError as exc:
            self._send(exc.status,
                       {"error": str(exc), "type": type(exc).__name__})
        except Exception as exc:  # noqa: BLE001 - 边界统一兜底
            self._send(500, {"error": f"内部错误：{exc}"})
        else:
            self._send(status, {"data": result})

    def log_message(self, format: str, *args: object) -> None:
        return
