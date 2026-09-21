"""结算：合同版本、按等级重量与帮扶加价生成、台账只追加、退货走冲正。"""

import json
import sqlite3
from decimal import Decimal

from .common import (Conflict, DomainError, Forbidden, QTY_EPS, as_decimal,
                     get_or_404, money, now_iso, record_event, require)


def _finance(conn, person_id: str) -> None:
    person = get_or_404(conn, "person", "person_id", person_id)
    if person["role"] != "finance":
        raise Forbidden("结算操作必须由 finance 角色执行")


def create_contract(conn, payload: dict) -> dict:
    """登记合同版本；新版本生效时旧版本自动置为非当前。"""
    require(payload, ["contract_id", "farmer_id", "grade_prices"])
    get_or_404(conn, "farmer", "farmer_id", payload["farmer_id"])
    grade_prices = payload["grade_prices"]
    if not isinstance(grade_prices, dict) or not grade_prices:
        raise DomainError("grade_prices 必须为非空的等级->单价映射", 400)
    payload.setdefault("support_premium_per_kg", 0)
    payload.setdefault("effective_from", now_iso())
    payload.setdefault("created_at", now_iso())

    row = conn.execute(
        "SELECT COALESCE(MAX(version),0) AS v FROM contract WHERE farmer_id=?",
        (payload["farmer_id"],),
    ).fetchone()
    version = int(payload.get("version") or row["v"] + 1)
    if version <= row["v"]:
        raise Conflict(
            f"合同版本必须递增，当前最大版本为 {row['v']}")
    digest_payload = {"grade_prices": grade_prices,
                      "support_premium_per_kg": payload["support_premium_per_kg"]}
    try:
        conn.execute(
            "UPDATE contract SET is_current=0 WHERE farmer_id=?",
            (payload["farmer_id"],))
        conn.execute(
            "INSERT INTO contract(contract_id, farmer_id, version, effective_from, "
            "grade_prices_json, support_premium_per_kg, is_current, terms_digest, "
            "created_at) VALUES (?,?,?,?,?,?,1,?,?)",
            (payload["contract_id"], payload["farmer_id"], version,
             payload["effective_from"], json.dumps(grade_prices, ensure_ascii=False),
             payload["support_premium_per_kg"],
             payload.get("terms_digest") or "sha256:" + json.dumps(digest_payload),
             payload["created_at"]))
    except sqlite3.IntegrityError as exc:
        raise Conflict(f"合同写入冲突：{exc}")
    record_event(conn, "settlement", payload["contract_id"],
                 "contract.version", payload)
    return {"contract_id": payload["contract_id"], "version": version}


def _current_contract(conn, farmer_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM contract WHERE farmer_id=? AND is_current=1",
        (farmer_id,),
    ).fetchone()
    if row is None:
        raise DomainError(f"养殖户 {farmer_id} 没有当前有效合同版本", 422)
    return row


