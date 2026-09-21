"""领域公共件：错误、时间、金额、摘要与事件记录。"""

import hashlib
import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP

from typing import Any

CST = timezone(timedelta(hours=8))
QTY_EPS = 1e-6


class DomainError(Exception):
    """业务规则冲突，HTTP 层映射为 4xx。"""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class NotFound(DomainError):
    def __init__(self, message: str):
        super().__init__(message, 404)


class Conflict(DomainError):
    def __init__(self, message: str):
        super().__init__(message, 409)


class Forbidden(DomainError):
    def __init__(self, message: str):
        super().__init__(message, 403)


def now_iso() -> str:
    return datetime.now(CST).isoformat()


def money(value: Decimal) -> float:
    """金额按分四舍五入，返回 float 仅用于序列化。"""
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def as_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def canonical_digest(payload: Any) -> str:
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def require(payload: dict, fields: list[str]) -> None:
    missing = [f for f in fields if f not in payload or payload[f] == ""]
    if missing:
        raise DomainError(f"缺少必填字段：{', '.join(missing)}", 400)


def record_event(conn, source_ref: str, subject_ref: str | None, action: str,
                 payload: dict, occurred_at: str | None = None,
                 result_ref: str | None = None) -> str:
    """追加一条交换事件。同一来源序号在事务内单调递增。

    外部事件自带序号时应先调用 ``check_external_sequence`` 校验。
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(source_sequence), 0) + 1 AS seq "
        "FROM event_log WHERE source_ref = ?",
        (source_ref,),
    ).fetchone()
    seq = row["seq"]
    digest = canonical_digest(payload)
    event_id = f"EVT-{source_ref}-{seq}"
    conn.execute(
        "INSERT INTO event_log(event_id, source_ref, source_sequence, subject_ref, "
        "action, occurred_at, received_at, payload_digest, result_ref) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (event_id, source_ref, seq, subject_ref, action,
         occurred_at or now_iso(), now_iso(), digest, result_ref),
    )
    return event_id


def check_external_sequence(conn, source_ref: str, sequence: int) -> None:
    """外部来源序号必须严格接续，拒绝乱序、重复与缺口。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(source_sequence), 0) AS seq "
        "FROM event_log WHERE source_ref = ?",
        (source_ref,),
    ).fetchone()
    expected = row["seq"] + 1
    if sequence != expected:
        raise Conflict(
            f"来源 {source_ref} 的序号应为 {expected}，收到 {sequence}")


def get_or_404(conn, table: str, key: str, value: str):
    row = conn.execute(
        f"SELECT * FROM {table} WHERE {key} = ?", (value,)
    ).fetchone()
    if row is None:
        raise NotFound(f"{table} 中不存在 {key}={value}")
    return row
