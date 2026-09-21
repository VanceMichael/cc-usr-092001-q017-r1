"""品控：五重检测、定向冻结、复检/解封/销毁的角色分离。"""

from src.domain import produce, quality, trading
from src.domain.common import DomainError, Forbidden
from tests.support import DomainCase


class FreezeTest(DomainCase):
    def _prep(self) -> str:
        self.apply_feed()
        a_batch = self.grade(hid="H-1")[0]
        # A 等品再拆两个装运子批。
        produce.split(self.conn, {
            "split_id": "S-1", "parent_batch_id": a_batch,
            "lines": [{"qty_kg": 300}, {"qty_kg": 300}]})
        return a_batch

    def _fail_inspection(self, iid: str, subject_id: str,
                         stage: str = "PRODUCT") -> None:
        quality.inspection(self.conn, {
            "inspection_id": iid, "stage": stage,
            "subject_type": "BATCH", "subject_id": subject_id,
            "result": "FAIL", "inspector_id": "P-INSP",
            "sampled_at": "2026-09-11T09:00:00+08:00"})

    def test_freeze_cascades_to_descendants(self) -> None:
        self.harvest()
        a_batch = self._prep()
        self._fail_inspection("INSP-FAIL", a_batch)
        out = quality.freeze(self.conn, {
            "freeze_id": "FZ-1", "inspection_id": "INSP-FAIL",
            "created_by": "P-SUP", "reason": "药残超标",
            "batch_ids": [a_batch]})
        # 父批 + 两个子批都在冻结范围；B 等品不受影响。
        self.assertEqual(set(out["batches"]),
                         {a_batch, "B-S-1-1", "B-S-1-2"})
        unaffected = self.conn.execute(
            "SELECT status FROM batch WHERE batch_id='B-G-1-2'").fetchone()
        self.assertEqual(unaffected["status"], "AVAILABLE")

    def test_freeze_water_body_scope(self) -> None:
        self.harvest()
        self._prep()
        quality.inspection(self.conn, {
            "inspection_id": "INSP-WF", "stage": "WATER",
            "subject_type": "WATER_BODY", "subject_id": "WB-1",
            "result": "FAIL", "inspector_id": "P-INSP",
            "sampled_at": "2026-09-11T09:00:00+08:00"})
        out = quality.freeze(self.conn, {
            "freeze_id": "FZ-W", "inspection_id": "INSP-WF",
            "created_by": "P-SUP", "reason": "水体指标异常",
            "water_body_ids": ["WB-1"]})
        # 水体下所有塘口批次（H-1 全树）都被冻结。
        self.assertIn("WB-1", out["water_bodies"])
        self.assertEqual(len(out["batches"]), 5)

    def test_freeze_requires_fail_and_supervisor(self) -> None:
        self.harvest()
        a_batch = self._prep()
        self.product_inspection("INSP-PASS", a_batch, "PASS")
        with self.assertRaises(DomainError):
            quality.freeze(self.conn, {
                "freeze_id": "FZ-X", "inspection_id": "INSP-PASS",
                "created_by": "P-SUP", "reason": "x", "batch_ids": [a_batch]})
        self._fail_inspection("INSP-FAIL2", a_batch)
        with self.assertRaises(Forbidden):
            quality.freeze(self.conn, {
                "freeze_id": "FZ-X2", "inspection_id": "INSP-FAIL2",
                "created_by": "P-MGR", "reason": "x",
                "batch_ids": [a_batch]})

    def test_release_flow_role_separation(self) -> None:
        self.harvest()
        a_batch = self._prep()
        self._fail_inspection("INSP-F", a_batch)
        quality.freeze(self.conn, {
            "freeze_id": "FZ-2", "inspection_id": "INSP-F",
            "created_by": "P-SUP", "reason": "疑似超标",
            "batch_ids": [a_batch]})
        # 复检不能由督导（冻结创建人）签。
        self.product_inspection("INSP-R1", a_batch, "PASS",
                                inspector="P-INSP2")
        with self.assertRaises(Forbidden):
            quality.retest(self.conn, {"freeze_id": "FZ-2",
                                       "signer_id": "P-SUP",
                                       "inspection_id": "INSP-R1"})
        # 检测员复检 PASS。
        quality.retest(self.conn, {"freeze_id": "FZ-2",
                                   "signer_id": "P-INSP2",
                                   "inspection_id": "INSP-R1"})
        # 解封必须 reviewer，且不能与复检同人（这里 INSP2 是 inspector）。
        with self.assertRaises(Forbidden):
            quality.release(self.conn, {"freeze_id": "FZ-2",
                                        "signer_id": "P-INSP2"})
        out = quality.release(self.conn, {"freeze_id": "FZ-2",
                                          "signer_id": "P-REV"})
        self.assertEqual(out["status"], "RELEASED")
        # 父批已全量拆分（EXHAUSTED）不在库；两个子批恢复可用。
        for child in ("B-S-1-1", "B-S-1-2"):
            status = self.conn.execute(
                "SELECT status FROM batch WHERE batch_id=?", (child,)).fetchone()
            self.assertEqual(status["status"], "AVAILABLE")

    def test_release_requires_passing_retest(self) -> None:
        self.harvest()
        a_batch = self._prep()
        self._fail_inspection("INSP-D", a_batch)
        quality.freeze(self.conn, {
            "freeze_id": "FZ-3", "inspection_id": "INSP-D",
            "created_by": "P-SUP", "reason": "超标",
            "batch_ids": [a_batch]})
        self.product_inspection("INSP-RF", a_batch, "FAIL",
                                inspector="P-INSP2")
        quality.retest(self.conn, {"freeze_id": "FZ-3",
                                   "signer_id": "P-INSP2",
                                   "inspection_id": "INSP-RF"})
        with self.assertRaises(DomainError):
            quality.release(self.conn, {"freeze_id": "FZ-3",
                                        "signer_id": "P-REV"})

    def test_destroy_flow_releases_hold_and_blocks_batch(self) -> None:
        self.harvest()
        a_batch = self._prep()
        child = "B-S-1-1"
        # 先把一个子批分配进柜，随后该批因检测失败冻结销毁。
        trading.create_order(self.conn, {
            "order_id": "O-1", "customer_ref": "BUYER-JP",
            "sales_id": "P-SALES", "grade_required": "A",
            "containers_count": 1, "kg_per_container": 1000,
            "conditions_json": '{"require_organic": true}'})
        trading.allocate(self.conn, {
            "allocation_id": "AL-1", "container_id": "C-O-1-1",
            "sales_id": "P-SALES",
            "lines": [{"batch_id": child, "qty_kg": 100}]})
        self._fail_inspection("INSP-DD", child)
        quality.freeze(self.conn, {
            "freeze_id": "FZ-4", "inspection_id": "INSP-DD",
            "created_by": "P-SUP", "reason": "超标",
            "batch_ids": [child]})
        self.product_inspection("INSP-RD", child, "FAIL",
                                inspector="P-INSP2")
        quality.retest(self.conn, {"freeze_id": "FZ-4",
                                   "signer_id": "P-INSP2",
                                   "inspection_id": "INSP-RD"})
        out = quality.destroy(self.conn, {"freeze_id": "FZ-4",
                                          "signer_id": "P-REV"})
        self.assertGreater(out["destroyed_kg"], 0)
        # 批次被销毁；预留分配被释放，柜容量回到 1000。
        b = self.conn.execute("SELECT * FROM batch WHERE batch_id=?",
                              (child,)).fetchone()
        self.assertEqual(b["status"], "DESTROYED")
        alloc = self.conn.execute(
            "SELECT status FROM batch_allocation WHERE allocation_id='AL-1'"
        ).fetchone()
        self.assertEqual(alloc["status"], "RELEASED")
        container = self.conn.execute(
            "SELECT * FROM order_container WHERE container_id='C-O-1-1'"
        ).fetchone()
        self.assertEqual(container["allocated_kg"], 0)

    def test_frozen_water_body_blocks_harvest(self) -> None:
        quality.inspection(self.conn, {
            "inspection_id": "INSP-WB", "stage": "WATER",
            "subject_type": "WATER_BODY", "subject_id": "WB-1",
            "result": "FAIL", "inspector_id": "P-INSP",
            "sampled_at": "2026-09-09T09:00:00+08:00"})
        quality.freeze(self.conn, {
            "freeze_id": "FZ-WB", "inspection_id": "INSP-WB",
            "created_by": "P-SUP", "reason": "水质调查",
            "water_body_ids": ["WB-1"]})
        with self.assertRaises(DomainError):
            self.harvest()

    def test_frozen_batch_cannot_allocate_or_ship(self) -> None:
        self.harvest()
        a_batch = self._prep()
        self._fail_inspection("INSP-BL", a_batch)
        quality.freeze(self.conn, {
            "freeze_id": "FZ-5", "inspection_id": "INSP-BL",
            "created_by": "P-SUP", "reason": "调查中",
            "batch_ids": [a_batch]})
        trading.create_order(self.conn, {
            "order_id": "O-BL", "customer_ref": "BUYER-EU",
            "sales_id": "P-SALES", "grade_required": "A",
            "containers_count": 1, "kg_per_container": 1000})
        with self.assertRaises(DomainError):
            trading.allocate(self.conn, {
                "allocation_id": "AL-BAD", "container_id": "C-O-BL-1",
                "sales_id": "P-SALES",
                "lines": [{"batch_id": "B-S-1-1", "qty_kg": 10}]})
