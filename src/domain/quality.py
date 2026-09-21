"""品控：五重检测、定向冻结、复检/解封/销毁的角色分离签署。"""

import sqlite3

from .common import (Conflict, DomainError, Forbidden, get_or_404, now_iso,
                     record_event, require)

STAGES = ("SEED", "WATER", "INPUT", "PRODUCT", "EXPORT")

# 各动作要求的签署角色；复检与解封不得为同一人（职能分离）。
ACTION_ROLE = {
    "FREEZE": "supervisor",
    "RETEST": "inspector",
    "RELEASE": "reviewer",
    "DESTROY": "reviewer",
}
SINGLE_ACTIONS = ("FREEZE", "RELEASE", "DESTROY")


def _check_role(conn, person_id: str, expected_role: str) -> sqlite3.Row:
    person = get_or_404(conn, "person", "person_id", person_id)
    if person["role"] != expected_role:
        raise Forbidden(
            f"动作需要 {expected_role} 角色，{person_id} 为 {person['role']}")
    return person


def inspection(conn, payload: dict) -> dict:
    """登记一次检测。PRODUCT 检测的对象为批次。"""
    require(payload, ["inspection_id", "stage", "subject_type", "subject_id",
                      "result", "inspector_id", "sampled_at"])
    if payload["stage"] not in STAGES:
        raise DomainError(f"检测环节必须为 {STAGES}", 400)
    _check_role(conn, payload["inspector_id"], "inspector")
    payload.setdefault("resulted_at", now_iso())
    payload.setdefault("items_json", "[]")
    try:
        conn.execute(
            "INSERT INTO inspection(inspection_id, stage, subject_type, subject_id, "
            "result, inspector_id, sampled_at, resulted_at, items_json, report_digest) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (payload["inspection_id"], payload["stage"], payload["subject_type"],
             payload["subject_id"], payload["result"], payload["inspector_id"],
             payload["sampled_at"], payload["resulted_at"], payload["items_json"],
             payload.get("report_digest")),
        )
    except sqlite3.IntegrityError:
        raise Conflict(f"检测单 {payload['inspection_id']} 已存在")
    record_event(conn, "quality", payload["inspection_id"], "inspection.record",
                 payload)
    return {"inspection_id": payload["inspection_id"],
            "result": payload["result"]}


# -- 冻结范围推导 -----------------------------------------------------------

def _descendant_batches(conn, batch_ids: list[str]) -> set[str]:
    """批次拆分树：父批被冻结时，全部下游子批一并冻结。"""
    frozen: set[str] = set()
    frontier = list(batch_ids)
    while frontier:
        current = frontier.pop()
        if current in frozen:
            continue
        frozen.add(current)
        rows = conn.execute(
            "SELECT batch_id FROM batch WHERE parent_batch_id = ?",
            (current,),
        ).fetchall()
        frontier.extend(r["batch_id"] for r in rows)
    return frozen


def affected_scope(conn, water_body_ids: list[str],
                   batch_ids: list[str]) -> tuple[set[str], set[str]]:
    """根据水体与批次推导完整冻结范围。

    水体 -> 其下所有塘口的起捕批（及拆分子批）；显式批次 -> 其下游子批。
    """
    ponds: set[str] = set()
    for wb in water_body_ids:
        rows = conn.execute(
            "SELECT pond_id FROM pond WHERE water_body_id = ?", (wb,)
        ).fetchall()
        ponds.update(r["pond_id"] for r in rows)
    root_batches = set(batch_ids)
    if ponds:
        rows = conn.execute(
            f"SELECT batch_id FROM batch b JOIN harvest_batch h "
            f"ON b.root_harvest_id = h.harvest_id "
            f"WHERE h.pond_id IN ({','.join('?' * len(ponds))})",
            list(ponds),
        ).fetchall()
        root_batches.update(r["batch_id"] for r in rows)
    batch_scope = _descendant_batches(conn, list(root_batches))
    wb_scope = set(water_body_ids)
    for pond_id in ponds:
        row = conn.execute(
            "SELECT water_body_id FROM pond WHERE pond_id = ?", (pond_id,)
        ).fetchone()
        if row:
            wb_scope.add(row["water_body_id"])
    return wb_scope, batch_scope


def _batch_frozen_by(conn, batch_id: str) -> str | None:
    row = conn.execute(
        "SELECT ft.freeze_id FROM freeze_target ft "
        "JOIN freeze_order fo ON fo.freeze_id = ft.freeze_id "
        "WHERE ft.target_type='BATCH' AND ft.target_id=? AND fo.status='FROZEN'",
        (batch_id,),
    ).fetchone()
    return row["freeze_id"] if row else None


