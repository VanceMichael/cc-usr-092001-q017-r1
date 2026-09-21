"""主数据：主体、塘口、苗种、投入品、指导与有机认证。"""

import sqlite3

from .common import Conflict, DomainError, get_or_404, now_iso, record_event, require

ROLES = {"manager", "inspector", "reviewer", "supervisor",
         "technician", "sales", "finance"}


def person(conn: sqlite3.Connection, payload: dict) -> dict:
    require(payload, ["person_id", "name", "role"])
    if payload["role"] not in ROLES:
        raise DomainError(f"未知角色：{payload['role']}", 400)
    try:
        conn.execute(
            "INSERT INTO person(person_id, name, role) VALUES (?,?,?)",
            (payload["person_id"], payload["name"], payload["role"]),
        )
    except sqlite3.IntegrityError:
        raise Conflict(f"人员 {payload['person_id']} 已存在")
    record_event(conn, "masters", payload["person_id"], "person.register", payload)
    return {"person_id": payload["person_id"]}


def _insert(conn, table: str, payload: dict, columns: list[str],
            source: str, subject: str, action: str,
            required: list[str] | None = None) -> dict:
    require(payload, required if required is not None else columns)
    values = [payload.get(c) for c in columns]
    try:
        conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' * len(columns))})",
            values,
        )
    except sqlite3.IntegrityError as exc:
        raise Conflict(f"{table} 写入冲突：{exc}")
    record_event(conn, source, subject, action, payload)
    return {"id": payload[columns[0]]}


def base(conn, payload: dict) -> dict:
    payload.setdefault("created_at", now_iso())
    return _insert(conn, "base", payload,
                   ["base_id", "name", "region", "created_at"],
                   "masters", payload["base_id"], "base.register")


def farmer(conn, payload: dict) -> dict:
    payload.setdefault("joined_at", now_iso())
    get_or_404(conn, "base", "base_id", payload["base_id"])
    return _insert(conn, "farmer", payload,
                   ["farmer_id", "name", "base_id", "id_code_digest",
                    "joined_at"],
                   "masters", payload["farmer_id"], "farmer.register",
                   required=["farmer_id", "name", "base_id", "joined_at"])


def water_body(conn, payload: dict) -> dict:
    payload.setdefault("created_at", now_iso())
    get_or_404(conn, "base", "base_id", payload["base_id"])
    return _insert(conn, "water_body", payload,
                   ["water_body_id", "base_id", "name", "created_at"],
                   "masters", payload["water_body_id"], "water_body.register")


def pond(conn, payload: dict) -> dict:
    payload.setdefault("created_at", now_iso())
    payload.setdefault("status", "ACTIVE")
    get_or_404(conn, "water_body", "water_body_id", payload["water_body_id"])
    get_or_404(conn, "farmer", "farmer_id", payload["farmer_id"])
    return _insert(conn, "pond", payload,
                   ["pond_id", "code", "water_body_id", "farmer_id",
                    "area_mu", "status", "created_at"],
                   "masters", payload["pond_id"], "pond.register")


def seed_lot(conn, payload: dict) -> dict:
    require(payload, ["seed_lot_id", "species", "supplier_ref",
                      "qty_seed", "produced_at"])
    return _insert(conn, "seed_lot", payload,
                   ["seed_lot_id", "species", "supplier_ref", "qty_seed",
                    "produced_at", "cert_ref", "payload_digest"],
                   "masters", payload["seed_lot_id"], "seed_lot.register",
                   required=["seed_lot_id", "species", "supplier_ref",
                             "qty_seed", "produced_at"])


def stocking(conn, payload: dict) -> dict:
    payload.setdefault("stocked_at", now_iso())
    get_or_404(conn, "seed_lot", "seed_lot_id", payload["seed_lot_id"])
    get_or_404(conn, "pond", "pond_id", payload["pond_id"])
    return _insert(conn, "stocking", payload,
                   ["stocking_id", "seed_lot_id", "pond_id", "qty",
                    "stocked_at", "recorder_id"],
                   "masters", payload["stocking_id"], "stocking.register",
                   required=["stocking_id", "seed_lot_id", "pond_id", "qty",
                             "stocked_at"])


def material(conn, payload: dict) -> dict:
    require(payload, ["material_code", "name", "category"])
    payload.setdefault("organic_allowed", 0)
    return _insert(conn, "material", payload,
                   ["material_code", "name", "category", "organic_allowed"],
                   "masters", payload["material_code"], "material.register")


def application(conn, payload: dict) -> dict:
    payload.setdefault("applied_at", now_iso())
    get_or_404(conn, "pond", "pond_id", payload["pond_id"])
    material_row = get_or_404(conn, "material", "material_code",
                              payload["material_code"])
    if material_row["category"] in ("VET_DRUG", "DISINFECTANT"):
        # 禁用投入品不得出现在养殖记录中。
        if not material_row["organic_allowed"]:
            raise DomainError(
                f"投入品 {payload['material_code']} 非有机许可，禁止施用", 422)
    return _insert(conn, "input_application", payload,
                   ["application_id", "pond_id", "material_code",
                    "material_lot", "qty", "unit", "applied_at",
                    "recorder_id", "evidence_digest"],
                   "masters", payload["application_id"], "input.apply",
                   required=["application_id", "pond_id", "material_code",
                             "qty", "unit", "applied_at"])


def guidance(conn, payload: dict) -> dict:
    payload.setdefault("occurred_at", now_iso())
    get_or_404(conn, "pond", "pond_id", payload["pond_id"])
    tech = get_or_404(conn, "person", "person_id", payload["technician_id"])
    if tech["role"] != "technician":
        raise DomainError("技术指导只能由 technician 角色记录", 403)
    return _insert(conn, "guidance", payload,
                   ["guidance_id", "pond_id", "technician_id", "topic",
                    "content_ref", "occurred_at"],
                   "masters", payload["guidance_id"], "guidance.record",
                   required=["guidance_id", "pond_id", "technician_id",
                             "topic", "occurred_at"])


def certification(conn, payload: dict) -> dict:
    require(payload, ["cert_id", "subject_type", "subject_id", "cert_no",
                      "issuer_ref", "valid_from", "valid_to"])
    payload.setdefault("status", "ACTIVE")
    if payload["valid_to"] <= payload["valid_from"]:
        raise DomainError("认证失效日期必须晚于生效日期", 400)
    return _insert(conn, "certification", payload,
                   ["cert_id", "subject_type", "subject_id", "cert_no",
                    "issuer_ref", "valid_from", "valid_to", "status",
                    "document_digest"],
                   "masters", payload["cert_id"], "certification.register",
                   required=["cert_id", "subject_type", "subject_id",
                             "cert_no", "issuer_ref", "valid_from",
                             "valid_to"])


def organic_cert_active(conn, subject_type: str, subject_id: str,
                        at_iso: str) -> sqlite3.Row | None:
    """返回在给定时间点有效的有机认证。"""
    return conn.execute(
        "SELECT * FROM certification WHERE subject_type=? AND subject_id=? "
        "AND status='ACTIVE' AND valid_from<=? AND valid_to>=?",
        (subject_type, subject_id, at_iso, at_iso),
    ).fetchone()
