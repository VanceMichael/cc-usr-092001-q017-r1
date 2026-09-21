"""出口贸易：订单柜量、并发分配不超卖、装运、交付回执与退货入库。"""

import sqlite3

from .common import (Conflict, DomainError, Forbidden, QTY_EPS, get_or_404,
                     now_iso, record_event, require)
from .produce import available_kg
from .quality import batch_is_frozen


def _person_role(conn, person_id: str) -> str:
    return get_or_404(conn, "person", "person_id", person_id)["role"]


def create_order(conn, payload: dict) -> dict:
    require(payload, ["order_id", "customer_ref", "sales_id",
                      "grade_required", "containers_count", "kg_per_container"])
    if _person_role(conn, payload["sales_id"]) != "sales":
        raise Forbidden("出口订单只能由 sales 角色创建")
    payload.setdefault("conditions_json", "{}")
    payload.setdefault("created_at", now_iso())
    try:
        conn.execute(
            "INSERT INTO export_order(order_id, customer_ref, sales_id, "
            "grade_required, conditions_json, containers_count, "
            "kg_per_container, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,'OPEN',?)",
            (payload["order_id"], payload["customer_ref"], payload["sales_id"],
             payload["grade_required"], payload["conditions_json"],
             payload["containers_count"], payload["kg_per_container"],
             payload["created_at"]))
        for seq in range(1, int(payload["containers_count"]) + 1):
            conn.execute(
                "INSERT INTO order_container(container_id, order_id, seq, "
                "target_kg) VALUES (?,?,?,?)",
                (f"C-{payload['order_id']}-{seq}", payload["order_id"], seq,
                 payload["kg_per_container"]))
    except sqlite3.IntegrityError as exc:
        raise Conflict(f"订单写入冲突：{exc}")
    record_event(conn, "trading", payload["order_id"], "order.create", payload)
    return {"order_id": payload["order_id"],
            "containers": [f"C-{payload['order_id']}-{s}"
                           for s in range(1, int(payload["containers_count"]) + 1)]}


def _get_container(conn, container_id: str) -> sqlite3.Row:
    return get_or_404(conn, "order_container", "container_id", container_id)


def _conditions_met(conn, conditions: dict, batch_id: str) -> None:
    """按订单质量条件校验：等级、有机认证、指定环节检测合格。"""
    batch = get_or_404(conn, "batch", "batch_id", batch_id)
    if conditions.get("grade_required") and batch["grade"]:
        if batch["grade"] != conditions["grade_required"]:
            raise DomainError(
                f"批次 {batch_id} 等级 {batch['grade']} 不满足订单要求 "
                f"{conditions['grade_required']}", 422)
    stage = conditions.get("require_inspection_stage")
    if stage:
        row = conn.execute(
            "SELECT 1 FROM inspection WHERE subject_type='BATCH' AND subject_id=? "
            "AND stage=? AND result='PASS'", (batch_id, stage),
        ).fetchone()
        if not row:
            raise DomainError(
                f"批次 {batch_id} 缺少 {stage} 环节合格检测，不能分配出口", 422)
    if conditions.get("require_organic"):
        harvest = get_or_404(conn, "harvest_batch", "harvest_id",
                             batch["root_harvest_id"])
        pond = get_or_404(conn, "pond", "pond_id", harvest["pond_id"])
        cert = conn.execute(
            "SELECT 1 FROM certification WHERE status='ACTIVE' "
            "AND ((subject_type='POND' AND subject_id=?) "
            "OR (subject_type='WATER_BODY' AND subject_id=?)) "
            "AND valid_from<=? AND valid_to>=?",
            (pond["pond_id"], pond["water_body_id"], now_iso(), now_iso()),
        ).fetchone()
        if not cert:
            raise DomainError(f"批次 {batch_id} 关联塘口当前无有效有机认证", 422)


