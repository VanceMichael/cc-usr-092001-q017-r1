"""出口贸易：条件联动、不超卖、发运回执、退货。"""

import os
import threading
import unittest

from src.db import connect, initialize, transaction
from src.domain import produce, quality, trading
from src.domain.common import Conflict, DomainError
from tests.support import DomainCase


class TradingTest(DomainCase):
    def _stock(self) -> tuple[str, str]:
        """起捕 1000kg：A600/B400，返回 A、B 子批。"""
        self.apply_feed()
        self.harvest()
        a_batch, b_batch = self.grade()
        return a_batch, b_batch

    def _order(self, oid: str = "O-1", target: float = 500,
               grade: str = "A", conditions: str = "{}") -> None:
        trading.create_order(self.conn, {
            "order_id": oid, "customer_ref": "BUYER-JP",
            "sales_id": "P-SALES", "grade_required": grade,
            "containers_count": 1, "kg_per_container": target,
            "conditions_json": conditions})

    def test_allocation_and_capacity(self) -> None:
        a_batch, _ = self._stock()
        self._order()
        trading.allocate(self.conn, {
            "allocation_id": "AL-1", "container_id": "C-O-1-1",
            "sales_id": "P-SALES",
            "lines": [{"batch_id": a_batch, "qty_kg": 500}]})
        avail = trading.order_availability(self.conn, "O-1")
        self.assertEqual(avail["open_kg"], 0)
        # 超柜量分配被拒。
        with self.assertRaises(Conflict):
            trading.allocate(self.conn, {
                "allocation_id": "AL-2", "container_id": "C-O-1-1",
                "sales_id": "P-SALES",
                "lines": [{"batch_id": a_batch, "qty_kg": 101}]})

    def test_grade_condition_blocks_mismatch(self) -> None:
        _, b_batch = self._stock()
        self._order(grade="A")
        with self.assertRaises(DomainError):
            trading.allocate(self.conn, {
                "allocation_id": "AL-B", "container_id": "C-O-1-1",
                "sales_id": "P-SALES",
                "lines": [{"batch_id": b_batch, "qty_kg": 100}]})

    def test_product_inspection_condition(self) -> None:
        a_batch, _ = self._stock()
        self._order(conditions='{"require_inspection_stage": "PRODUCT"}')
        with self.assertRaises(DomainError):
            trading.allocate(self.conn, {
                "allocation_id": "AL-1", "container_id": "C-O-1-1",
                "sales_id": "P-SALES",
                "lines": [{"batch_id": a_batch, "qty_kg": 100}]})
        self.product_inspection("INSP-A", a_batch, "PASS")
        trading.allocate(self.conn, {
            "allocation_id": "AL-1", "container_id": "C-O-1-1",
            "sales_id": "P-SALES",
            "lines": [{"batch_id": a_batch, "qty_kg": 100}]})

    def test_ship_receipt_return_flow(self) -> None:
        a_batch, _ = self._stock()
        self._order(target=300)
        trading.allocate(self.conn, {
            "allocation_id": "AL-1", "container_id": "C-O-1-1",
            "sales_id": "P-SALES",
            "lines": [{"batch_id": a_batch, "qty_kg": 300}]})
        trading.ship(self.conn, {
            "shipment_id": "SH-1", "allocation_id": "AL-1",
            "qty_kg": 300, "shipped_at": "2026-09-15T08:00:00+08:00"})
        with self.assertRaises(DomainError):
            trading.receipt(self.conn, {
                "receipt_id": "R-1", "container_id": "C-O-1-1",
                "delivered_kg": 301, "received_by": "AGENT-JP",
                "delivered_at": "2026-09-17T08:00:00+08:00"})
        trading.receipt(self.conn, {
            "receipt_id": "R-1", "container_id": "C-O-1-1",
            "delivered_kg": 280, "received_by": "AGENT-JP",
            "delivered_at": "2026-09-17T08:00:00+08:00"})
        out = trading.register_return(self.conn, {
            "return_id": "RT-1", "receipt_id": "R-1",
            "batch_id": a_batch, "qty_kg": 20, "reason": "到货死亡超标",
            "created_by": "P-SALES",
            "created_at": "2026-09-18T08:00:00+08:00"})
        # 退货批挂接原批，可再次分配且等级沿用。
        ret_batch = out["return_batch_id"]
        rb = self.conn.execute("SELECT * FROM batch WHERE batch_id=?",
                               (ret_batch,)).fetchone()
        self.assertEqual(rb["kind"], "RETURN")
        self.assertEqual(rb["grade"], "A")
        self.assertEqual(rb["origin_batch_id"], a_batch)
        # 不能超过实际可退数量。
        with self.assertRaises(Conflict):
            trading.register_return(self.conn, {
                "return_id": "RT-2", "receipt_id": "R-1",
                "batch_id": a_batch, "qty_kg": 281, "reason": "x",
                "created_by": "P-SALES",
                "created_at": "2026-09-18T09:00:00+08:00"})


