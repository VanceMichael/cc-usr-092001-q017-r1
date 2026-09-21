"""追溯与一致性核对。

正向：塘口 -> 起捕 -> 分级 -> 拆分 -> 分配 -> 发运 -> 回执/退货。
反向：市场样品/批次 -> 逐级父批 -> 起捕 -> 塘口 -> 投入/指导/检测/认证。
"""

import sqlite3

from .common import get_or_404


def _batch_chain_up(conn, batch_id: str) -> list[dict]:
    """沿 parent_batch_id 上溯到起捕批。"""
    chain: list[dict] = []
    current = batch_id
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        b = get_or_404(conn, "batch", "batch_id", current)
        chain.append(dict(b))
        current = b["parent_batch_id"]
    return chain


def _pond_timeline(conn, pond_id: str) -> dict:
    def q(sql: str, params=()):
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

    return {
        "stockings": q(
            "SELECT s.*, sl.species, sl.supplier_ref, sl.cert_ref "
            "FROM stocking s JOIN seed_lot sl ON sl.seed_lot_id=s.seed_lot_id "
            "WHERE pond_id=? ORDER BY stocked_at", (pond_id,)),
        "applications": q(
            "SELECT a.*, m.name AS material_name, m.category, m.organic_allowed "
            "FROM input_application a JOIN material m "
            "ON m.material_code=a.material_code WHERE pond_id=? ORDER BY applied_at",
            (pond_id,)),
        "guidance": q(
            "SELECT * FROM guidance WHERE pond_id=? ORDER BY occurred_at",
            (pond_id,)),
        "water_inspections": q(
            "SELECT i.* FROM inspection i JOIN pond p ON p.pond_id=? "
            "WHERE (i.subject_type='POND' AND i.subject_id=p.pond_id) "
            "OR (i.subject_type='WATER_BODY' AND i.subject_id=p.water_body_id) "
            "ORDER BY i.sampled_at", (pond_id,)),
        "certifications": q(
            "SELECT c.* FROM certification c JOIN pond p ON p.pond_id=? "
            "WHERE (c.subject_type='POND' AND c.subject_id=p.pond_id) "
            "OR (c.subject_type='WATER_BODY' AND c.subject_id=p.water_body_id) "
            "ORDER BY c.valid_from", (pond_id,)),
    }


def trace_sample(conn, sample_id: str) -> dict:
    """回答：一只市场抽检样品来自哪片塘、接受过哪些投入和检测。"""
    sample = get_or_404(conn, "market_sample", "sample_id", sample_id)
    return trace_batch(conn, sample["batch_id"], sample_id=sample_id)


def trace_batch(conn, batch_id: str, sample_id: str | None = None) -> dict:
    chain = _batch_chain_up(conn, batch_id)
    root_harvest_id = chain[-1]["root_harvest_id"]
    harvest = get_or_404(conn, "harvest_batch", "harvest_id",
                         root_harvest_id)
    pond = get_or_404(conn, "pond", "pond_id", harvest["pond_id"])
    water_body = get_or_404(conn, "water_body", "water_body_id",
                            pond["water_body_id"])
    farmer = get_or_404(conn, "farmer", "farmer_id", harvest["farmer_id"])

    # 该起捕批全树（含退货回流批）。
    tree = [dict(r) for r in conn.execute(
        "SELECT batch_id, parent_batch_id, origin_batch_id, kind, grade, "
        "qty_kg, allocated_kg, shipped_kg, destroyed_kg, split_out_kg, status "
        "FROM batch WHERE root_harvest_id=? ORDER BY created_at",
        (root_harvest_id,)).fetchall()]

    product_tests = [dict(r) for r in conn.execute(
        "SELECT * FROM inspection WHERE subject_type='BATCH' AND subject_id IN "
        f"({','.join('?' * len(tree))}) ORDER BY sampled_at",
        [t["batch_id"] for t in tree]).fetchall()] if tree else []
    export_tests = [dict(r) for r in conn.execute(
        "SELECT * FROM inspection WHERE stage='EXPORT' ORDER BY sampled_at")]

    # 流向：分配/发运/回执/退货。
    flows = [dict(r) for r in conn.execute(
        "SELECT ba.allocation_id, ba.container_id, ba.batch_id, ba.qty_kg, "
        "ba.shipped_kg, ba.status AS alloc_status, c.seq, o.order_id, "
        "o.customer_ref, dr.receipt_id, dr.delivered_kg, dr.delivered_at "
        "FROM batch_allocation ba "
        "JOIN order_container c ON c.container_id=ba.container_id "
        "JOIN export_order o ON o.order_id=c.order_id "
        "LEFT JOIN delivery_receipt dr ON dr.container_id=c.container_id "
        f"WHERE ba.batch_id IN ({','.join('?' * len(tree))}) "
        "ORDER BY ba.created_at", [t["batch_id"] for t in tree]).fetchall()] \
        if tree else []
    returns = [dict(r) for r in conn.execute(
        "SELECT sr.* FROM sales_return sr "
        "WHERE sr.batch_id IN (" + ",".join("?" * len(tree)) + ") "
        "OR sr.batch_id IN (SELECT batch_id FROM batch WHERE origin_batch_id IN ("
        + ",".join("?" * len(tree)) + "))",
        [t["batch_id"] for t in tree] + [t["batch_id"] for t in tree]).fetchall()] \
        if tree else []

    sample = None
    if sample_id:
        sample = dict(get_or_404(conn, "market_sample", "sample_id", sample_id))

    return {
        "sample": sample,
        "queried_batch_id": batch_id,
        "lineage": [{"batch_id": b["batch_id"], "kind": b["kind"],
                     "grade": b["grade"], "qty_kg": b["qty_kg"],
                     "parent_batch_id": b["parent_batch_id"]} for b in chain],
        "origin": {
            "harvest_id": root_harvest_id,
            "caught_at": harvest["caught_at"],
            "qty_kg": harvest["qty_kg"],
            "pond": {"pond_id": pond["pond_id"], "code": pond["code"],
                     "area_mu": pond["area_mu"]},
            "water_body": {"water_body_id": water_body["water_body_id"],
                           "name": water_body["name"]},
            "farmer": {"farmer_id": farmer["farmer_id"], "name": farmer["name"]},
            "seed_lot_id": harvest["seed_lot_id"],
        },
        "pond_records": _pond_timeline(conn, pond["pond_id"]),
        "product_inspections": product_tests,
        "batch_tree": tree,
        "flows": flows,
        "returns": returns,
    }