def batch_is_frozen(conn, batch_id: str) -> bool:
    return _batch_frozen_by(conn, batch_id) is not None


def water_body_is_frozen(conn, water_body_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM freeze_target ft JOIN freeze_order fo "
        "ON fo.freeze_id = ft.freeze_id WHERE ft.target_type='WATER_BODY' "
        "AND ft.target_id=? AND fo.status='FROZEN' LIMIT 1",
        (water_body_id,)).fetchone()
    return row is not None


def freeze(conn, payload: dict) -> dict:
    """检测不合格后，由督导创建冻结单，只冻结相关水体与关联批次。"""
    require(payload, ["freeze_id", "inspection_id", "created_by", "reason"])
    insp = get_or_404(conn, "inspection", "inspection_id",
                      payload["inspection_id"])
    if insp["result"] != "FAIL":
        raise DomainError("只有检测结果为 FAIL 才能发起冻结", 422)
    _check_role(conn, payload["created_by"], ACTION_ROLE["FREEZE"])

    water_body_ids = payload.get("water_body_ids", [])
    batch_ids = payload.get("batch_ids", [])
    if not water_body_ids and not batch_ids:
        raise DomainError("冻结单必须至少指定一个水体或批次", 400)
    wb_scope, batch_scope = affected_scope(conn, water_body_ids, batch_ids)

    ts = now_iso()
    try:
        conn.execute(
            "INSERT INTO freeze_order(freeze_id, inspection_id, reason, status, "
            "created_by, created_at) VALUES (?,?,?,'FROZEN',?,?)",
            (payload["freeze_id"], payload["inspection_id"], payload["reason"],
             payload["created_by"], ts),
        )
        for wb in sorted(wb_scope):
            conn.execute(
                "INSERT INTO freeze_target(freeze_id, target_type, target_id) "
                "VALUES (?,'WATER_BODY',?)", (payload["freeze_id"], wb))
        for bid in sorted(batch_scope):
            conn.execute(
                "INSERT INTO freeze_target(freeze_id, target_type, target_id) "
                "VALUES (?,'BATCH',?)", (payload["freeze_id"], bid))
            conn.execute(
                "UPDATE batch SET status='FROZEN' WHERE batch_id=? "
                "AND status='AVAILABLE'", (bid,))
            conn.execute(
                "INSERT INTO stock_movement(movement_id, batch_id, delta_kg, "
                "ref_type, ref_id, note, created_at) "
                "VALUES (?,?,0,'FREEZE_HOLD',?,?,?)",
                (f"MOV-{payload['freeze_id']}-{bid}", bid,
                 payload["freeze_id"], "质量冻结，暂停分配", ts))
        conn.execute(
            "INSERT INTO freeze_action(action_id, freeze_id, action, signer_id, "
            "signer_role, signed_at) VALUES (?,?, 'FREEZE', ?, ?, ?)",
            (f"ACT-{payload['freeze_id']}-FREEZE", payload["freeze_id"],
             payload["created_by"], ACTION_ROLE["FREEZE"], ts))
    except sqlite3.IntegrityError as exc:
        raise Conflict(f"冻结单写入冲突：{exc}")

    record_event(conn, "quality", payload["freeze_id"], "freeze.create",
                 {"freeze_id": payload["freeze_id"],
                  "inspection_id": payload["inspection_id"],
                  "water_bodies": sorted(wb_scope),
                  "batches": sorted(batch_scope)})
    return {"freeze_id": payload["freeze_id"],
            "water_bodies": sorted(wb_scope),
            "batches": sorted(batch_scope)}


def _open_freeze(conn, freeze_id: str) -> sqlite3.Row:
    fo = get_or_404(conn, "freeze_order", "freeze_id", freeze_id)
    if fo["status"] != "FROZEN":
        raise DomainError(
            f"冻结单 {freeze_id} 已结束（{fo['status']}），不可重复签署", 422)
    return fo


