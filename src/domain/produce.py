"""生产：起捕、分级、批次拆分与市场样品。批次数量全程守恒。"""

import sqlite3

from .common import (Conflict, DomainError, QTY_EPS, get_or_404, now_iso,
                     record_event, require)
from .quality import batch_is_frozen, water_body_is_frozen


def available_kg(row: sqlite3.Row) -> float:
    """可用于新分配的数量：总量扣除已装、销毁、拆出与在持分配。"""
    return (row["qty_kg"] - row["allocated_kg"] - row["shipped_kg"]
            - row["destroyed_kg"] - row["split_out_kg"])


def _assert_active_cert(conn, harvest) -> None:
    pond = get_or_404(conn, "pond", "pond_id", harvest["pond_id"])
    wb = get_or_404(conn, "water_body", "water_body_id", pond["water_body_id"])
    for subject_type, subject_id in (
        ("WATER_BODY", wb["water_body_id"]),
        ("POND", pond["pond_id"]),
    ):
        row = conn.execute(
            "SELECT 1 FROM certification WHERE subject_type=? AND subject_id=? "
            "AND status='ACTIVE' AND valid_from<=? AND valid_to>=?",
            (subject_type, subject_id, harvest["caught_at"],
             harvest["caught_at"]),
        ).fetchone()
        if row:
            return
    raise DomainError("起捕时塘口或所属水体无有效有机认证，不能进入高价收购", 422)


def harvest(conn, payload: dict) -> dict:
    require(payload, ["harvest_id", "pond_id", "farmer_id", "qty_kg",
                      "caught_at"])
    pond = get_or_404(conn, "pond", "pond_id", payload["pond_id"])
    farmer = get_or_404(conn, "farmer", "farmer_id", payload["farmer_id"])
    if pond["farmer_id"] != farmer["farmer_id"]:
        raise DomainError("起捕登记的养殖户与塘口承包户不一致", 422)
    if water_body_is_frozen(conn, pond["water_body_id"]):
        raise DomainError("所属水体处于冻结状态，暂停起捕", 422)
    _assert_active_cert(conn, payload)

    seed_row = conn.execute(
        "SELECT seed_lot_id FROM stocking WHERE pond_id=? "
        "ORDER BY stocked_at DESC LIMIT 1", (payload["pond_id"],)
    ).fetchone()
    ts = now_iso()
    try:
        conn.execute(
            "INSERT INTO harvest_batch(harvest_id, pond_id, seed_lot_id, "
            "farmer_id, qty_kg, caught_at, method) VALUES (?,?,?,?,?,?,?)",
            (payload["harvest_id"], payload["pond_id"],
             payload.get("seed_lot_id") or (seed_row["seed_lot_id"] if seed_row else None),
             payload["farmer_id"], payload["qty_kg"], payload["caught_at"],
             payload.get("method")))
        conn.execute(
            "INSERT INTO batch(batch_id, root_harvest_id, kind, qty_kg, "
            "created_at) VALUES (?,?,'HARVEST',?,?)",
            (f"B-{payload['harvest_id']}", payload["harvest_id"],
             payload["qty_kg"], ts))
        conn.execute(
            "INSERT INTO stock_movement(movement_id, batch_id, delta_kg, "
            "ref_type, ref_id, created_at) VALUES (?,?,?,'PRODUCE',?,?)",
            (f"MOV-PROD-{payload['harvest_id']}", f"B-{payload['harvest_id']}",
             payload["qty_kg"], payload["harvest_id"], ts))
    except sqlite3.IntegrityError as exc:
        raise Conflict(f"起捕批写入冲突：{exc}")
    record_event(conn, "produce", payload["harvest_id"], "harvest.create",
                 payload)
    return {"harvest_id": payload["harvest_id"],
            "batch_id": f"B-{payload['harvest_id']}"}


