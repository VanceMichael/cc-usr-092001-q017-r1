"""白玉蟹联合品控结算链的领域服务。

主线：塘口 → 养殖户 → 苗种批次 → 捕捞批次 → 分级批次 → 出口分配 → 交付回执 → 结算。

关键不变量：
- 守恒：投苗不超过苗种批次余量；分级重量之和必须等于起捕毛重；
  分配扣减可用量，任何时刻 已分配 ≤ 批次重量。
- 冻结：检测不合格只冻结相关水体与关联批次；复检、解封、销毁由不同角色签署。
- 不超卖：订单分配在同一事务内用条件更新扣减订单额度与批次可用量。
- 不改账：结算生成后不改写，退货以冲正行（负金额）追加并指向原行。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator


class DomainError(Exception):
    """业务规则冲突，携带稳定错误代码与 HTTP 状态码。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# 冻结处置动作与签署角色的对应关系：复检、解封、销毁必须由不同角色签署
FREEZE_ACTION_ROLES = {
    "retest": "inspector",
    "release": "quality_manager",
    "destroy": "destruction_officer",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_ref(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _require_iso8601(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise DomainError("invalid_time", f"{field} 必须是带偏移量的 ISO 8601 字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise DomainError("invalid_time", f"{field} 必须是带偏移量的 ISO 8601 字符串") from None
    if parsed.tzinfo is None:
        raise DomainError("invalid_time", f"{field} 必须携带时区偏移量")


def _require_digest(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise DomainError("invalid_digest", f"{field} 必须是 sha256: 前缀的摘要")


def _require_positive_int(value: Any, field: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DomainError("invalid_quantity", f"{field} 必须是正整数")


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """写事务：立即获取写锁，保证并发分配等检查-扣减序列的原子性。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except DomainError:
        conn.execute("ROLLBACK")
        raise
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise DomainError("conflict", f"记录冲突或引用缺失：{exc}", 409) from exc
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _get(conn: sqlite3.Connection, sql: str, params: tuple, message: str) -> sqlite3.Row:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise DomainError("not_found", message, 404)
    return row


def _record_event(
    conn: sqlite3.Connection,
    subject_ref: str,
    event_type: str,
    occurred_at: str,
    payload: dict[str, Any],
) -> None:
    """按 contracts/ 约定的信封记录领域事件，来源序号在本服务内递增。"""
    digest = "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    seq = conn.execute(
        "SELECT COALESCE(MAX(source_sequence), 0) + 1 AS s FROM domain_event"
    ).fetchone()["s"]
    conn.execute(
        "INSERT INTO domain_event(event_id, subject_ref, event_type, occurred_at,"
        " source_sequence, payload_digest) VALUES(?,?,?,?,?,?)",
        (_new_ref("EVT"), subject_ref, event_type, occurred_at, seq, digest),
    )


# ---------------------------------------------------------------------------
# 主数据建档
# ---------------------------------------------------------------------------


def create_farmer(
    conn: sqlite3.Connection,
    *,
    farmer_ref: str,
    display_name: str,
    base_region: str,
    assist_tier: str = "standard",
) -> dict[str, Any]:
    with _tx(conn):
        conn.execute(
            "INSERT INTO farmer(farmer_ref, display_name, base_region, assist_tier, created_at)"
            " VALUES(?,?,?,?,?)",
            (farmer_ref, display_name, base_region, assist_tier, _now()),
        )
        _record_event(conn, farmer_ref, "farmer.created", _now(), {"base_region": base_region})
    return {"farmer_ref": farmer_ref}


def create_pond(
    conn: sqlite3.Connection,
    *,
    pond_ref: str,
    farmer_ref: str,
    base_region: str,
    area_mu: float,
    water_body_ref: str,
) -> dict[str, Any]:
    if not isinstance(area_mu, (int, float)) or area_mu <= 0:
        raise DomainError("invalid_area", "area_mu 必须为正数")
    with _tx(conn):
        _get(conn, "SELECT farmer_ref FROM farmer WHERE farmer_ref=?", (farmer_ref,), "养殖户不存在")
        conn.execute(
            "INSERT INTO pond(pond_ref, farmer_ref, base_region, area_mu, water_body_ref,"
            " status, created_at) VALUES(?,?,?,?,?,'active',?)",
            (pond_ref, farmer_ref, base_region, area_mu, water_body_ref, _now()),
        )
        _record_event(conn, pond_ref, "pond.created", _now(), {"farmer_ref": farmer_ref})
    return {"pond_ref": pond_ref}


def create_contract(
    conn: sqlite3.Connection,
    *,
    contract_ref: str,
    version: int,
    farmer_ref: str,
    effective_from: str,
    effective_to: str,
    prices: list[dict[str, Any]],
) -> dict[str, Any]:
    """登记合同版本及其等级价格。版本一旦登记不可改写，调价必须升版本。"""
    _require_iso8601(effective_from, "effective_from")
    _require_iso8601(effective_to, "effective_to")
    if not prices:
        raise DomainError("invalid_prices", "合同必须包含至少一个等级价格")
    for price in prices:
        _require_positive_int(price.get("unit_price_cents"), "unit_price_cents")
        assist = price.get("assist_cents", 0)
        if not isinstance(assist, int) or assist < 0:
            raise DomainError("invalid_prices", "assist_cents 必须是非负整数")
    with _tx(conn):
        _get(conn, "SELECT farmer_ref FROM farmer WHERE farmer_ref=?", (farmer_ref,), "养殖户不存在")
        conn.execute(
            "INSERT INTO contract(contract_ref, version, farmer_ref, effective_from,"
            " effective_to, created_at) VALUES(?,?,?,?,?,?)",
            (contract_ref, version, farmer_ref, effective_from, effective_to, _now()),
        )
        for price in prices:
            conn.execute(
                "INSERT INTO contract_price(contract_ref, version, grade, unit_price_cents,"
                " assist_cents) VALUES(?,?,?,?,?)",
                (
                    contract_ref,
                    version,
                    price["grade"],
                    price["unit_price_cents"],
                    price.get("assist_cents", 0),
                ),
            )
        _record_event(
            conn, contract_ref, "contract.created", _now(),
            {"version": version, "farmer_ref": farmer_ref},
        )
    return {"contract_ref": contract_ref, "version": version}


def create_seedling_batch(
    conn: sqlite3.Connection,
    *,
    seed_batch_ref: str,
    supplied_by: str,
    quantity: int,
    supplied_at: str,
    digest: str | None = None,
) -> dict[str, Any]:
    _require_positive_int(quantity, "quantity")
    _require_iso8601(supplied_at, "supplied_at")
    if digest is not None:
        _require_digest(digest, "digest")
    with _tx(conn):
        conn.execute(
            "INSERT INTO seedling_batch(seed_batch_ref, supplied_by, quantity, remaining,"
            " supplied_at, digest) VALUES(?,?,?,?,?,?)",
            (seed_batch_ref, supplied_by, quantity, quantity, supplied_at, digest),
        )
        _record_event(conn, seed_batch_ref, "seedling.created", supplied_at, {"quantity": quantity})
    return {"seed_batch_ref": seed_batch_ref}


def stock_pond(
    conn: sqlite3.Connection,
    *,
    stock_ref: str,
    seed_batch_ref: str,
    pond_ref: str,
    quantity: int,
    occurred_at: str,
) -> dict[str, Any]:
    """投苗：扣减苗种批次余量，保证 Σ投放 ≤ 批次总量（守恒）。"""
    _require_positive_int(quantity, "quantity")
    _require_iso8601(occurred_at, "occurred_at")
    with _tx(conn):
        pond = _get(conn, "SELECT * FROM pond WHERE pond_ref=?", (pond_ref,), "塘口不存在")
        if pond["status"] != "active":
            raise DomainError("pond_frozen", "塘口处于冻结状态，禁止投苗", 409)
        cursor = conn.execute(
            "UPDATE seedling_batch SET remaining = remaining - ?"
            " WHERE seed_batch_ref=? AND remaining >= ?",
            (quantity, seed_batch_ref, quantity),
        )
        if cursor.rowcount == 0:
            raise DomainError("insufficient_seedlings", "苗种批次余量不足，投放不守恒", 409)
        conn.execute(
            "INSERT INTO stocking(stock_ref, seed_batch_ref, pond_ref, quantity, occurred_at)"
            " VALUES(?,?,?,?,?)",
            (stock_ref, seed_batch_ref, pond_ref, quantity, occurred_at),
        )
        _record_event(conn, pond_ref, "pond.stocked", occurred_at,
                      {"seed_batch_ref": seed_batch_ref, "quantity": quantity})
    return {"stock_ref": stock_ref}


# ---------------------------------------------------------------------------
# 养殖过程记录
# ---------------------------------------------------------------------------


def record_input(
    conn: sqlite3.Connection,
    *,
    input_ref: str,
    pond_ref: str,
    kind: str,
    material_ref: str,
    material_digest: str,
    quantity: float,
    unit: str,
    occurred_at: str,
    recorded_by: str,
) -> dict[str, Any]:
    """登记投入品。材料只保存受控引用与 sha256 摘要。冻结期间仍如实记录事实。"""
    _require_iso8601(occurred_at, "occurred_at")
    _require_digest(material_digest, "material_digest")
    if not isinstance(quantity, (int, float)) or quantity <= 0:
        raise DomainError("invalid_quantity", "quantity 必须为正数")
    with _tx(conn):
        _get(conn, "SELECT pond_ref FROM pond WHERE pond_ref=?", (pond_ref,), "塘口不存在")
        conn.execute(
            "INSERT INTO input_record(input_ref, pond_ref, kind, material_ref, material_digest,"
            " quantity, unit, occurred_at, recorded_by) VALUES(?,?,?,?,?,?,?,?,?)",
            (input_ref, pond_ref, kind, material_ref, material_digest,
             quantity, unit, occurred_at, recorded_by),
        )
        _record_event(conn, pond_ref, "input.recorded", occurred_at,
                      {"input_ref": input_ref, "kind": kind, "material_ref": material_ref})
    return {"input_ref": input_ref}


def record_guidance(
    conn: sqlite3.Connection,
    *,
    guidance_ref: str,
    pond_ref: str,
    advisor_ref: str,
    summary: str,
    occurred_at: str,
) -> dict[str, Any]:
    _require_iso8601(occurred_at, "occurred_at")
    with _tx(conn):
        _get(conn, "SELECT pond_ref FROM pond WHERE pond_ref=?", (pond_ref,), "塘口不存在")
        conn.execute(
            "INSERT INTO guidance(guidance_ref, pond_ref, advisor_ref, summary, occurred_at)"
            " VALUES(?,?,?,?,?)",
            (guidance_ref, pond_ref, advisor_ref, summary, occurred_at),
        )
        _record_event(conn, pond_ref, "guidance.recorded", occurred_at,
                      {"guidance_ref": guidance_ref, "advisor_ref": advisor_ref})
    return {"guidance_ref": guidance_ref}


# ---------------------------------------------------------------------------
# 起捕与分级
# ---------------------------------------------------------------------------


def create_harvest(
    conn: sqlite3.Connection,
    *,
    batch_ref: str,
    pond_ref: str,
    gross_weight_g: int,
    occurred_at: str,
    actor_ref: str,
) -> dict[str, Any]:
    """起捕生成捕捞批次，自动关联该塘口最近一次投苗的苗种批次。"""
    _require_positive_int(gross_weight_g, "gross_weight_g")
    _require_iso8601(occurred_at, "occurred_at")
    with _tx(conn):
        pond = _get(conn, "SELECT * FROM pond WHERE pond_ref=?", (pond_ref,), "塘口不存在")
        if pond["status"] != "active":
            raise DomainError("pond_frozen", "塘口处于冻结状态，禁止起捕", 409)
        stocking = conn.execute(
            "SELECT seed_batch_ref FROM stocking WHERE pond_ref=?"
            " ORDER BY occurred_at DESC, stock_ref DESC LIMIT 1",
            (pond_ref,),
        ).fetchone()
        conn.execute(
            "INSERT INTO harvest_batch(batch_ref, pond_ref, seed_batch_ref, gross_weight_g,"
            " graded_weight_g, status, occurred_at) VALUES(?,?,?,?,0,'open',?)",
            (batch_ref, pond_ref, stocking["seed_batch_ref"] if stocking else None,
             gross_weight_g, occurred_at),
        )
        _record_event(conn, batch_ref, "harvest.created", occurred_at,
                      {"pond_ref": pond_ref, "gross_weight_g": gross_weight_g,
                       "actor_ref": actor_ref})
    return {"batch_ref": batch_ref}


def grade_harvest(
    conn: sqlite3.Connection,
    *,
    batch_ref: str,
    lots: list[dict[str, Any]],
    actor_ref: str,
    occurred_at: str,
) -> dict[str, Any]:
    """分级：把捕捞批次拆分为若干等级批次，重量之和必须等于起捕毛重（守恒）。"""
    _require_iso8601(occurred_at, "occurred_at")
    if not lots:
        raise DomainError("invalid_lots", "分级结果不能为空")
    for lot in lots:
        _require_positive_int(lot.get("weight_g"), "weight_g")
        if not lot.get("grade"):
            raise DomainError("invalid_lots", "每个分级批次必须指定等级")
    with _tx(conn):
        batch = _get(
            conn, "SELECT * FROM harvest_batch WHERE batch_ref=?", (batch_ref,), "捕捞批次不存在"
        )
        if batch["status"] != "open":
            raise DomainError("batch_not_open", "只有待分级状态的批次可以分级", 409)
        total = sum(lot["weight_g"] for lot in lots)
        if total != batch["gross_weight_g"]:
            raise DomainError(
                "conservation_violation",
                f"分级重量之和 {total} 克不等于起捕毛重 {batch['gross_weight_g']} 克",
                409,
            )
        for lot in lots:
            conn.execute(
                "INSERT INTO grade_lot(lot_ref, batch_ref, grade, weight_g, available_g, status)"
                " VALUES(?,?,?,?,?,'available')",
                (lot["lot_ref"], batch_ref, lot["grade"], lot["weight_g"], lot["weight_g"]),
            )
        conn.execute(
            "UPDATE harvest_batch SET status='graded', graded_weight_g=? WHERE batch_ref=?",
            (total, batch_ref),
        )
        _record_event(conn, batch_ref, "harvest.graded", occurred_at,
                      {"lots": [{k: lot[k] for k in ("lot_ref", "grade", "weight_g")}
                                for lot in lots], "actor_ref": actor_ref})
    return {"batch_ref": batch_ref, "lots": [lot["lot_ref"] for lot in lots]}


# ---------------------------------------------------------------------------
# 抽检、冻结与处置
# ---------------------------------------------------------------------------


def _resolve_scope(
    conn: sqlite3.Connection, scope_type: str, scope_ref: str
) -> tuple[sqlite3.Row, list[str]]:
    """把检测对象解析为（塘口, 关联捕捞批次列表）。"""
    if scope_type == "pond":
        pond = _get(conn, "SELECT * FROM pond WHERE pond_ref=?", (scope_ref,), "塘口不存在")
        rows = conn.execute(
            "SELECT batch_ref FROM harvest_batch WHERE pond_ref=? AND status IN ('open','graded')",
            (scope_ref,),
        ).fetchall()
        return pond, [row["batch_ref"] for row in rows]
    if scope_type == "harvest_batch":
        batch = _get(
            conn, "SELECT * FROM harvest_batch WHERE batch_ref=?", (scope_ref,), "捕捞批次不存在"
        )
        pond = _get(
            conn, "SELECT * FROM pond WHERE pond_ref=?", (batch["pond_ref"],), "塘口不存在"
        )
        return pond, [scope_ref]
    if scope_type == "grade_lot":
        lot = _get(conn, "SELECT * FROM grade_lot WHERE lot_ref=?", (scope_ref,), "分级批次不存在")
        batch = _get(
            conn, "SELECT * FROM harvest_batch WHERE batch_ref=?",
            (lot["batch_ref"],), "捕捞批次不存在",
        )
        pond = _get(
            conn, "SELECT * FROM pond WHERE pond_ref=?", (batch["pond_ref"],), "塘口不存在"
        )
        return pond, [batch["batch_ref"]]
    raise DomainError("invalid_scope", "scope_type 必须是 pond / harvest_batch / grade_lot")


def _freeze_scope(
    conn: sqlite3.Connection,
    pond_ref: str,
    batch_refs: list[str],
    inspection_ref: str,
    actor_ref: str,
    occurred_at: str,
) -> list[str]:
    """冻结相关水体与关联批次；已冻结的对象不重复冻结。"""
    created: list[str] = []

    def freeze_one(scope_type: str, scope_ref: str) -> str | None:
        active = conn.execute(
            "SELECT freeze_ref FROM freeze WHERE scope_type=? AND scope_ref=? AND status='active'",
            (scope_type, scope_ref),
        ).fetchone()
        if active is not None:
            return None
        freeze_ref = _new_ref("FRZ")
        conn.execute(
            "INSERT INTO freeze(freeze_ref, scope_type, scope_ref, reason_inspection_ref,"
            " frozen_by, frozen_at, status) VALUES(?,?,?,?,?,?,'active')",
            (freeze_ref, scope_type, scope_ref, inspection_ref, actor_ref, occurred_at),
        )
        return freeze_ref

    pond_freeze = freeze_one("pond", pond_ref)
    if pond_freeze is not None:
        conn.execute("UPDATE pond SET status='frozen' WHERE pond_ref=?", (pond_ref,))
        created.append(pond_freeze)
    for batch_ref in batch_refs:
        batch_freeze = freeze_one("batch", batch_ref)
        if batch_freeze is None:
            continue
        conn.execute(
            "UPDATE harvest_batch SET status='frozen' WHERE batch_ref=?", (batch_ref,)
        )
        conn.execute(
            "UPDATE grade_lot SET status='frozen' WHERE batch_ref=? AND status='available'",
            (batch_ref,),
        )
        created.append(batch_freeze)
    return created


def record_inspection(
    conn: sqlite3.Connection,
    *,
    inspection_ref: str,
    scope_type: str,
    scope_ref: str,
    round: int,
    result: str,
    inspector_ref: str,
    occurred_at: str,
    method_ref: str | None = None,
    digest: str | None = None,
) -> dict[str, Any]:
    """登记抽检。不合格时只冻结相关水体与关联批次，并返回冻结编号。"""
    _require_iso8601(occurred_at, "occurred_at")
    if digest is not None:
        _require_digest(digest, "digest")
    if not isinstance(round, int) or not 1 <= round <= 5:
        raise DomainError("invalid_round", "round 必须在 1..5 之间（五重检测）")
    if result not in ("pass", "fail"):
        raise DomainError("invalid_result", "result 必须是 pass 或 fail")
    with _tx(conn):
        pond, batch_refs = _resolve_scope(conn, scope_type, scope_ref)
        conn.execute(
            "INSERT INTO inspection(inspection_ref, scope_type, scope_ref, round, result,"
            " method_ref, inspector_ref, occurred_at, digest) VALUES(?,?,?,?,?,?,?,?,?)",
            (inspection_ref, scope_type, scope_ref, round, result,
             method_ref, inspector_ref, occurred_at, digest),
        )
        freezes: list[str] = []
        if result == "fail":
            freezes = _freeze_scope(
                conn, pond["pond_ref"], batch_refs, inspection_ref, inspector_ref, occurred_at
            )
        _record_event(conn, scope_ref, "inspection.recorded", occurred_at,
                      {"inspection_ref": inspection_ref, "result": result,
                       "round": round, "freezes": freezes})
    return {"inspection_ref": inspection_ref, "freezes": freezes}


def record_freeze_action(
    conn: sqlite3.Connection,
    *,
    freeze_ref: str,
    action: str,
    actor_ref: str,
    role: str,
    occurred_at: str,
    result: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """冻结处置签署：复检（检测员）、解封（质量经理）、销毁（销毁专员）。

    解封必须先有检测员签署的合格复检；销毁仅适用于批次冻结。
    """
    required_role = FREEZE_ACTION_ROLES.get(action)
    if required_role is None:
        raise DomainError("invalid_action", "action 必须是 retest / release / destroy")
    if role != required_role:
        raise DomainError(
            "forbidden_role", f"{action} 必须由角色 {required_role} 签署，当前为 {role}", 403
        )
    _require_iso8601(occurred_at, "occurred_at")
    if action == "retest" and result not in ("pass", "fail"):
        raise DomainError("invalid_result", "复检必须给出 pass 或 fail 结果")
    with _tx(conn):
        freeze = _get(
            conn, "SELECT * FROM freeze WHERE freeze_ref=?", (freeze_ref,), "冻结记录不存在"
        )
        if freeze["status"] != "active":
            raise DomainError("freeze_closed", "冻结记录已关闭，不能继续处置", 409)
        if action == "release":
            passed = conn.execute(
                "SELECT 1 FROM freeze_action WHERE freeze_ref=? AND action='retest'"
                " AND result='pass'",
                (freeze_ref,),
            ).fetchone()
            if passed is None:
                raise DomainError("retest_required", "解封前必须由检测员签署合格复检", 409)
        if action == "destroy" and freeze["scope_type"] != "batch":
            raise DomainError("invalid_scope", "销毁仅适用于批次冻结，水体请走复检解封流程", 409)
        action_ref = _new_ref("ACT")
        conn.execute(
            "INSERT INTO freeze_action(action_ref, freeze_ref, action, result, actor_ref, role,"
            " occurred_at, note) VALUES(?,?,?,?,?,?,?,?)",
            (action_ref, freeze_ref, action, result, actor_ref, role, occurred_at, note),
        )
        if action == "release":
            _apply_release(conn, freeze)
            conn.execute(
                "UPDATE freeze SET status='released' WHERE freeze_ref=?", (freeze_ref,)
            )
        elif action == "destroy":
            _apply_destroy(conn, freeze)
            conn.execute(
                "UPDATE freeze SET status='destroyed' WHERE freeze_ref=?", (freeze_ref,)
            )
        _record_event(conn, freeze["scope_ref"], f"freeze.{action}", occurred_at,
                      {"freeze_ref": freeze_ref, "actor_ref": actor_ref,
                       "role": role, "result": result})
    return {"action_ref": action_ref}


def _apply_release(conn: sqlite3.Connection, freeze: sqlite3.Row) -> None:
    if freeze["scope_type"] == "pond":
        conn.execute(
            "UPDATE pond SET status='active' WHERE pond_ref=?", (freeze["scope_ref"],)
        )
        return
    batch = _get(
        conn, "SELECT * FROM harvest_batch WHERE batch_ref=?",
        (freeze["scope_ref"],), "捕捞批次不存在",
    )
    restored = "graded" if batch["graded_weight_g"] > 0 else "open"
    conn.execute(
        "UPDATE harvest_batch SET status=? WHERE batch_ref=? AND status='frozen'",
        (restored, batch["batch_ref"]),
    )
    conn.execute(
        "UPDATE grade_lot SET status='available' WHERE batch_ref=? AND status='frozen'",
        (batch["batch_ref"],),
    )


def _apply_destroy(conn: sqlite3.Connection, freeze: sqlite3.Row) -> None:
    batch_ref = freeze["scope_ref"]
    conn.execute(
        "UPDATE grade_lot SET status='destroyed', available_g=0"
        " WHERE batch_ref=? AND status='frozen'",
        (batch_ref,),
    )
    conn.execute(
        "UPDATE harvest_batch SET status='destroyed' WHERE batch_ref=? AND status='frozen'",
        (batch_ref,),
    )


# ---------------------------------------------------------------------------
# 出口采购：订单、分配、交付回执
# ---------------------------------------------------------------------------


def create_export_order(
    conn: sqlite3.Connection,
    *,
    order_ref: str,
    buyer_ref: str,
    containers: int,
    total_quantity_g: int,
    quality_grades: list[str],
) -> dict[str, Any]:
    _require_positive_int(containers, "containers")
    _require_positive_int(total_quantity_g, "total_quantity_g")
    if not quality_grades or not all(isinstance(g, str) and g for g in quality_grades):
        raise DomainError("invalid_quality_terms", "quality_grades 必须是非空等级列表")
    with _tx(conn):
        conn.execute(
            "INSERT INTO export_order(order_ref, buyer_ref, containers, total_quantity_g,"
            " allocated_g, quality_grades, status, created_at) VALUES(?,?,?,?,0,?,'open',?)",
            (order_ref, buyer_ref, containers, total_quantity_g,
             json.dumps(sorted(quality_grades), ensure_ascii=False), _now()),
        )
        _record_event(conn, order_ref, "export_order.created", _now(),
                      {"buyer_ref": buyer_ref, "containers": containers,
                       "total_quantity_g": total_quantity_g})
    return {"order_ref": order_ref}


def allocate_to_order(
    conn: sqlite3.Connection,
    *,
    alloc_ref: str,
    order_ref: str,
    lot_ref: str,
    quantity_g: int,
    actor_ref: str,
    allocated_at: str,
) -> dict[str, Any]:
    """把分级批次分配给出口订单。

    在同一事务内先用条件更新扣减订单额度、再扣减批次可用量；
    任一条件不满足即整体回滚，保证并发场景不超卖、不超分。
    """
    _require_positive_int(quantity_g, "quantity_g")
    _require_iso8601(allocated_at, "allocated_at")
    with _tx(conn):
        order = _get(
            conn, "SELECT * FROM export_order WHERE order_ref=?", (order_ref,), "出口订单不存在"
        )
        if order["status"] not in ("open", "partial"):
            raise DomainError("order_closed", "订单已关闭，不能继续分配", 409)
        lot = _get(conn, "SELECT * FROM grade_lot WHERE lot_ref=?", (lot_ref,), "分级批次不存在")
        accepted = json.loads(order["quality_grades"])
        if lot["grade"] not in accepted:
            raise DomainError(
                "grade_not_accepted",
                f"批次等级 {lot['grade']} 不在订单质量条件 {accepted} 内",
                409,
            )
        cursor = conn.execute(
            "UPDATE export_order SET allocated_g = allocated_g + ?"
            " WHERE order_ref=? AND allocated_g + ? <= total_quantity_g",
            (quantity_g, order_ref, quantity_g),
        )
        if cursor.rowcount == 0:
            raise DomainError("order_overflow", "订单柜量额度不足，禁止超卖", 409)
        cursor = conn.execute(
            "UPDATE grade_lot SET available_g = available_g - ?"
            " WHERE lot_ref=? AND status='available' AND available_g >= ?",
            (quantity_g, lot_ref, quantity_g),
        )
        if cursor.rowcount == 0:
            raise DomainError("insufficient_stock", "批次可用量不足或已冻结", 409)
        conn.execute(
            "INSERT INTO export_allocation(alloc_ref, order_ref, lot_ref, quantity_g, status,"
            " allocated_at) VALUES(?,?,?,?,'allocated',?)",
            (alloc_ref, order_ref, lot_ref, quantity_g, allocated_at),
        )
        conn.execute(
            "UPDATE export_order SET status = CASE"
            " WHEN allocated_g >= total_quantity_g THEN 'fulfilled' ELSE 'partial' END"
            " WHERE order_ref=?",
            (order_ref,),
        )
        _record_event(conn, lot_ref, "allocation.created", allocated_at,
                      {"alloc_ref": alloc_ref, "order_ref": order_ref,
                       "quantity_g": quantity_g, "actor_ref": actor_ref})
    return {"alloc_ref": alloc_ref}


def record_delivery(
    conn: sqlite3.Connection,
    *,
    receipt_ref: str,
    alloc_ref: str,
    received_qty_g: int,
    received_at: str,
    receiver_ref: str,
    note: str | None = None,
) -> dict[str, Any]:
    """登记交付回执。实收数量是后续结算的重量依据。"""
    if not isinstance(received_qty_g, int) or received_qty_g < 0:
        raise DomainError("invalid_quantity", "received_qty_g 必须是非负整数")
    _require_iso8601(received_at, "received_at")
    with _tx(conn):
        alloc = _get(
            conn, "SELECT * FROM export_allocation WHERE alloc_ref=?",
            (alloc_ref,), "分配记录不存在",
        )
        if alloc["status"] != "allocated":
            raise DomainError("allocation_closed", "该分配已有交付回执或已取消", 409)
        if received_qty_g > alloc["quantity_g"]:
            raise DomainError("receipt_overflow", "实收数量不能超过分配数量", 409)
        conn.execute(
            "INSERT INTO delivery_receipt(receipt_ref, alloc_ref, received_qty_g, received_at,"
            " receiver_ref, note) VALUES(?,?,?,?,?,?)",
            (receipt_ref, alloc_ref, received_qty_g, received_at, receiver_ref, note),
        )
        conn.execute(
            "UPDATE export_allocation SET status='delivered' WHERE alloc_ref=?", (alloc_ref,)
        )
        _record_event(conn, alloc_ref, "delivery.received", received_at,
                      {"receipt_ref": receipt_ref, "received_qty_g": received_qty_g,
                       "receiver_ref": receiver_ref})
    return {"receipt_ref": receipt_ref}


# ---------------------------------------------------------------------------
# 农户结算与退货冲正
# ---------------------------------------------------------------------------


def _price_for(
    conn: sqlite3.Connection, farmer_ref: str, grade: str, delivered_at: str
) -> sqlite3.Row:
    """按交付时间匹配生效的合同版本，取该等级的单价与帮扶加价。"""
    contract = conn.execute(
        "SELECT contract_ref, version FROM contract"
        " WHERE farmer_ref=? AND effective_from<=? AND effective_to>=?"
        " ORDER BY version DESC LIMIT 1",
        (farmer_ref, delivered_at, delivered_at),
    ).fetchone()
    if contract is None:
        raise DomainError(
            "contract_missing", f"交付时间 {delivered_at} 不在任何合同版本有效期内", 409
        )
    price = conn.execute(
        "SELECT * FROM contract_price WHERE contract_ref=? AND version=? AND grade=?",
        (contract["contract_ref"], contract["version"], grade),
    ).fetchone()
    if price is None:
        raise DomainError(
            "price_missing",
            f"合同 {contract['contract_ref']} v{contract['version']} 缺少等级 {grade} 的价格",
            409,
        )
    return price


def _amount_cents(weight_g: int, unit_price_cents: int, assist_cents: int) -> int:
    """金额 = 重量(克) × (单价 + 帮扶加价)(分/公斤) ÷ 1000，四舍五入到分。"""
    return (weight_g * (unit_price_cents + assist_cents) + 500) // 1000


def generate_settlement(
    conn: sqlite3.Connection,
    *,
    settlement_ref: str,
    farmer_ref: str,
    period: str,
    actor_ref: str,
) -> dict[str, Any]:
    """按已交付未结算的回执生成结算单：合同版本 × 等级 × 实收重量 × (单价+帮扶加价)。"""
    with _tx(conn):
        _get(conn, "SELECT farmer_ref FROM farmer WHERE farmer_ref=?", (farmer_ref,), "养殖户不存在")
        rows = conn.execute(
            "SELECT ea.alloc_ref, gl.grade, dr.received_qty_g, dr.received_at"
            " FROM export_allocation ea"
            " JOIN delivery_receipt dr ON dr.alloc_ref = ea.alloc_ref"
            " JOIN grade_lot gl ON gl.lot_ref = ea.lot_ref"
            " JOIN harvest_batch hb ON hb.batch_ref = gl.batch_ref"
            " JOIN pond p ON p.pond_ref = hb.pond_ref"
            " WHERE p.farmer_ref=? AND ea.status='delivered' AND ea.settled_ref IS NULL"
            " ORDER BY dr.received_at, ea.alloc_ref",
            (farmer_ref,),
        ).fetchall()
        if not rows:
            raise DomainError("nothing_to_settle", "没有已交付且未结算的回执", 409)
        created_at = _now()
        lines: list[dict[str, Any]] = []
        total = 0
        contract_key: tuple[str, int] | None = None
        for row in rows:
            price = _price_for(conn, farmer_ref, row["grade"], row["received_at"])
            key = (price["contract_ref"], price["version"])
            if contract_key is None:
                contract_key = key
            elif contract_key != key:
                raise DomainError(
                    "mixed_contract_versions",
                    "同一结算期内存在多个生效合同版本，请按版本分别结算",
                    409,
                )
            amount = _amount_cents(
                row["received_qty_g"], price["unit_price_cents"], price["assist_cents"]
            )
            total += amount
            lines.append({
                "line_ref": _new_ref("LN"),
                "alloc_ref": row["alloc_ref"],
                "grade": row["grade"],
                "weight_g": row["received_qty_g"],
                "unit_price_cents": price["unit_price_cents"],
                "assist_cents": price["assist_cents"],
                "amount_cents": amount,
            })
        assert contract_key is not None
        conn.execute(
            "INSERT INTO settlement(settlement_ref, farmer_ref, contract_ref, contract_version,"
            " period, total_cents, created_at) VALUES(?,?,?,?,?,?,?)",
            (settlement_ref, farmer_ref, contract_key[0], contract_key[1],
             period, total, created_at),
        )
        for line in lines:
            conn.execute(
                "INSERT INTO settlement_line(line_ref, settlement_ref, kind, alloc_ref, grade,"
                " weight_g, unit_price_cents, assist_cents, amount_cents, created_at)"
                " VALUES(?,?, 'earning', ?,?,?,?,?,?,?)",
                (line["line_ref"], settlement_ref, line["alloc_ref"], line["grade"],
                 line["weight_g"], line["unit_price_cents"], line["assist_cents"],
                 line["amount_cents"], created_at),
            )
            conn.execute(
                "UPDATE export_allocation SET settled_ref=? WHERE alloc_ref=?",
                (settlement_ref, line["alloc_ref"]),
            )
        _record_event(conn, settlement_ref, "settlement.generated", created_at,
                      {"farmer_ref": farmer_ref, "period": period,
                       "total_cents": total, "actor_ref": actor_ref})
    return {"settlement_ref": settlement_ref, "total_cents": total,
            "lines": [line["line_ref"] for line in lines]}


def record_return(
    conn: sqlite3.Connection,
    *,
    line_ref: str,
    original_line_ref: str,
    weight_g: int,
    reason: str,
    actor_ref: str,
    occurred_at: str,
) -> dict[str, Any]:
    """退货冲正：新增负金额冲正行指向原结算行，原行与原结算单金额保持不变。"""
    _require_positive_int(weight_g, "weight_g")
    _require_iso8601(occurred_at, "occurred_at")
    with _tx(conn):
        original = _get(
            conn, "SELECT * FROM settlement_line WHERE line_ref=?",
            (original_line_ref,), "原结算行不存在",
        )
        if original["kind"] != "earning":
            raise DomainError("invalid_reversal_target", "只能对应收行发起退货冲正", 409)
        reversed_row = conn.execute(
            "SELECT COALESCE(SUM(weight_g), 0) AS w FROM settlement_line"
            " WHERE reverses_line_ref=?",
            (original_line_ref,),
        ).fetchone()
        if reversed_row["w"] + weight_g > original["weight_g"]:
            raise DomainError("return_exceeds", "累计退货重量超过原结算重量", 409)
        amount = -_amount_cents(weight_g, original["unit_price_cents"], original["assist_cents"])
        conn.execute(
            "INSERT INTO settlement_line(line_ref, settlement_ref, kind, alloc_ref, grade,"
            " weight_g, unit_price_cents, assist_cents, amount_cents, reverses_line_ref,"
            " reason, created_at) VALUES(?,?, 'reversal', ?,?,?,?,?,?,?,?,?)",
            (line_ref, original["settlement_ref"], original["alloc_ref"], original["grade"],
             weight_g, original["unit_price_cents"], original["assist_cents"],
             amount, original_line_ref, reason, _now()),
        )
        _record_event(conn, original["settlement_ref"], "settlement.reversed", occurred_at,
                      {"line_ref": line_ref, "original_line_ref": original_line_ref,
                       "weight_g": weight_g, "amount_cents": amount,
                       "reason": reason, "actor_ref": actor_ref})
    return {"line_ref": line_ref, "amount_cents": amount}


# ---------------------------------------------------------------------------
# 查询：样品追溯与结算解释
# ---------------------------------------------------------------------------


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def trace_subject(conn: sqlite3.Connection, subject_ref: str) -> dict[str, Any]:
    """从市场抽检样品（分级批次/捕捞批次/分配/回执/塘口编号）回溯全链路。

    回答：样品来自哪片塘、哪个养殖户、哪批苗种、接受过哪些投入与检测、流向何处。
    """
    lot = conn.execute(
        "SELECT * FROM grade_lot WHERE lot_ref=?", (subject_ref,)
    ).fetchone()
    batch = None
    pond_ref = None
    if lot is not None:
        batch = _get(conn, "SELECT * FROM harvest_batch WHERE batch_ref=?",
                     (lot["batch_ref"],), "捕捞批次不存在")
    if batch is None:
        batch = conn.execute(
            "SELECT * FROM harvest_batch WHERE batch_ref=?", (subject_ref,)
        ).fetchone()
    if batch is None:
        alloc = conn.execute(
            "SELECT * FROM export_allocation WHERE alloc_ref=?", (subject_ref,)
        ).fetchone()
        if alloc is not None:
            lot = conn.execute(
                "SELECT * FROM grade_lot WHERE lot_ref=?", (alloc["lot_ref"],)
            ).fetchone()
            batch = _get(conn, "SELECT * FROM harvest_batch WHERE batch_ref=?",
                         (lot["batch_ref"],), "捕捞批次不存在")
    if batch is None:
        receipt = conn.execute(
            "SELECT * FROM delivery_receipt WHERE receipt_ref=?", (subject_ref,)
        ).fetchone()
        if receipt is not None:
            alloc = _get(conn, "SELECT * FROM export_allocation WHERE alloc_ref=?",
                         (receipt["alloc_ref"],), "分配记录不存在")
            lot = conn.execute(
                "SELECT * FROM grade_lot WHERE lot_ref=?", (alloc["lot_ref"],)
            ).fetchone()
            batch = _get(conn, "SELECT * FROM harvest_batch WHERE batch_ref=?",
                         (lot["batch_ref"],), "捕捞批次不存在")
    if batch is not None:
        pond_ref = batch["pond_ref"]
    else:
        pond = conn.execute(
            "SELECT * FROM pond WHERE pond_ref=?", (subject_ref,)
        ).fetchone()
        if pond is None:
            raise DomainError("not_found", "无法识别的样品编号", 404)
        pond_ref = pond["pond_ref"]

    pond = _get(conn, "SELECT * FROM pond WHERE pond_ref=?", (pond_ref,), "塘口不存在")
    farmer = _get(conn, "SELECT * FROM farmer WHERE farmer_ref=?",
                  (pond["farmer_ref"],), "养殖户不存在")

    seedling = None
    if batch is not None and batch["seed_batch_ref"] is not None:
        seedling = dict(_get(conn, "SELECT * FROM seedling_batch WHERE seed_batch_ref=?",
                             (batch["seed_batch_ref"],), "苗种批次不存在"))

    lots = _rows(conn, "SELECT * FROM grade_lot WHERE batch_ref=?", (batch["batch_ref"],)) \
        if batch is not None else []
    scope_refs = [pond_ref] + ([batch["batch_ref"]] if batch is not None else []) \
        + [l["lot_ref"] for l in lots]
    placeholders = ",".join("?" for _ in scope_refs)
    inspections = _rows(
        conn,
        f"SELECT * FROM inspection WHERE scope_ref IN ({placeholders}) ORDER BY occurred_at",
        tuple(scope_refs),
    )
    freezes = _rows(
        conn,
        f"SELECT * FROM freeze WHERE scope_ref IN ({placeholders}) ORDER BY frozen_at",
        tuple(scope_refs),
    )
    for freeze in freezes:
        freeze["actions"] = _rows(
            conn, "SELECT * FROM freeze_action WHERE freeze_ref=? ORDER BY occurred_at",
            (freeze["freeze_ref"],),
        )
    flows = _rows(
        conn,
        "SELECT ea.alloc_ref, ea.order_ref, ea.lot_ref, ea.quantity_g, ea.status,"
        " ea.allocated_at, dr.receipt_ref, dr.received_qty_g, dr.received_at, dr.receiver_ref"
        " FROM export_allocation ea"
        " LEFT JOIN delivery_receipt dr ON dr.alloc_ref = ea.alloc_ref"
        " WHERE ea.lot_ref IN (SELECT lot_ref FROM grade_lot WHERE batch_ref=?)"
        " ORDER BY ea.allocated_at",
        (batch["batch_ref"],),
    ) if batch is not None else []

    return {
        "subject_ref": subject_ref,
        "pond": dict(pond),
        "farmer": dict(farmer),
        "seedling_batch": seedling,
        "harvest_batch": dict(batch) if batch is not None else None,
        "grade_lots": lots,
        "stockings": _rows(
            conn, "SELECT * FROM stocking WHERE pond_ref=? ORDER BY occurred_at", (pond_ref,)
        ),
        "inputs": _rows(
            conn, "SELECT * FROM input_record WHERE pond_ref=? ORDER BY occurred_at", (pond_ref,)
        ),
        "guidance": _rows(
            conn, "SELECT * FROM guidance WHERE pond_ref=? ORDER BY occurred_at", (pond_ref,)
        ),
        "inspections": inspections,
        "freezes": freezes,
        "flows": flows,
    }


def explain_settlement(conn: sqlite3.Connection, settlement_ref: str) -> dict[str, Any]:
    """解释每笔结算为何得到该金额：等级、重量、单价、帮扶加价、冲正与净额。"""
    settlement = _get(
        conn, "SELECT * FROM settlement WHERE settlement_ref=?",
        (settlement_ref,), "结算单不存在",
    )
    lines = _rows(
        conn,
        "SELECT * FROM settlement_line WHERE settlement_ref=? ORDER BY created_at, line_ref",
        (settlement_ref,),
    )
    for line in lines:
        line["formula"] = (
            f"{line['weight_g']}克 × ({line['unit_price_cents']}+{line['assist_cents']})"
            f"分/公斤 ÷ 1000 = {abs(line['amount_cents'])}分"
        )
    reversal_total = sum(l["amount_cents"] for l in lines if l["kind"] == "reversal")
    return {
        "settlement": dict(settlement),
        "lines": lines,
        "reversal_total_cents": reversal_total,
        "net_total_cents": settlement["total_cents"] + reversal_total,
    }