def allocate(conn, payload: dict) -> dict:
    """把多个批次的可用库存并发分配给一个柜。

    全部数量条件更新（单条 UPDATE 原子判定库存），任一条失败整体回滚，
    因此高并发下也不会超卖。
    """
    require(payload, ["allocation_id", "container_id", "sales_id", "lines"])
    if _person_role(conn, payload["sales_id"]) != "sales":
        raise Forbidden("分配只能由 sales 角色执行")
    lines = payload["lines"]
    if not isinstance(lines, list) or not lines:
        raise DomainError("分配明细不能为空", 400)
    total = 0.0
    for line in lines:
        require(line, ["batch_id", "qty_kg"])
        if line["qty_kg"] <= 0:
            raise DomainError("分配重量必须为正", 400)
        total += float(line["qty_kg"])

    container = _get_container(conn, payload["container_id"])
    order = get_or_404(conn, "export_order", "order_id", container["order_id"])
    if order["status"] in ("COMPLETED", "CANCELLED"):
        raise DomainError(f"订单 {order['order_id']} 已{order['status']}", 422)
    if container["status"] in ("SHIPPED", "RECEIVED", "RETURNED"):
        raise DomainError(f"柜 {container['container_id']} 已发运，不能再分配", 422)
    free_capacity = (container["target_kg"] - container["allocated_kg"]
                     - container["shipped_kg"] + container["returned_kg"])
    if free_capacity + QTY_EPS < total:
        raise Conflict(
            f"柜剩余容量 {free_capacity}kg 不足，本次申请 {total}kg")

    import json as _json
    conditions = _json.loads(order["conditions_json"] or "{}")
    conditions["grade_required"] = order["grade_required"]

    ts = now_iso()
    allocations: list[dict] = []
    for i, line in enumerate(lines, 1):
        bid = line["batch_id"]
        qty = float(line["qty_kg"])
        batch = get_or_404(conn, "batch", "batch_id", bid)
        if batch["status"] != "AVAILABLE":
            raise Conflict(f"批次 {bid} 状态为 {batch['status']}，不可分配")
        _conditions_met(conn, conditions, bid)
        # 原子条件更新：可用量不足时 rowcount=0，绝无超卖。
        cur = conn.execute(
            "UPDATE batch SET allocated_kg = allocated_kg + ? "
            "WHERE batch_id=? AND status='AVAILABLE' AND "
            "qty_kg - allocated_kg - shipped_kg - destroyed_kg - split_out_kg >= ? "
            "- 1e-6",
            (qty, bid, qty),
        )
        if cur.rowcount != 1:
            raise Conflict(f"批次 {bid} 可用库存不足 {qty}kg，分配失败")
        alloc_id = (payload["allocation_id"] if len(lines) == 1
                    else f"{payload['allocation_id']}-{i}")
        try:
            conn.execute(
                "INSERT INTO batch_allocation(allocation_id, container_id, "
                "batch_id, qty_kg, status, created_at) "
                "VALUES (?,?,?,?,'HELD',?)",
                (alloc_id, payload["container_id"], bid, qty, ts))
        except sqlite3.IntegrityError:
            raise Conflict(f"批次 {bid} 在该柜已存在分配，请合并数量")
        conn.execute(
            "INSERT INTO stock_movement(movement_id, batch_id, delta_kg, "
            "ref_type, ref_id, note, created_at) "
            "VALUES (?,?,?,'ALLOCATE_HOLD',?,?,?)",
            (f"MOV-HOLD-{alloc_id}", bid, -qty, alloc_id,
             f"分配至柜 {payload['container_id']}", ts))
        allocations.append({"allocation_id": alloc_id, "batch_id": bid,
                            "qty_kg": qty})

    conn.execute(
        "UPDATE order_container SET allocated_kg = allocated_kg + ?, "
        "status=CASE WHEN status='PLANNING' THEN 'ALLOCATED' ELSE status END "
        "WHERE container_id=?",
        (total, payload["container_id"]))
    record_event(conn, "trading", payload["container_id"], "order.allocate",
                 {"allocation_id": payload["allocation_id"],
                  "container_id": payload["container_id"], "lines": lines})
    return {"container_id": payload["container_id"], "allocations": allocations}


def release_allocation(conn, payload: dict) -> dict:
    """释放未装运的预留库存。"""
    require(payload, ["allocation_id", "sales_id"])
    if _person_role(conn, payload["sales_id"]) != "sales":
        raise Forbidden("释放只能由 sales 角色执行")
    alloc = get_or_404(conn, "batch_allocation", "allocation_id",
                       payload["allocation_id"])
    if alloc["status"] != "HELD":
        raise DomainError(f"分配 {alloc['allocation_id']} 状态为 "
                          f"{alloc['status']}，不能释放", 422)
    unshipped = alloc["qty_kg"] - alloc["shipped_kg"]
    ts = now_iso()
    conn.execute(
        "UPDATE batch SET allocated_kg = allocated_kg - ? WHERE batch_id=?",
        (unshipped, alloc["batch_id"]))
    conn.execute(
        "UPDATE order_container SET allocated_kg = allocated_kg - ? "
        "WHERE container_id=?", (unshipped, alloc["container_id"]))
    conn.execute(
        "UPDATE batch_allocation SET status=CASE WHEN shipped_kg > 0 "
        "THEN 'SHIPPED' ELSE 'RELEASED' END WHERE allocation_id=?",
        (alloc["allocation_id"],))
    conn.execute(
        "INSERT INTO stock_movement(movement_id, batch_id, delta_kg, ref_type, "
        "ref_id, note, created_at) VALUES (?,?,?,'ALLOC_RELEASE',?,?,?)",
        (f"MOV-REL-{alloc['allocation_id']}", alloc["batch_id"], unshipped,
         alloc["allocation_id"], "释放预留", ts))
    record_event(conn, "trading", alloc["allocation_id"], "order.release",
                 payload)
    return {"allocation_id": alloc["allocation_id"], "released_kg": unshipped}


