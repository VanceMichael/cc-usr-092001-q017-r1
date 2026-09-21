"""领域服务行为检查：守恒、冻结签署、并发分配、结算冲正与追溯。"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from src import services
from src.db import connect, migrate
from src.services import DomainError

T0 = "2026-09-01T08:00:00+08:00"
T1 = "2026-09-10T09:00:00+08:00"
T2 = "2026-09-15T10:00:00+08:00"
T3 = "2026-09-18T11:00:00+08:00"
T4 = "2026-09-19T12:00:00+08:00"


class ServiceTestBase(unittest.TestCase):
    """准备一套基础事实：养殖户、塘口、合同、苗种、投苗、起捕、分级。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.sqlite3")
        migrate(self.db_path)
        self.conn = connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.addCleanup(self.tmp.cleanup)

        services.create_farmer(
            self.conn, farmer_ref="FARM-1", display_name="合作农户甲",
            base_region="湖州", assist_tier="assisted",
        )
        services.create_pond(
            self.conn, pond_ref="POND-1", farmer_ref="FARM-1",
            base_region="湖州", area_mu=12.5, water_body_ref="WB-1",
        )
        services.create_contract(
            self.conn, contract_ref="CT-1", version=1, farmer_ref="FARM-1",
            effective_from="2026-01-01T00:00:00+08:00",
            effective_to="2026-12-31T23:59:59+08:00",
            prices=[
                {"grade": "A", "unit_price_cents": 10000, "assist_cents": 500},
                {"grade": "B", "unit_price_cents": 6000, "assist_cents": 0},
            ],
        )
        services.create_seedling_batch(
            self.conn, seed_batch_ref="SEED-1", supplied_by="龙头企业",
            quantity=10000, supplied_at=T0, digest="sha256:seed",
        )
        services.stock_pond(
            self.conn, stock_ref="STK-1", seed_batch_ref="SEED-1",
            pond_ref="POND-1", quantity=5000, occurred_at=T0,
        )

    def harvest_and_grade(self) -> None:
        services.create_harvest(
            self.conn, batch_ref="HB-1", pond_ref="POND-1",
            gross_weight_g=100_000, occurred_at=T1, actor_ref="OP-1",
        )
        services.grade_harvest(
            self.conn, batch_ref="HB-1", actor_ref="OP-1", occurred_at=T1,
            lots=[
                {"lot_ref": "LOT-A", "grade": "A", "weight_g": 60_000},
                {"lot_ref": "LOT-B", "grade": "B", "weight_g": 40_000},
            ],
        )