def _settled_kg(conn, harvest_id: str, grade: str) -> float:
    """已进入未冲销结算的重量（草稿占用也算，避免重复生成）。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(l.qty_kg),0) AS kg FROM settlement_line l "
        "JOIN settlement s ON s.settlement_id=l.settlement_id "
        "WHERE l.harvest_id=? AND l.grade=? AND s.status!='REVERSED'",
        (harvest_id, grade),
    ).fetchone()
    return row["kg"]


def generate(conn, payload: dict) -> dict:
    """按当前合同版本对指定起捕批的分级重量生成结算。

    每行金额 = 重量 × (等级单价 + 帮扶加价)；合同版本与单价固化在结算行，
    事后合同改版不影响本笔，保证每笔金额都能解释。
    """
    require(payload, ["settlement_id", "farmer_id", "period", "created_by"])
    _finance(conn, payload["created_by"])
    contract = _current_contract(conn, payload["farmer_id"])
    prices = json.loads(contract["grade_prices_json"])
    premium = as_decimal(contract["support_premium_per_kg"])

    harvest_ids = payload.get("harvest_ids")
    params: list = [payload["farmer_id"]]
    where = "h.farmer_id=?"
    if harvest_ids:
        where += f" AND g.harvest_id IN ({','.join('?' * len(harvest_ids))})"
        params.extend(harvest_ids)
    graded = conn.execute(
        f"SELECT g.harvest_id, g.grade, SUM(g.qty_kg) AS qty FROM grade_record g "
        f"JOIN harvest_batch h ON h.harvest_id=g.harvest_id "
        f"WHERE {where} GROUP BY g.harvest_id, g.grade",
        params,
    ).fetchall()

    lines: list[dict] = []
    for row in graded:
        already = _settled_kg(conn, row["harvest_id"], row["grade"])
        open_qty = row["qty"] - already
        if open_qty <= QTY_EPS:
            continue
        if row["grade"] not in prices:
            raise DomainError(
                f"合同版本 v{contract['version']} 缺少等级 "
                f"{row['grade']} 的单价，无法结算", 422)
        unit_price = as_decimal(prices[row["grade"]])
        amount = (as_decimal(open_qty) * (unit_price + premium)).quantize(
            Decimal("0.01"))
        lines.append({"harvest_id": row["harvest_id"], "grade": row["grade"],
                      "qty_kg": round(open_qty, 6),
                      "unit_price": money(unit_price),
                      "premium_per_kg": money(premium),
                      "amount": money(amount)})
    if not lines:
        raise DomainError("没有待结算的分级重量（可能已全部结算）", 422)

    ts = now_iso()
    try:
        conn.execute(
            "INSERT INTO settlement(settlement_id, farmer_id, contract_id, "
            "contract_version, period, status, generated_at, note) "
            "VALUES (?,?,?,?,?,'DRAFT',?,?)",
            (payload["settlement_id"], payload["farmer_id"],
             contract["contract_id"], contract["version"], payload["period"],
             ts, payload.get("note")))
        for i, line in enumerate(lines, 1):
            conn.execute(
                "INSERT INTO settlement_line(line_id, settlement_id, harvest_id, "
                "grade, qty_kg, unit_price, premium_per_kg, amount) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"SL-{payload['settlement_id']}-{i}", payload["settlement_id"],
                 line["harvest_id"], line["grade"], line["qty_kg"],
                 line["unit_price"], line["premium_per_kg"], line["amount"]))
    except sqlite3.IntegrityError as exc:
        raise Conflict(f"结算写入冲突：{exc}")
    record_event(conn, "settlement", payload["settlement_id"],
                 "settlement.generate",
                 {"settlement_id": payload["settlement_id"], "lines": lines})
    return {"settlement_id": payload["settlement_id"],
            "contract_version": contract["version"], "lines": lines,
            "total": money(sum((as_decimal(l["amount"]) for l in lines),
                               Decimal(0)))}


def confirm(conn, payload: dict) -> dict:
    """确认结算并把每行写入只追加台账（NORMAL）。"""
    require(payload, ["settlement_id", "signer_id"])
    _finance(conn, payload["signer_id"])
    settlement = get_or_404(conn, "settlement", "settlement_id",
                            payload["settlement_id"])
    if settlement["status"] != "DRAFT":
        raise DomainError(f"结算状态为 {settlement['status']}，不能确认", 422)
    ts = now_iso()
    lines = conn.execute(
        "SELECT * FROM settlement_line WHERE settlement_id=?",
        (payload["settlement_id"],),
    ).fetchall()
    for line in lines:
        conn.execute(
            "INSERT INTO ledger_entry(entry_id, settlement_id, line_id, "
            "harvest_id, farmer_id, direction, amount, grade, qty_kg, "
            "unit_price, premium_per_kg, reason, created_by, created_at) "
            "VALUES (?,?,?,?,?,'NORMAL',?,?,?,?,?,?,?,?)",
            (f"LE-{line['line_id']}", payload["settlement_id"],
             line["line_id"], line["harvest_id"], settlement["farmer_id"],
             line["amount"], line["grade"], line["qty_kg"], line["unit_price"],
             line["premium_per_kg"], "结算确认", payload["signer_id"], ts))
    conn.execute(
        "UPDATE settlement SET status='CONFIRMED', confirmed_at=? "
        "WHERE settlement_id=?", (ts, payload["settlement_id"]))
    record_event(conn, "settlement", payload["settlement_id"],
                 "settlement.confirm", payload)
    return {"settlement_id": payload["settlement_id"], "status": "CONFIRMED"}


def _reversed_kg(conn, line_id: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(qty_kg),0) AS kg FROM ledger_entry "
        "WHERE line_id=? AND direction='REVERSAL'", (line_id,),
    ).fetchone()
    return -row["kg"]  # 冲正数量以负金额/负重量记录


def reverse(conn, payload: dict) -> dict:
    """冲正：原台账行不动，新增方向为 REVERSAL 的负向记录。

    可整笔冲正，也可按 (起捕批, 等级, 重量) 部分冲正（退货场景）。
    """
    require(payload, ["settlement_id", "signer_id", "reason"])
    _finance(conn, payload["signer_id"])
    settlement = get_or_404(conn, "settlement", "settlement_id",
                            payload["settlement_id"])
    if settlement["status"] != "CONFIRMED":
        raise DomainError("只有已确认结算可以冲正", 422)

    requested = payload.get("lines")
    ts = now_iso()
    reversals: list[dict] = []
    lines = conn.execute(
        "SELECT * FROM settlement_line WHERE settlement_id=?",
        (payload["settlement_id"],),
    ).fetchall()
    for line in lines:
        remaining = line["qty_kg"] - _reversed_kg(conn, line["line_id"])
        if remaining <= QTY_EPS:
            continue
        qty = remaining
        if requested:
            match = [r for r in requested
                     if r["harvest_id"] == line["harvest_id"]
                     and r["grade"] == line["grade"]]
            if not match:
                continue
            qty = float(match[0]["qty_kg"])
        if qty > remaining + QTY_EPS:
            raise Conflict(
                f"{line['harvest_id']}/{line['grade']} 可冲正重量为 "
                f"{remaining}kg，申请 {qty}kg")
        ratio = as_decimal(qty) / as_decimal(line["qty_kg"])
        amount = (as_decimal(line["amount"]) * ratio).quantize(Decimal("0.01"))
        original = conn.execute(
            "SELECT entry_id FROM ledger_entry WHERE line_id=? "
            "AND direction='NORMAL'", (line["line_id"],),
        ).fetchone()
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger_entry WHERE line_id=? "
            "AND direction='REVERSAL'", (line["line_id"],),
        ).fetchone()["c"]
        entry_id = f"LE-REV-{line['line_id']}-{n + 1}"
        conn.execute(
            "INSERT INTO ledger_entry(entry_id, settlement_id, line_id, "
            "harvest_id, farmer_id, direction, amount, grade, qty_kg, "
            "unit_price, premium_per_kg, reason, ref_type, ref_id, "
            "reverses_entry_id, created_by, created_at) "
            "VALUES (?,?,?,?,?,'REVERSAL',?,?,?,?,?,?,?,?,?,?,?)",
            (entry_id, payload["settlement_id"], line["line_id"],
             line["harvest_id"], settlement["farmer_id"], money(-amount),
             line["grade"], -round(qty, 6), line["unit_price"],
             line["premium_per_kg"], payload["reason"],
             "SALES_RETURN" if payload.get("return_id") else "MANUAL",
             payload.get("return_id"), original["entry_id"],
             payload["signer_id"], ts))
        reversals.append({"line_id": line["line_id"], "qty_kg": round(qty, 6),
                          "amount": money(-amount)})

    if not reversals:
        raise DomainError("没有可冲正的余额", 422)

    still_open = conn.execute(
        "SELECT COUNT(*) AS c FROM settlement_line l WHERE "
        "l.qty_kg - (SELECT COALESCE(SUM(-qty_kg),0) FROM ledger_entry e "
        "WHERE e.line_id=l.line_id AND e.direction='REVERSAL') > 1e-6 "
        "AND l.settlement_id=?", (payload["settlement_id"],),
    ).fetchone()["c"]
    if still_open == 0:
        conn.execute(
            "UPDATE settlement SET status='REVERSED' WHERE settlement_id=?",
            (payload["settlement_id"],))
    record_event(conn, "settlement", payload["settlement_id"],
                 "settlement.reverse",
                 {"settlement_id": payload["settlement_id"],
                  "reversals": reversals, "reason": payload["reason"]})
    return {"settlement_id": payload["settlement_id"],
            "status": "REVERSED" if still_open == 0 else "CONFIRMED",
            "reversals": reversals}


def explain(conn, settlement_id: str) -> dict:
    """说明每笔结算为何得到该金额：合同版本、单价、帮扶加价、冲正、净额。"""
    settlement = get_or_404(conn, "settlement", "settlement_id",
                            settlement_id)
    contract = get_or_404(conn, "contract", "contract_id",
                          settlement["contract_id"])
    lines_out: list[dict] = []
    for line in conn.execute(
            "SELECT * FROM settlement_line WHERE settlement_id=?",
            (settlement_id,)).fetchall():
        reversed_rows = conn.execute(
            "SELECT entry_id, amount, qty_kg, reason, ref_id, created_at "
            "FROM ledger_entry WHERE line_id=? AND direction='REVERSAL' "
            "ORDER BY created_at", (line["line_id"],)).fetchall()
        reversed_kg = sum(-r["qty_kg"] for r in reversed_rows)
        reversed_amt = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS a FROM ledger_entry "
            "WHERE line_id=? AND direction='REVERSAL'",
            (line["line_id"],)).fetchone()["a"]
        lines_out.append({
            "line_id": line["line_id"],
            "harvest_id": line["harvest_id"],
            "grade": line["grade"],
            "qty_kg": line["qty_kg"],
            "unit_price": line["unit_price"],
            "support_premium_per_kg": line["premium_per_kg"],
            "formula": (f"{line['qty_kg']}kg × ({line['unit_price']} + "
                        f"{line['premium_per_kg']}) = {line['amount']}"),
            "amount": line["amount"],
            "reversed_kg": round(reversed_kg, 6),
            "reversed_amount": money(as_decimal(reversed_amt)),
            "net_amount": money(as_decimal(line["amount"])
                                + as_decimal(reversed_amt)),
            "reversals": [dict(r) for r in reversed_rows],
        })
    ledger = conn.execute(
        "SELECT direction, COALESCE(SUM(amount),0) AS total "
        "FROM ledger_entry WHERE settlement_id=? GROUP BY direction",
        (settlement_id,),
    ).fetchall()
    totals = {r["direction"]: r["total"] for r in ledger}
    return {
        "settlement_id": settlement_id,
        "farmer_id": settlement["farmer_id"],
        "period": settlement["period"],
        "status": settlement["status"],
        "contract": {"contract_id": contract["contract_id"],
                     "version": contract["version"],
                     "effective_from": contract["effective_from"],
                     "grade_prices": json.loads(contract["grade_prices_json"]),
                     "support_premium_per_kg":
                         contract["support_premium_per_kg"]},
        "lines": lines_out,
        "original_amount": totals.get("NORMAL", 0),
        "reversed_amount": totals.get("REVERSAL", 0),
        "net_payable": money(as_decimal(totals.get("NORMAL", 0))
                             + as_decimal(totals.get("REVERSAL", 0))),
    }


def farmer_balance(conn, farmer_id: str) -> dict:
    get_or_404(conn, "farmer", "farmer_id", farmer_id)
    rows = conn.execute(
        "SELECT direction, COALESCE(SUM(amount),0) AS total, "
        "COALESCE(SUM(qty_kg),0) AS kg FROM ledger_entry "
        "WHERE farmer_id=? GROUP BY direction", (farmer_id,),
    ).fetchall()
    out = {"farmer_id": farmer_id, "normal_amount": 0,
           "reversal_amount": 0, "net_payable": 0}
    for r in rows:
        if r["direction"] == "NORMAL":
            out["normal_amount"] = r["total"]
        else:
            out["reversal_amount"] = r["total"]
    out["net_payable"] = money(as_decimal(out["normal_amount"])
                               + as_decimal(out["reversal_amount"]))
    return out