def consistency_report(conn) -> dict:
    """全链一致性核对，任何一项不为零都会让高价收购失去依据。"""
    issues: list[dict] = []

    # 1. 每个拆分组：父批拆出 = 子批重量之和。
    for g in conn.execute(
            "SELECT sg.split_group_id, sg.parent_batch_id, sg.consumed_kg, "
            "COALESCE(SUM(c.qty_kg),0) AS children_kg FROM split_group sg "
            "LEFT JOIN batch c ON c.split_group_id=sg.split_group_id "
            "GROUP BY sg.split_group_id").fetchall():
        if abs(g["consumed_kg"] - g["children_kg"]) > 1e-6:
            issues.append({"code": "SPLIT_NOT_BALANCED",
                           "ref": g["split_group_id"],
                           "detail": f"拆出 {g['consumed_kg']} != 子批合计 "
                                     f"{g['children_kg']}"})

    # 2. 批次：qty >= 已分+已装+销毁+拆出。
    for b in conn.execute(
            "SELECT * FROM batch WHERE qty_kg + 1e-6 < allocated_kg + shipped_kg "
            "+ destroyed_kg + split_out_kg").fetchall():
        issues.append({"code": "BATCH_OVERCOMMITTED", "ref": b["batch_id"],
                       "detail": "占用数量超过批次总量"})

    # 3. 分级重量不超过起捕重量。
    for h in conn.execute(
            "SELECT h.harvest_id, h.qty_kg AS harvest_kg, "
            "COALESCE(SUM(g.qty_kg),0) AS graded_kg FROM harvest_batch h "
            "LEFT JOIN grade_record g ON g.harvest_id=h.harvest_id "
            "GROUP BY h.harvest_id HAVING graded_kg > harvest_kg + 1e-6").fetchall():
        issues.append({"code": "GRADE_OVER_HARVEST", "ref": h["harvest_id"],
                       "detail": f"分级 {h['graded_kg']} > 起捕 "
                                 f"{h['harvest_kg']}"})

    # 4. 柜：已分配+已装不超过目标（退货释放容量后允许超装被退回部分）。
    for c in conn.execute(
            "SELECT * FROM order_container WHERE allocated_kg + shipped_kg "
            "> target_kg + returned_kg + 1e-6").fetchall():
        issues.append({"code": "CONTAINER_OVER_TARGET",
                       "ref": c["container_id"],
                       "detail": "柜装载超过柜量目标"})

    # 5. 台账：NORMAL 与 REVERSAL 不得超出原行重量。
    for e in conn.execute(
            "SELECT l.line_id, l.qty_kg AS line_kg, "
            "COALESCE(SUM(CASE WHEN e.direction='REVERSAL' THEN -e.qty_kg END),0) "
            "AS rev_kg FROM settlement_line l LEFT JOIN ledger_entry e "
            "ON e.line_id=l.line_id GROUP BY l.line_id HAVING rev_kg > line_kg+1e-6"
            ).fetchall():
        issues.append({"code": "LEDGER_OVER_REVERSED", "ref": e["line_id"],
                       "detail": "冲正重量超过结算重量"})

    # 6. 库存移动逐批求和应与批次账面（现存可支配口径）一致。
    movement_issues = conn.execute(
        "SELECT b.batch_id, b.qty_kg, b.allocated_kg, b.shipped_kg, "
        "b.destroyed_kg, b.split_out_kg, "
        "COALESCE(SUM(m.delta_kg),0) AS move_sum FROM batch b "
        "LEFT JOIN stock_movement m ON m.batch_id=b.batch_id "
        "GROUP BY b.batch_id "
        "HAVING ABS(move_sum - (b.qty_kg - b.allocated_kg - b.shipped_kg "
        "- b.destroyed_kg - b.split_out_kg)) > 1e-6").fetchall()
    for m in movement_issues:
        issues.append({"code": "MOVEMENT_MISMATCH", "ref": m["batch_id"],
                       "detail": "库存移动累计与批次账面不一致"})

    # 7. 冻结单与批次状态一致性（只检查仍有在库产品的批次）。
    for b in conn.execute(
            "SELECT b.batch_id, b.qty_kg, b.allocated_kg, b.shipped_kg, "
            "b.destroyed_kg, b.split_out_kg FROM batch b JOIN freeze_target ft "
            "ON ft.target_type='BATCH' AND ft.target_id=b.batch_id "
            "JOIN freeze_order fo ON fo.freeze_id=ft.freeze_id "
            "WHERE fo.status='FROZEN' AND b.status NOT IN ('FROZEN','DESTROYED') "
            "AND b.qty_kg - b.allocated_kg - b.shipped_kg - b.destroyed_kg "
            "- b.split_out_kg > 1e-6").fetchall():
        issues.append({"code": "FREEZE_STATUS_DRIFT", "ref": b["batch_id"],
                       "detail": "批次被冻结单覆盖但状态不是 FROZEN"})

    return {"ok": not issues, "issue_count": len(issues), "issues": issues}