class ConcurrencyTest(unittest.TestCase):
    """两个线程同时争抢同一批库存，只能有一个成功，绝不超卖。"""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path
        from src.domain import masters
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "cc.sqlite3")
        os.environ["DATABASE_PATH"] = self.db_path
        conn = connect(self.db_path)
        initialize(conn)
        conn.execute("BEGIN IMMEDIATE")
        for pid, role in [("P-SALES", "sales")]:
            masters.person(conn, {"person_id": pid, "name": "外销",
                                  "role": role})
        masters.base(conn, {"base_id": "B-1", "name": "基地",
                            "region": "湖州"})
        masters.farmer(conn, {"farmer_id": "F-1", "name": "农户",
                              "base_id": "B-1"})
        masters.water_body(conn, {"water_body_id": "WB-1", "base_id": "B-1",
                                  "name": "水体"})
        masters.pond(conn, {"pond_id": "P-1", "code": "C-1",
                            "water_body_id": "WB-1", "farmer_id": "F-1",
                            "area_mu": 5})
        masters.certification(conn, {
            "cert_id": "C-1", "subject_type": "WATER_BODY",
            "subject_id": "WB-1", "cert_no": "N", "issuer_ref": "I",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00"})
        produce.harvest(conn, {"harvest_id": "H-1", "pond_id": "P-1",
                               "farmer_id": "F-1", "qty_kg": 100,
                               "caught_at": "2026-09-10T06:00:00+08:00"})
        produce.grade(conn, {"grade_id": "G-1", "harvest_id": "H-1",
                             "lines": [{"grade": "A", "qty_kg": 100}]})
        for oid in ("O-1", "O-2"):
            trading.create_order(conn, {
                "order_id": oid, "customer_ref": "BUYER",
                "sales_id": "P-SALES", "grade_required": "A",
                "containers_count": 1, "kg_per_container": 100})
        conn.execute("COMMIT")
        conn.close()
        self.batch = "B-G-1-1"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_concurrent_allocation_does_not_oversell(self) -> None:
        barrier = threading.Barrier(2)
        results: list[str] = []

        def worker(order_id: str, alloc_id: str) -> None:
            conn = connect(self.db_path)
            try:
                barrier.wait()
                with transaction(conn):
                    trading.allocate(conn, {
                        "allocation_id": alloc_id,
                        "container_id": f"C-{order_id}-1",
                        "sales_id": "P-SALES",
                        "lines": [{"batch_id": self.batch, "qty_kg": 100}]})
                results.append("OK")
            except Conflict:
                results.append("CONFLICT")
            finally:
                conn.close()

        t1 = threading.Thread(target=worker, args=("O-1", "AL-1"))
        t2 = threading.Thread(target=worker, args=("O-2", "AL-2"))
        t1.start(); t2.start()
        t1.join(); t2.join()
        self.assertEqual(sorted(results), ["CONFLICT", "OK"])
        conn = connect(self.db_path)
        row = conn.execute(
            "SELECT allocated_kg, qty_kg FROM batch WHERE batch_id=?",
            (self.batch,)).fetchone()
        self.assertEqual(row["allocated_kg"], 100)
        self.assertLessEqual(row["allocated_kg"], row["qty_kg"])
        conn.close()
