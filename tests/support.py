"""测试辅助：每个用例一个临时数据库与一套基础主数据。"""

import os
import tempfile
import unittest
from pathlib import Path

from src.db import connect, initialize
from src.domain import masters


class DomainCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.sqlite3")
        os.environ["DATABASE_PATH"] = self.db_path
        self.conn = connect(self.db_path)
        initialize(self.conn)
        self.conn.execute("BEGIN IMMEDIATE")
        self._seed()

    def tearDown(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except Exception:
            pass
        self.conn.close()
        self.tmp.cleanup()

    def _seed(self) -> None:
        for pid, name, role in [
            ("P-MGR", "管理", "manager"),
            ("P-INSP", "检测员甲", "inspector"),
            ("P-INSP2", "检测员乙", "inspector"),
            ("P-REV", "审核人", "reviewer"),
            ("P-SUP", "督导", "supervisor"),
            ("P-TECH", "技术员", "technician"),
            ("P-SALES", "外销员", "sales"),
            ("P-FIN", "财务", "finance"),
        ]:
            masters.person(self.conn, {"person_id": pid, "name": name,
                                       "role": role})
        masters.base(self.conn, {"base_id": "BASE-1", "name": "太湖联合基地",
                                 "region": "湖州"})
        masters.farmer(self.conn,
                       {"farmer_id": "F-1", "name": "养殖户老周",
                        "base_id": "BASE-1"})
        masters.water_body(self.conn,
                           {"water_body_id": "WB-1", "base_id": "BASE-1",
                            "name": "一号水体"})
        masters.pond(self.conn,
                     {"pond_id": "POND-1", "code": "HZ-001",
                      "water_body_id": "WB-1", "farmer_id": "F-1",
                      "area_mu": 12.5})
        masters.seed_lot(self.conn,
                         {"seed_lot_id": "SEED-1", "species": "白玉蟹",
                          "supplier_ref": "SUP-X", "qty_seed": 50000,
                          "produced_at": "2026-03-01T08:00:00+08:00",
                          "cert_ref": "CERT-SEED-1"})
        masters.stocking(self.conn,
                         {"stocking_id": "STK-1", "seed_lot_id": "SEED-1",
                          "pond_id": "POND-1", "qty": 20000,
                          "stocked_at": "2026-03-02T08:00:00+08:00"})
        for code, name, cat, allowed in [
            ("FEED-A", "有机配合饲料", "FEED", 1),
            ("DRUG-X", "某种抗菌药", "VET_DRUG", 0),
        ]:
            masters.material(self.conn,
                             {"material_code": code, "name": name,
                              "category": cat, "organic_allowed": allowed})
        masters.certification(self.conn,
                              {"cert_id": "CERT-WB-1",
                               "subject_type": "WATER_BODY",
                               "subject_id": "WB-1", "cert_no": "OR-WB-1",
                               "issuer_ref": "ORG-AGENCY",
                               "valid_from": "2026-01-01T00:00:00+08:00",
                               "valid_to": "2027-01-01T00:00:00+08:00"})

    # -- 便捷构造 ----------------------------------------------------------
    def apply_feed(self, app_id: str = "APP-1") -> None:
        masters.application(self.conn, {
            "application_id": app_id, "pond_id": "POND-1",
            "material_code": "FEED-A", "qty": 100, "unit": "kg",
            "applied_at": "2026-05-10T08:00:00+08:00",
            "recorder_id": "P-TECH"})

    def harvest(self, hid: str = "H-1", qty: float = 1000,
                at: str = "2026-09-10T06:00:00+08:00") -> str:
        from src.domain import produce
        return produce.harvest(self.conn, {
            "harvest_id": hid, "pond_id": "POND-1", "farmer_id": "F-1",
            "qty_kg": qty, "caught_at": at})["batch_id"]

    def grade(self, grade_id: str = "G-1", hid: str = "H-1",
              lines=None) -> list[str]:
        from src.domain import produce
        lines = lines or [{"grade": "A", "qty_kg": 600},
                          {"grade": "B", "qty_kg": 400}]
        return produce.grade(self.conn, {"grade_id": grade_id,
                                         "harvest_id": hid,
                                         "lines": lines})["batches"]

    def product_inspection(self, iid: str, batch_id: str, result: str,
                           stage: str = "PRODUCT",
                           inspector: str = "P-INSP") -> None:
        from src.domain import quality
        quality.inspection(self.conn, {
            "inspection_id": iid, "stage": stage,
            "subject_type": "BATCH", "subject_id": batch_id,
            "result": result, "inspector_id": inspector,
            "sampled_at": "2026-09-11T08:00:00+08:00"})
