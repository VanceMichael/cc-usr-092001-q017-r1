"""生产链：有机认证门槛、禁用投入品、分级与拆分守恒。"""

from src.domain import produce
from src.domain.common import DomainError
from tests.support import DomainCase


class ProduceTest(DomainCase):
    def test_forbidden_vet_drug(self) -> None:
        from src.domain import masters
        with self.assertRaises(DomainError):
            masters.application(self.conn, {
                "application_id": "APP-BAD", "pond_id": "POND-1",
                "material_code": "DRUG-X", "qty": 2, "unit": "kg",
                "applied_at": "2026-05-12T08:00:00+08:00"})

    def test_harvest_requires_organic_cert(self) -> None:
        self.conn.execute(
            "UPDATE certification SET status='SUSPENDED' WHERE cert_id='CERT-WB-1'")
        with self.assertRaises(DomainError):
            self.harvest()

    def test_grade_conservation(self) -> None:
        self.apply_feed()
        self.harvest(qty=1000)
        children = self.grade()
        self.assertEqual(
            [self.conn.execute(
                "SELECT grade, qty_kg FROM batch WHERE batch_id=? ",
                (cid,)).fetchone()[1] for cid in children], [600.0, 400.0])
        parent = self.conn.execute(
            "SELECT * FROM batch WHERE batch_id='B-H-1'").fetchone()
        self.assertEqual(parent["split_out_kg"], 1000.0)
        self.assertEqual(parent["status"], "EXHAUSTED")

    def test_grade_over_weight_rejected(self) -> None:
        self.apply_feed()
        self.harvest(qty=100)
        with self.assertRaises(DomainError):
            self.grade(lines=[{"grade": "A", "qty_kg": 60},
                              {"grade": "B", "qty_kg": 41}])

    def test_split_conservation(self) -> None:
        self.apply_feed()
        self.harvest(qty=1000)
        children = self.grade()
        a_batch = children[0]
        out = produce.split(self.conn, {
            "split_id": "S-1", "parent_batch_id": a_batch,
            "lines": [{"qty_kg": 200}, {"qty_kg": 400}]})
        self.assertEqual(len(out["batches"]), 2)
        self.assertEqual(out["total_kg"], 600.0)
        # 父批 A(600) 拆出 600 后耗尽，子批可独立使用。
        parent = self.conn.execute(
            "SELECT * FROM batch WHERE batch_id=?", (a_batch,)).fetchone()
        self.assertEqual(parent["split_out_kg"], 600.0)

    def test_split_too_much_rejected(self) -> None:
        self.apply_feed()
        self.harvest(qty=1000)
        a_batch = self.grade()[0]
        with self.assertRaises(DomainError):
            produce.split(self.conn, {
                "split_id": "S-2", "parent_batch_id": a_batch,
                "lines": [{"qty_kg": 601}]})