class ConservationTest(ServiceTestBase):
    def test_stocking_must_not_exceed_seedling_batch(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            services.stock_pond(
                self.conn, stock_ref="STK-2", seed_batch_ref="SEED-1",
                pond_ref="POND-1", quantity=6000, occurred_at=T0,
            )
        self.assertEqual(ctx.exception.code, "insufficient_seedlings")

    def test_grading_must_conserve_weight(self) -> None:
        services.create_harvest(
            self.conn, batch_ref="HB-1", pond_ref="POND-1",
            gross_weight_g=100_000, occurred_at=T1, actor_ref="OP-1",
        )
        with self.assertRaises(DomainError) as ctx:
            services.grade_harvest(
                self.conn, batch_ref="HB-1", actor_ref="OP-1", occurred_at=T1,
                lots=[{"lot_ref": "LOT-A", "grade": "A", "weight_g": 60_000}],
            )
        self.assertEqual(ctx.exception.code, "conservation_violation")

    def test_grading_splits_and_stays_traceable(self) -> None:
        self.harvest_and_grade()
        rows = self.conn.execute(
            "SELECT SUM(weight_g) AS w, SUM(available_g) AS a FROM grade_lot WHERE batch_ref='HB-1'"
        ).fetchone()
        self.assertEqual((rows["w"], rows["a"]), (100_000, 100_000))


class FreezeFlowTest(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.harvest_and_grade()
        result = services.record_inspection(
            self.conn, inspection_ref="INS-1", scope_type="grade_lot", scope_ref="LOT-B",
            round=3, result="fail", inspector_ref="QC-1", occurred_at=T2,
            digest="sha256:report",
        )
        self.pond_freeze, self.batch_freeze = result["freezes"]

    def test_fail_freezes_only_related_water_and_batches(self) -> None:
        pond = self.conn.execute("SELECT status FROM pond WHERE pond_ref='POND-1'").fetchone()
        lots = self.conn.execute(
            "SELECT lot_ref, status FROM grade_lot ORDER BY lot_ref"
        ).fetchall()
        self.assertEqual(pond["status"], "frozen")
        self.assertEqual([(l["lot_ref"], l["status"]) for l in lots],
                         [("LOT-A", "frozen"), ("LOT-B", "frozen")])
        # 关联批次被冻结后禁止分配
        services.create_export_order(
            self.conn, order_ref="ORD-1", buyer_ref="BUYER-1", containers=1,
            total_quantity_g=50_000, quality_grades=["A"],
        )
        with self.assertRaises(DomainError) as ctx:
            services.allocate_to_order(
                self.conn, alloc_ref="AL-1", order_ref="ORD-1", lot_ref="LOT-A",
                quantity_g=1000, actor_ref="OP-2", allocated_at=T3,
            )
        self.assertEqual(ctx.exception.code, "insufficient_stock")

    def test_release_requires_retest_and_distinct_roles(self) -> None:
        # 未复检直接解封 → 拒绝
        with self.assertRaises(DomainError) as ctx:
            services.record_freeze_action(
                self.conn, freeze_ref=self.batch_freeze, action="release",
                actor_ref="QM-1", role="quality_manager", occurred_at=T3,
            )
        self.assertEqual(ctx.exception.code, "retest_required")
        # 角色不符 → 拒绝（复检必须由检测员签署）
        with self.assertRaises(DomainError) as ctx:
            services.record_freeze_action(
                self.conn, freeze_ref=self.batch_freeze, action="retest",
                actor_ref="QM-1", role="quality_manager", occurred_at=T3, result="pass",
            )
        self.assertEqual(ctx.exception.code, "forbidden_role")
        # 检测员复检合格 → 质量经理解封
        services.record_freeze_action(
            self.conn, freeze_ref=self.batch_freeze, action="retest",
            actor_ref="QC-2", role="inspector", occurred_at=T3, result="pass",
        )
        services.record_freeze_action(
            self.conn, freeze_ref=self.batch_freeze, action="release",
            actor_ref="QM-1", role="quality_manager", occurred_at=T4,
        )
        lots = self.conn.execute(
            "SELECT status FROM grade_lot ORDER BY lot_ref"
        ).fetchall()
        self.assertEqual([l["status"] for l in lots], ["available", "available"])

    def test_destroy_requires_destruction_officer_and_batch_scope(self) -> None:
        # 水体冻结不能销毁
        with self.assertRaises(DomainError) as ctx:
            services.record_freeze_action(
                self.conn, freeze_ref=self.pond_freeze, action="destroy",
                actor_ref="DO-1", role="destruction_officer", occurred_at=T3,
            )
        self.assertEqual(ctx.exception.code, "invalid_scope")
        # 批次销毁：签署角色必须是销毁专员
        with self.assertRaises(DomainError) as ctx:
            services.record_freeze_action(
                self.conn, freeze_ref=self.batch_freeze, action="destroy",
                actor_ref="QC-1", role="inspector", occurred_at=T3,
            )
        self.assertEqual(ctx.exception.code, "forbidden_role")
        services.record_freeze_action(
            self.conn, freeze_ref=self.batch_freeze, action="destroy",
            actor_ref="DO-1", role="destruction_officer", occurred_at=T3,
        )
        lots = self.conn.execute(
            "SELECT status, available_g FROM grade_lot ORDER BY lot_ref"
        ).fetchall()
        self.assertEqual([(l["status"], l["available_g"]) for l in lots],
                         [("destroyed", 0), ("destroyed", 0)])
        # 水体仍冻结，需复检合格后由质量经理解封
        pond = self.conn.execute("SELECT status FROM pond WHERE pond_ref='POND-1'").fetchone()
        self.assertEqual(pond["status"], "frozen")
        services.record_freeze_action(
            self.conn, freeze_ref=self.pond_freeze, action="retest",
            actor_ref="QC-2", role="inspector", occurred_at=T4, result="pass",
        )
        services.record_freeze_action(
            self.conn, freeze_ref=self.pond_freeze, action="release",
            actor_ref="QM-1", role="quality_manager", occurred_at=T4,
        )
        pond = self.conn.execute("SELECT status FROM pond WHERE pond_ref='POND-1'").fetchone()
        self.assertEqual(pond["status"], "active")


class AllocationTest(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.harvest_and_grade()
        services.create_export_order(
            self.conn, order_ref="ORD-1", buyer_ref="BUYER-1", containers=2,
            total_quantity_g=50_000, quality_grades=["A"],
        )

    def test_grade_condition_and_order_quota(self) -> None:
        # B 等级不在订单质量条件内
        with self.assertRaises(DomainError) as ctx:
            services.allocate_to_order(
                self.conn, alloc_ref="AL-X", order_ref="ORD-1", lot_ref="LOT-B",
                quantity_g=1000, actor_ref="OP-2", allocated_at=T2,
            )
        self.assertEqual(ctx.exception.code, "grade_not_accepted")
        # 超出订单柜量额度
        with self.assertRaises(DomainError) as ctx:
            services.allocate_to_order(
                self.conn, alloc_ref="AL-Y", order_ref="ORD-1", lot_ref="LOT-A",
                quantity_g=60_000, actor_ref="OP-2", allocated_at=T2,
            )
        self.assertEqual(ctx.exception.code, "order_overflow")

    def test_concurrent_allocation_never_oversells(self) -> None:
        """8 个线程各抢 20 公斤，批次只有 60 公斤中的 LOT-A 可用，订单额度 50 公斤：
        最终成交总额必须等于 min(批次可用, 订单额度) 的整倍数，且无一超卖。"""
        barrier = threading.Barrier(8)
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker(i: int) -> None:
            conn = connect(self.db_path)
            try:
                barrier.wait()
                services.allocate_to_order(
                    conn, alloc_ref=f"AL-{i}", order_ref="ORD-1", lot_ref="LOT-A",
                    quantity_g=20_000, actor_ref=f"OP-{i}", allocated_at=T2,
                )
                with lock:
                    outcomes.append("ok")
            except DomainError as exc:
                with lock:
                    outcomes.append(exc.code)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(outcomes.count("ok"), 2)
        self.assertEqual(outcomes.count("order_overflow"), 6)
        order = self.conn.execute(
            "SELECT allocated_g, status FROM export_order WHERE order_ref='ORD-1'"
        ).fetchone()
        lot = self.conn.execute(
            "SELECT available_g FROM grade_lot WHERE lot_ref='LOT-A'"
        ).fetchone()
        self.assertEqual(order["allocated_g"], 40_000)
        self.assertEqual(lot["available_g"], 20_000)


class SettlementTest(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.harvest_and_grade()
        services.create_export_order(
            self.conn, order_ref="ORD-1", buyer_ref="BUYER-1", containers=1,
            total_quantity_g=50_000, quality_grades=["A", "B"],
        )
        services.allocate_to_order(
            self.conn, alloc_ref="AL-1", order_ref="ORD-1", lot_ref="LOT-A",
            quantity_g=20_000, actor_ref="OP-2", allocated_at=T2,
        )
        services.record_delivery(
            self.conn, receipt_ref="RC-1", alloc_ref="AL-1",
            received_qty_g=18_000, received_at=T3, receiver_ref="PORT-1",
        )

    def test_settlement_uses_contract_version_grade_weight_and_assist(self) -> None:
        result = services.generate_settlement(
            self.conn, settlement_ref="ST-1", farmer_ref="FARM-1",
            period="2026-09", actor_ref="FIN-1",
        )
        # 18000 克 × (10000 + 500) 分/公斤 ÷ 1000 = 189000 分
        self.assertEqual(result["total_cents"], 189_000)
        explained = services.explain_settlement(self.conn, "ST-1")
        self.assertEqual(explained["settlement"]["contract_version"], 1)
        line = explained["lines"][0]
        self.assertEqual((line["grade"], line["weight_g"]), ("A", 18_000))
        self.assertEqual(line["amount_cents"], 189_000)
        # 已结算的回执不能重复结算
        with self.assertRaises(DomainError) as ctx:
            services.generate_settlement(
                self.conn, settlement_ref="ST-2", farmer_ref="FARM-1",
                period="2026-09", actor_ref="FIN-1",
            )
        self.assertEqual(ctx.exception.code, "nothing_to_settle")

    def test_return_is_recorded_as_reversal_without_touching_original(self) -> None:
        services.generate_settlement(
            self.conn, settlement_ref="ST-1", farmer_ref="FARM-1",
            period="2026-09", actor_ref="FIN-1",
        )
        original = services.explain_settlement(self.conn, "ST-1")["lines"][0]
        services.record_return(
            self.conn, line_ref="LN-R1", original_line_ref=original["line_ref"],
            weight_g=3000, reason="外商退货", actor_ref="FIN-2", occurred_at=T4,
        )
        explained = services.explain_settlement(self.conn, "ST-1")
        # 原账不变，冲正行追加，净额 = 189000 - 31500
        self.assertEqual(explained["settlement"]["total_cents"], 189_000)
        self.assertEqual(explained["reversal_total_cents"], -31_500)
        self.assertEqual(explained["net_total_cents"], 157_500)
        kinds = sorted(line["kind"] for line in explained["lines"])
        self.assertEqual(kinds, ["earning", "reversal"])
        # 累计退货不能超过原结算重量
        with self.assertRaises(DomainError) as ctx:
            services.record_return(
                self.conn, line_ref="LN-R2", original_line_ref=original["line_ref"],
                weight_g=16_000, reason="超额退货", actor_ref="FIN-2", occurred_at=T4,
            )
        self.assertEqual(ctx.exception.code, "return_exceeds")


class TraceTest(ServiceTestBase):
    def test_market_sample_traces_back_to_pond_inputs_and_inspections(self) -> None:
        services.record_input(
            self.conn, input_ref="IN-1", pond_ref="POND-1", kind="feed",
            material_ref="MAT-FEED-1", material_digest="sha256:feed",
            quantity=25.0, unit="kg", occurred_at=T0, recorded_by="TECH-1",
        )
        services.record_guidance(
            self.conn, guidance_ref="GD-1", pond_ref="POND-1",
            advisor_ref="TECH-1", summary="调水与投喂指导", occurred_at=T0,
        )
        self.harvest_and_grade()
        services.record_inspection(
            self.conn, inspection_ref="INS-OK", scope_type="pond", scope_ref="POND-1",
            round=1, result="pass", inspector_ref="QC-1", occurred_at=T1,
        )
        services.create_export_order(
            self.conn, order_ref="ORD-1", buyer_ref="BUYER-1", containers=1,
            total_quantity_g=50_000, quality_grades=["A"],
        )
        services.allocate_to_order(
            self.conn, alloc_ref="AL-1", order_ref="ORD-1", lot_ref="LOT-A",
            quantity_g=20_000, actor_ref="OP-2", allocated_at=T2,
        )
        services.record_delivery(
            self.conn, receipt_ref="RC-1", alloc_ref="AL-1",
            received_qty_g=20_000, received_at=T3, receiver_ref="PORT-1",
        )

        # 市场抽检样品按交付回执编号回溯
        trace = services.trace_subject(self.conn, "RC-1")
        self.assertEqual(trace["pond"]["pond_ref"], "POND-1")
        self.assertEqual(trace["farmer"]["farmer_ref"], "FARM-1")
        self.assertEqual(trace["seedling_batch"]["seed_batch_ref"], "SEED-1")
        self.assertEqual(trace["harvest_batch"]["batch_ref"], "HB-1")
        self.assertEqual([i["input_ref"] for i in trace["inputs"]], ["IN-1"])
        self.assertEqual([g["guidance_ref"] for g in trace["guidance"]], ["GD-1"])
        self.assertEqual([i["inspection_ref"] for i in trace["inspections"]], ["INS-OK"])
        self.assertEqual(trace["flows"][0]["receipt_ref"], "RC-1")
        # 分级批次编号同样可溯
        by_lot = services.trace_subject(self.conn, "LOT-A")
        self.assertEqual(by_lot["pond"]["pond_ref"], "POND-1")
        with self.assertRaises(DomainError):
            services.trace_subject(self.conn, "NOPE")


if __name__ == "__main__":
    unittest.main()