def retest(conn, payload: dict) -> dict:
    """复检由检测员执行并签署；与初始检测人不同更能保证独立。"""
    require(payload, ["freeze_id", "signer_id", "inspection_id"])
    _open_freeze(conn, payload["freeze_id"])
    _check_role(conn, payload["signer_id"], ACTION_ROLE["RETEST"])
    insp = get_or_404(conn, "inspection", "inspection_id",
                      payload["inspection_id"])
    if insp["result"] == "PENDING":
        raise DomainError("复检尚无结论，不能签署", 422)
    creator = conn.execute(
        "SELECT created_by FROM freeze_order WHERE freeze_id=?",
        (payload["freeze_id"],),
    ).fetchone()
    if payload["signer_id"] == creator["created_by"]:
        raise Forbidden("复检签署人不得与冻结创建人为同一人")

    ts = now_iso()
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM freeze_action WHERE freeze_id=? AND action='RETEST'",
        (payload["freeze_id"],),
    ).fetchone()["c"]
    conn.execute(
        "INSERT INTO freeze_action(action_id, freeze_id, action, signer_id, "
        "signer_role, result, note, signed_at, retest_inspection_id) "
        "VALUES (?,?,'RETEST',?,?,?,?,?,?)",
        (f"ACT-{payload['freeze_id']}-RETEST-{n + 1}", payload["freeze_id"],
         payload["signer_id"], ACTION_ROLE["RETEST"], insp["result"],
         payload.get("note"), ts, payload["inspection_id"]))
    record_event(conn, "quality", payload["freeze_id"], "freeze.retest",
                 {"freeze_id": payload["freeze_id"],
                  "inspection_id": payload["inspection_id"],
                  "result": insp["result"],
                  "signer_id": payload["signer_id"]})
    return {"freeze_id": payload["freeze_id"], "result": insp["result"]}


def _unique_signer(conn, freeze_id: str, action: str, signer_id: str) -> None:
    """解封/销毁签署人不能是冻结创建人，也不能是任一复检人。"""
    fo = conn.execute(
        "SELECT created_by FROM freeze_order WHERE freeze_id=?", (freeze_id,)
    ).fetchone()
    if signer_id == fo["created_by"]:
        raise Forbidden(f"{action} 签署人不得与冻结创建人为同一人")
    rows = conn.execute(
        "SELECT DISTINCT signer_id FROM freeze_action "
        "WHERE freeze_id=? AND action='RETEST'", (freeze_id,)
    ).fetchall()
    if any(r["signer_id"] == signer_id for r in rows):
        raise Forbidden(f"{action} 签署人不得与复检人为同一人")


def _once(conn, freeze_id: str, action: str) -> None:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM freeze_action WHERE freeze_id=? AND action=?",
        (freeze_id, action),
    ).fetchone()
    if row["c"]:
        raise Conflict(f"冻结单 {freeze_id} 已存在 {action} 签署")


def release(conn, payload: dict) -> dict:
    """复检全部合格后，由审核人解封；恢复批次可用状态。"""
    require(payload, ["freeze_id", "signer_id"])
    _open_freeze(conn, payload["freeze_id"])
    _once(conn, payload["freeze_id"], "RELEASE")
    _check_role(conn, payload["signer_id"], ACTION_ROLE["RELEASE"])
    _unique_signer(conn, payload["freeze_id"], "RELEASE", payload["signer_id"])

    last = conn.execute(
        "SELECT result FROM freeze_action WHERE freeze_id=? AND action='RETEST' "
        "ORDER BY signed_at DESC, action_id DESC LIMIT 1",
        (payload["freeze_id"],),
    ).fetchone()
    if last is None or last["result"] != "PASS":
        raise DomainError("最近一次复检必须为 PASS 才能解封", 422)

    ts = now_iso()
    targets = conn.execute(
        "SELECT target_id FROM freeze_target WHERE freeze_id=? AND target_type='BATCH'",
        (payload["freeze_id"],),
    ).fetchall()
    for t in targets:
        conn.execute(
            "UPDATE batch SET status='AVAILABLE' WHERE batch_id=? AND status='FROZEN'",
            (t["target_id"],))
    conn.execute(
        "UPDATE freeze_order SET status='RELEASED', closed_at=? WHERE freeze_id=?",
        (ts, payload["freeze_id"]))
    conn.execute(
        "INSERT INTO freeze_action(action_id, freeze_id, action, signer_id, "
        "signer_role, note, signed_at) VALUES (?,?,'RELEASE',?,?,?,?)",
        (f"ACT-{payload['freeze_id']}-RELEASE", payload["freeze_id"],
         payload["signer_id"], ACTION_ROLE["RELEASE"], payload.get("note"), ts))
    record_event(conn, "quality", payload["freeze_id"], "freeze.release",
                 {"freeze_id": payload["freeze_id"],
                  "signer_id": payload["signer_id"]})
    return {"freeze_id": payload["freeze_id"], "status": "RELEASED"}


