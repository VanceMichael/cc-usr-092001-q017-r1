"""结算：合同版本、帮扶加价、可解释金额、冲正不改原账。"""

import sqlite3

from src.domain import settlement
from src.domain.common import DomainError, Forbidden
from tests.support import DomainCase


class SettlementTest(DomainCase):
    def _contract(self, cid: str = "CT-1", version=None,
                  prices=None, premium: float = 10) -> dict:
        return settlement.create_contract(self.conn, {
            "contract_id": cid, "farmer_id": "F-1",
            "version": version,
            "grade_prices": prices or {"A": 120, "B": 80},
            "support_premium_per_kg": premium,
            "effective_from": "2026-01-01T00:00:00+08:00"})

    def _graded_harvest(self) -> None:
        self.apply_feed()
        self.harvest()
        self.grade()

    def test_generate_explain_and_confirm(self) -> None:
        self._graded_harvest()
        self._contract()
        out = settlement.generate(self.conn, {
            "settlement_id": "ST-1", "farmer_id": "F-1",
            "period": "2026-09", "created_by": "P-FIN"})
        self.assertEqual(out["contract_version"], 1)
        # A: 600×(120+10)=78000；B: 400×(80+10)=36000。
        self.assertEqual(out["total"], 114000.0)
        settlement.confirm(self.conn, {"settlement_id": "ST-1",
                                       "signer_id": "P-FIN"})
        explanation = settlement.explain(self.conn, "ST-1")
        formulas = {l["grade"]: l["formula"] for l in explanation["lines"]}
        self.assertIn("600.0kg × (120.0 + 10.0) = 78000.0", formulas["A"])
        self.assertEqual(explanation["net_payable"], 114000.0)

    def test_only_finance_role(self) -> None:
        self._graded_harvest()
        self._contract()
        with self.assertRaises(Forbidden):
            settlement.generate(self.conn, {
                "settlement_id": "ST-X", "farmer_id": "F-1",
                "period": "2026-09", "created_by": "P-MGR"})

    def test_no_duplicate_generate(self) -> None:
        self._graded_harvest()
        self._contract()
        settlement.generate(self.conn, {
            "settlement_id": "ST-1", "farmer_id": "F-1",
            "period": "2026-09", "created_by": "P-FIN"})
        with self.assertRaises(DomainError):
            settlement.generate(self.conn, {
                "settlement_id": "ST-2", "farmer_id": "F-1",
                "period": "2026-09", "created_by": "P-FIN"})

    def test_contract_version_freezes_prices(self) -> None:
        self._graded_harvest()
        self._contract(cid="CT-1", prices={"A": 120, "B": 80})
        settlement.generate(self.conn, {
            "settlement_id": "ST-1", "farmer_id": "F-1",
            "period": "2026-09", "created_by": "P-FIN"})
        settlement.confirm(self.conn, {"settlement_id": "ST-1",
                                       "signer_id": "P-FIN"})
        # 下月合同改版涨价，不影响已生成结算。
        self._contract(cid="CT-2", prices={"A": 150, "B": 90}, premium=15)
        explanation = settlement.explain(self.conn, "ST-1")
        self.assertEqual(explanation["contract"]["version"], 1)
        self.assertEqual(explanation["lines"][0]["unit_price"], 120.0)

        # 新起捕批按新版本结算。
        self.harvest(hid="H-2", qty=500, at="2026-10-08T06:00:00+08:00")
        self.grade(grade_id="G-2", hid="H-2",
                   lines=[{"grade": "A", "qty_kg": 500}])
        out = settlement.generate(self.conn, {
            "settlement_id": "ST-2", "farmer_id": "F-1",
            "period": "2026-10", "created_by": "P-FIN",
            "harvest_ids": ["H-2"]})
        self.assertEqual(out["total"], 82500.0)  # 500×(150+15)

    def test_missing_grade_price_blocks_settlement(self) -> None:
        self._graded_harvest()
        self._contract(prices={"A": 120})  # 缺 B 价
        with self.assertRaises(DomainError):
            settlement.generate(self.conn, {
                "settlement_id": "ST-1", "farmer_id": "F-1",
                "period": "2026-09", "created_by": "P-FIN"})

    def test_partial_reversal_for_return_keeps_original(self) -> None:
        self._graded_harvest()
        self._contract()
        settlement.generate(self.conn, {
            "settlement_id": "ST-1", "farmer_id": "F-1",
            "period": "2026-09", "created_by": "P-FIN"})
        settlement.confirm(self.conn, {"settlement_id": "ST-1",
                                       "signer_id": "P-FIN"})
        # 退货 20kg A 等品：只追加负向冲正。
        out = settlement.reverse(self.conn, {
            "settlement_id": "ST-1", "signer_id": "P-FIN",
            "reason": "外商退货 20kg", "return_id": "RT-1",
            "lines": [{"harvest_id": "H-1", "grade": "A", "qty_kg": 20}]})
        self.assertEqual(out["status"], "CONFIRMED")  # 部分冲正
        reversal = self.conn.execute(
            "SELECT * FROM ledger_entry WHERE direction='REVERSAL'").fetchone()
        self.assertEqual(reversal["amount"], -2600.0)  # -(20×130)
        self.assertEqual(reversal["ref_type"], "SALES_RETURN")
        explanation = settlement.explain(self.conn, "ST-1")
        self.assertEqual(explanation["net_payable"], 111400.0)
        balance = settlement.farmer_balance(self.conn, "F-1")
        self.assertEqual(balance["net_payable"], 111400.0)

        # 原账行不可修改、不可删除。
        original = self.conn.execute(
            "SELECT entry_id FROM ledger_entry WHERE direction='NORMAL' "
            "LIMIT 1").fetchone()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE ledger_entry SET amount=0 WHERE entry_id=?",
                (original["entry_id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM ledger_entry WHERE entry_id=?",
                (original["entry_id"],))

    def test_cannot_reverse_more_than_settled(self) -> None:
        self._graded_harvest()
        self._contract()
        settlement.generate(self.conn, {
            "settlement_id": "ST-1", "farmer_id": "F-1",
            "period": "2026-09", "created_by": "P-FIN"})
        settlement.confirm(self.conn, {"settlement_id": "ST-1",
                                       "signer_id": "P-FIN"})
        with self.assertRaises(Exception):
            settlement.reverse(self.conn, {
                "settlement_id": "ST-1", "signer_id": "P-FIN",
                "reason": "超额",
                "lines": [{"harvest_id": "H-1", "grade": "A",
                           "qty_kg": 601}]})