def grade(conn, payload: dict) -> dict:
    """分级即把起捕批按等级拆成子批；分级重量之和不得超过在库重量。"""
    require(payload, ["grade_id", "harvest_id", "lines"])
    lines = payload["lines"]
    if not isinstance(lines, list) or not lines:
        raise DomainError("分级明细不能为空", 400)
    total = 0.0
    for line in lines:
        require(line, ["grade", "qty_kg"])
        if line["qty_kg"] <= 0:
            raise DomainError("分级重量必须为正", 400)
        total += float(line["qty_kg"])

    harvest = get_or_404(conn, "harvest_batch", "harvest_id",
                         payload["harvest_id"])
    parent_id = f"B-{harvest['harvest_id']}"
    parent = get_or_404(conn, "batch", "batch_id", parent_id)
    if available_kg(parent) + QTY_EPS < total:
        raise DomainError(
            f"分级重量 {total} 超过起捕批可拆重量 {available_kg(parent)}", 422)
    if batch_is_frozen(conn, parent_id):
        raise DomainError("批次处于冻结状态，不能分级", 422)

    payload.setdefault("graded_at", now_iso())
    ts = now_iso()
    group_id = f"SG-{payload['grade_id']}"
    child_ids: list[str] = []
    try:
        conn.execute(
            "INSERT INTO split_group(split_group_id, parent_batch_id, "
            "consumed_kg, created_at) VALUES (?,?,?,?)",
            (group_id, parent_id, total, ts))
        conn.execute(
            "UPDATE batch SET split_out_kg=split_out_kg+?, "
            "status=CASE WHEN split_out_kg+? >= qty_kg THEN 'EXHAUSTED' "
            "ELSE status END WHERE batch_id=?",
            (total, total, parent_id))
        conn.execute(
            "INSERT INTO stock_movement(movement_id, batch_id, delta_kg, "
            "ref_type, ref_id, note, created_at) "
            "VALUES (?,?,?,'SPLIT_OUT',?,?,?)",
            (f"MOV-GOUT-{payload['grade_id']}", parent_id, -total, group_id,
             f"分级拆出 {total}kg", ts))
        for i, line in enumerate(lines, 1):
            child_id = f"B-{payload['grade_id']}-{i}"
            conn.execute(
                "INSERT INTO batch(batch_id, parent_batch_id, split_group_id, "
                "root_harvest_id, kind, grade, qty_kg, created_at) "
                "VALUES (?,?,?,?,'SPLIT',?,?,?)",
                (child_id, parent_id, group_id, harvest["harvest_id"],
                 line["grade"], line["qty_kg"], ts))
            conn.execute(
                "INSERT INTO grade_record(grade_id, harvest_id, grade, qty_kg, "
                "graded_at, graded_by) VALUES (?,?,?,?,?,?)",
                (f"GR-{payload['grade_id']}-{i}", payload["harvest_id"],
                 line["grade"], line["qty_kg"], payload["graded_at"],
                 payload.get("graded_by")))
            conn.execute(
                "INSERT INTO stock_movement(movement_id, batch_id, delta_kg, "
                "ref_type, ref_id, note, created_at) "
                "VALUES (?,?,?,'SPLIT_IN',?,?,?)",
                (f"MOV-GIN-{child_id}", child_id, line["qty_kg"], group_id,
                 f"分级形成 {line['grade']} 等品", ts))
            child_ids.append(child_id)
    except sqlite3.IntegrityError as exc:
        raise Conflict(f"分级写入冲突：{exc}")
    record_event(conn, "produce", payload["harvest_id"], "grade.create",
                 {"grade_id": payload["grade_id"],
                  "harvest_id": payload["harvest_id"], "lines": lines})
    return {"grade_id": payload["grade_id"], "batches": child_ids,
            "total_kg": total}