def ship(conn, payload: dict) -> dict:
    """发运：预留转已装；冻结批次不得出库。"""
    require(payload, ["shipment_id", "allocation_id", "qty_kg", "shipped_at"])
    alloc = get_or_404(conn, "batch_allocation", "allocation_id",
                       payload["allocation_id"])
    if alloc["status"] != "HELD":
        raise DomainError("只有 HELD 状态的分配可以发运", 422)
    qty = float(payload["qty_kg"])
    if qty <= 0:
        raise DomainError("发运重量必须为正", 400)
    if alloc["qty_kg"] - alloc["shipped_kg"] + QTY_EPS < qty:
        raise Conflict(
            f"分配待装 {alloc['qty_kg'] - alloc['shipped_kg']}kg 不足 {qty}kg")
    if batch_is_frozen(conn, alloc["batch_id"]):
        raise DomainError("批次处于冻结状态，禁止出库", 422)

    ts = now_iso()
    conn.execute(
        "UPDATE batch SET allocated_kg=allocated_kg-?, shipped_kg=shipped_kg+? "
        "WHERE batch_id=?", (qty, qty, alloc["batch_id"]))
    conn.execute(
        "UPDATE batch_allocation SET shipped_kg=shipped_kg+?, "
        "status=CASE WHEN shipped_kg+? >= qty_kg THEN 'SHIPPED' ELSE 'HELD' END "
        "WHERE allocation_id=?", (qty, qty, alloc["allocation_id"]))
    conn.execute(
        "UPDATE order_container SET allocated_kg=allocated_kg-?, "
        "shipped_kg=shipped_kg+?, status='SHIPPED' WHERE container_id=?",
        (qty, qty, alloc["container_id"]))
    conn.execute(
        "INSERT INTO shipment(shipment_id, allocation_id, qty_kg, shipped_at) "
        "VALUES (?,?,?,?)",
        (payload["shipment_id"], alloc["allocation_id"], qty,
         payload["shipped_at"]))
    # 预留转实发：先冲回 HOLD 占用，再记实际出库，两笔净效果为零但语义完整。
    conn.execute(
        "INSERT INTO stock_movement(movement_id, batch_id, delta_kg, ref_type, "
        "ref_id, note, created_at) VALUES (?,?,?,'ALLOC_RELEASE',?,?,?)",
        (f"MOV-SHIPREL-{payload['shipment_id']}", alloc["batch_id"], qty,
         payload["shipment_id"], "发运时预留转实发", ts))
    conn.execute(
        "INSERT INTO stock_movement(movement_id, batch_id, delta_kg, ref_type, "
        "ref_id, note, created_at) VALUES (?,?,?,'SHIP',?,?,?)",
        (f"MOV-SHIP-{payload['shipment_id']}", alloc["batch_id"], -qty,
         payload["shipment_id"], f"发运至 {alloc['container_id']}", ts))
    record_event(conn, "trading", payload["shipment_id"], "order.ship", payload)
    return {"shipment_id": payload["shipment_id"], "qty_kg": qty}


def receipt(conn, payload: dict) -> dict:
    """登记外商交付回执，实收不得超过该柜已装数量。"""
    require(payload, ["receipt_id", "container_id", "delivered_kg",
                      "received_by", "delivered_at"])
    container = _get_container(conn, payload["container_id"])
    if container["shipped_kg"] <= 0:
        raise DomainError("柜尚未发运，不能登记回执", 422)
    if float(payload["delivered_kg"]) > container["shipped_kg"] + QTY_EPS:
        raise Conflict(
            f"回执实收 {payload['delivered_kg']}kg 超过已装 "
            f"{container['shipped_kg']}kg")
    try:
        conn.execute(
            "INSERT INTO delivery_receipt(receipt_id, container_id, delivered_kg, "
            "received_by, delivered_at, evidence_digest) VALUES (?,?,?,?,?,?)",
            (payload["receipt_id"], payload["container_id"],
             payload["delivered_kg"], payload["received_by"],
             payload["delivered_at"], payload.get("evidence_digest")))
    except sqlite3.IntegrityError:
        raise Conflict(f"柜 {payload['container_id']} 已有交付回执")
    conn.execute(
        "UPDATE order_container SET status='RECEIVED' WHERE container_id=?",
        (payload["container_id"],))
    conn.execute(
        "UPDATE export_order SET status='COMPLETED' WHERE order_id=? AND "
        "NOT EXISTS (SELECT 1 FROM order_container WHERE order_id=export_order.order_id "
        "AND status NOT IN ('RECEIVED','RETURNED'))",
        (container["order_id"],))
    record_event(conn, "trading", payload["receipt_id"], "order.receipt",
                 payload)
    return {"receipt_id": payload["receipt_id"],
            "delivered_kg": payload["delivered_kg"]}