def destroy(conn, payload: dict) -> dict:
    """复检不合格，由审核人签署销毁；核销批次数量并记录移动。"""
    require(payload, ["freeze_id", "signer_id"])
    _open_freeze(conn, payload["freeze_id"])
    _once(conn, payload["freeze_id"], "DESTROY")
    _check_role(conn, payload["signer_id"], ACTION_ROLE["DESTROY"])
    _unique_signer(conn, payload["freeze_id"], "DESTROY", payload["signer_id"])

    ts = now_iso()
    targets = conn.execute(
        "SELECT target_id FROM freeze_target WHERE freeze_id=? AND target_type='BATCH'",
        (payload["freeze_id"],),
    ).fetchall()
    total = 0.0
    for t in targets:
        bid = t["target_id"]
        b = get_or_404(conn, "batch", "batch_id", bid)
        # 已拆出重量属于子批；本批可销毁的是未装运、未销毁、未拆出的部分。
        remaining = (b["qty_kg"] - b["shipped_kg"] - b["destroyed_kg"]
                     - b["split_out_kg"])
        if remaining <= 0:
            continue
        held_rel = min(b["allocated_kg"], remaining)
        conn.execute(
            "UPDATE batch SET destroyed_kg=destroyed_kg+?, "
            "allocated_kg=MAX(0, allocated_kg-?), status='DESTROYED' "
            "WHERE batch_id=?",
            (remaining, held_rel, bid))
        # 同步释放该批未装运的柜预留，容量还回柜以便另行补货。
        if held_rel > 0:
            held_allocs = conn.execute(
                "SELECT allocation_id, container_id, qty_kg, shipped_kg "
                "FROM batch_allocation WHERE batch_id=? AND status='HELD'",
                (bid,)).fetchall()
            for a in held_allocs:
                unshipped = a["qty_kg"] - a["shipped_kg"]
                conn.execute(
                    "UPDATE batch_allocation SET status='RELEASED' "
                    "WHERE allocation_id=?", (a["allocation_id"],))
                conn.execute(
                    "UPDATE order_container SET allocated_kg=MAX(0, allocated_kg-?) "
                    "WHERE container_id=?", (unshipped, a["container_id"]))
            conn.execute(
                "INSERT INTO stock_movement(movement_id, batch_id, delta_kg, "
                "ref_type, ref_id, note, created_at) "
                "VALUES (?,?,?,'ALLOC_RELEASE',?,?,?)",
                (f"MOV-DREL-{bid}-{payload['freeze_id']}", bid, held_rel,
                 payload["freeze_id"], "销毁时释放未装运预留", ts))
        conn.execute(
            "INSERT INTO stock_movement(movement_id, batch_id, delta_kg, ref_type, "
            "ref_id, note, created_at) VALUES (?,?,?,'DESTROY',?,?,?)",
            (f"MOV-DEST-{bid}-{payload['freeze_id']}", bid, -remaining,
             payload["freeze_id"], payload.get("note", "复检不合格销毁"), ts))
        total += remaining
    conn.execute(
        "UPDATE freeze_order SET status='DESTROYED', closed_at=? WHERE freeze_id=?",
        (ts, payload["freeze_id"]))
    conn.execute(
        "INSERT INTO freeze_action(action_id, freeze_id, action, signer_id, "
        "signer_role, note, signed_at) VALUES (?,?,'DESTROY',?,?,?,?)",
        (f"ACT-{payload['freeze_id']}-DESTROY", payload["freeze_id"],
         payload["signer_id"], ACTION_ROLE["DESTROY"], payload.get("note"), ts))
    record_event(conn, "quality", payload["freeze_id"], "freeze.destroy",
                 {"freeze_id": payload["freeze_id"],
                  "destroyed_kg": total,
                  "signer_id": payload["signer_id"]})
    return {"freeze_id": payload["freeze_id"], "status": "DESTROYED",
            "destroyed_kg": total}


def freeze_detail(conn, freeze_id: str) -> dict:
    fo = get_or_404(conn, "freeze_order", "freeze_id", freeze_id)
    targets = conn.execute(
        "SELECT target_type, target_id FROM freeze_target WHERE freeze_id=?",
        (freeze_id,),
    ).fetchall()
    actions = conn.execute(
        "SELECT action, signer_id, signer_role, result, signed_at, "
        "retest_inspection_id FROM freeze_action WHERE freeze_id=? "
        "ORDER BY signed_at, action_id", (freeze_id,),
    ).fetchall()
    return {
        "freeze_id": freeze_id,
        "inspection_id": fo["inspection_id"],
        "reason": fo["reason"],
        "status": fo["status"],
        "created_by": fo["created_by"],
        "water_bodies": [t["target_id"] for t in targets
                         if t["target_type"] == "WATER_BODY"],
        "batches": [t["target_id"] for t in targets
                    if t["target_type"] == "BATCH"],
        "actions": [dict(a) for a in actions],
    }