def split(conn, payload: dict) -> dict:
    """装运前拆分：一个父批拆为多个子批，子批重量之和必须等于拆出重量。"""
    require(payload, ["split_id", "parent_batch_id", "lines"])
    lines = payload["lines"]
    if not isinstance(lines, list) or not lines:
        raise DomainError("拆分明细不能为空", 400)
    total = 0.0
    for line in lines:
        require(line, ["qty_kg"])
        if line["qty_kg"] <= 0:
            raise DomainError("拆分重量必须为正", 400)
        total += float(line["qty_kg"])
    parent = get_or_404(conn, "batch", "batch_id", payload["parent_batch_id"])
    if batch_is_frozen(conn, payload["parent_batch_id"]):
        raise DomainError("批次处于冻结状态，不能拆分", 422)
    free = (parent["qty_kg"] - parent["allocated_kg"] - parent["shipped_kg"]
            - parent["destroyed_kg"] - parent["split_out_kg"])
    if abs(free - total) > QTY_EPS and free < total:
        raise DomainError(
            f"拆出重量 {total} 超过可拆重量 {free}", 422)

    ts = now_iso()
    group_id = f"SG-{payload['split_id']}"
    child_ids: list[str] = []
    try:
        conn.execute(
            "INSERT INTO split_group(split_group_id, parent_batch_id, "
            "consumed_kg, created_at) VALUES (?,?,?,?)",
            (group_id, payload["parent_batch_id"], total, ts))
        conn.execute(
            "UPDATE batch SET split_out_kg=split_out_kg+?, "
            "status=CASE WHEN split_out_kg+? >= qty_kg THEN 'EXHAUSTED' "
            "ELSE status END WHERE batch_id=?",
            (total, total, payload["parent_batch_id"]))
        conn.execute(
            "INSERT INTO stock_movement(movement_id, batch_id, delta_kg, "
            "ref_type, ref_id, note, created_at) "
            "VALUES (?,?,?,'SPLIT_OUT',?,?,?)",
            (f"MOV-SOUT-{payload['split_id']}", payload["parent_batch_id"],
             -total, group_id, f"装运拆分 {total}kg", ts))
        for i, line in enumerate(lines, 1):
            child_id = line.get("batch_id") or f"B-{payload['split_id']}-{i}"
            conn.execute(
                "INSERT INTO batch(batch_id, parent_batch_id, split_group_id, "
                "root_harvest_id, kind, grade, qty_kg, created_at) "
                "VALUES (?,?,?,?,'SPLIT',?,?,?)",
                (child_id, payload["parent_batch_id"], group_id,
                 parent["root_harvest_id"], line.get("grade") or parent["grade"],
                 line["qty_kg"], ts))
            conn.execute(
                "INSERT INTO stock_movement(movement_id, batch_id, delta_kg, "
                "ref_type, ref_id, note, created_at) "
                "VALUES (?,?,?,'SPLIT_IN',?,?,?)",
                (f"MOV-SIN-{child_id}", child_id, line["qty_kg"], group_id,
                 "拆分子批", ts))
            child_ids.append(child_id)
    except sqlite3.IntegrityError as exc:
        raise Conflict(f"拆分写入冲突：{exc}")
    record_event(conn, "produce", payload["parent_batch_id"], "batch.split",
                 {"split_id": payload["split_id"],
                  "parent_batch_id": payload["parent_batch_id"], "lines": lines})
    return {"split_id": payload["split_id"], "batches": child_ids,
            "total_kg": total}


def market_sample(conn, payload: dict) -> dict:
    """登记市场抽检样品，挂在具体批次上，供正向追溯使用。"""
    require(payload, ["sample_id", "batch_id", "market_ref", "sampled_at"])
    get_or_404(conn, "batch", "batch_id", payload["batch_id"])
    payload.setdefault("items_json", "[]")
    try:
        conn.execute(
            "INSERT INTO market_sample(sample_id, batch_id, market_ref, "
            "sampled_at, items_json, report_digest) VALUES (?,?,?,?,?,?)",
            (payload["sample_id"], payload["batch_id"], payload["market_ref"],
             payload["sampled_at"], payload["items_json"],
             payload.get("report_digest")))
    except sqlite3.IntegrityError:
        raise Conflict(f"样品 {payload['sample_id']} 已存在")
    record_event(conn, "produce", payload["sample_id"], "market_sample.register",
                 payload)
    return {"sample_id": payload["sample_id"],
            "batch_id": payload["batch_id"]}