def register_return(conn, payload: dict) -> dict:
    """退货入库：形成挂接原批的 RETURN 批次，只追加不改正账。

    结算冲正由财务在 settlement 模块另行签署。
    """
    require(payload, ["return_id", "receipt_id", "batch_id", "qty_kg",
                      "reason", "created_by", "created_at"])
    receipt = get_or_404(conn, "delivery_receipt", "receipt_id",
                         payload["receipt_id"])
    container = _get_container(conn, receipt["container_id"])
    batch = get_or_404(conn, "batch", "batch_id", payload["batch_id"])
    qty = float(payload["qty_kg"])
    # 该批在本柜的可退数量 = 已装 - 已退。
    alloc = conn.execute(
        "SELECT COALESCE(SUM(shipped_kg),0) AS shipped FROM batch_allocation "
        "WHERE container_id=? AND batch_id=?",
        (container["container_id"], payload["batch_id"]),
    ).fetchone()
    returned = conn.execute(
        "SELECT COALESCE(SUM(r.qty_kg),0) AS q FROM sales_return r "
        "JOIN delivery_receipt d ON d.receipt_id=r.receipt_id "
        "WHERE d.container_id=? AND r.batch_id=?",
        (container["container_id"], payload["batch_id"]),
    ).fetchone()
    if alloc["shipped"] - returned["q"] + QTY_EPS < qty:
        raise Conflict(
            f"批次 {payload['batch_id']} 在柜 {container['container_id']} "
            f"可退 {alloc['shipped'] - returned['q']}kg，申请 {qty}kg")

    return_batch_id = f"B-RET-{payload['return_id']}"
    ts = now_iso()
    try:
        conn.execute(
            "INSERT INTO sales_return(return_id, receipt_id, batch_id, qty_kg, "
            "reason, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (payload["return_id"], payload["receipt_id"], payload["batch_id"],
             qty, payload["reason"], payload["created_by"], payload["created_at"]))
        conn.execute(
            "INSERT INTO batch(batch_id, origin_batch_id, root_harvest_id, kind, "
            "grade, qty_kg, status, created_at) VALUES (?,?,?,'RETURN',?,?,?,?)",
            (return_batch_id, batch["batch_id"], batch["root_harvest_id"],
             batch["grade"], qty, "AVAILABLE", ts))
        conn.execute(
            "UPDATE order_container SET returned_kg=returned_kg+?, "
            "status='RETURNED' WHERE container_id=?",
            (qty, container["container_id"]))
        conn.execute(
            "INSERT INTO stock_movement(movement_id, batch_id, delta_kg, ref_type, "
            "ref_id, note, created_at) VALUES (?,?,?,'RETURN_IN',?,?,?)",
            (f"MOV-RET-{payload['return_id']}", return_batch_id, qty,
             payload["return_id"],
             f"退货入库，原批 {batch['batch_id']}", ts))
    except sqlite3.IntegrityError as exc:
        raise Conflict(f"退货写入冲突：{exc}")
    record_event(conn, "trading", payload["return_id"], "order.return",
                 payload, result_ref=return_batch_id)
    return {"return_id": payload["return_id"],
            "return_batch_id": return_batch_id, "qty_kg": qty}


def order_availability(conn, order_id: str) -> dict:
    """订单联动视图：柜量目标、已分配、已装、可用库存能否满足。"""
    order = get_or_404(conn, "export_order", "order_id", order_id)
    containers = conn.execute(
        "SELECT * FROM order_container WHERE order_id=? ORDER BY seq",
        (order_id,),
    ).fetchall()
    grade = order["grade_required"]
    avail = conn.execute(
        "SELECT COALESCE(SUM(qty_kg-allocated_kg-shipped_kg-destroyed_kg-"
        "split_out_kg),0) AS kg FROM batch WHERE status='AVAILABLE' AND grade=?",
        (grade,),
    ).fetchone()["kg"]
    total_target = sum(c["target_kg"] for c in containers)
    total_held = sum(c["allocated_kg"] for c in containers)
    total_shipped = sum(c["shipped_kg"] for c in containers)
    total_returned = sum(c["returned_kg"] for c in containers)
    open_kg = total_target - total_held - total_shipped + total_returned
    return {
        "order_id": order_id,
        "grade_required": grade,
        "target_kg": total_target,
        "allocated_kg": total_held,
        "shipped_kg": total_shipped,
        "returned_kg": total_returned,
        "open_kg": open_kg,
        "grade_available_stock_kg": avail,
        "can_fulfill": avail + QTY_EPS >= open_kg,
        "containers": [dict(c) for c in containers],
    }
