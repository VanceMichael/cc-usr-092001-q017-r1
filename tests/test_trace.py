"""追溯：市场样品反向到塘口/投入/检测，全链一致性核对。"""

from src.domain import produce, quality, settlement, trading, trace
from tests.support import DomainCase


class TraceTest(DomainCase):
    def _full_chain(self) -> str:
        """塘口 -> 投入/指导 -> 起捕 -> 分级 -> 拆分 -> 检测 -> 出口 -> 回执。"""
        self.apply_feed()
        from src.domain import masters
        masters.guidance(self.conn, {
            "guidance_id": "GD-1", "pond_id": "POND-1",
            "technician_id": "P-TECH", "topic": "蜕壳期水质管理",
            "occurred_at": "2026-06-01T08:00:00+08:00"})
        self.harvest()
        a_batch, _ = self.grade()
        leaf = produce.split(self.conn, {
            "split_id": "S-1", "parent_batch_id": a_batch,
            "lines": [{"qty_kg": 300}, {"qty_kg": 300}]})["batches"][0]
        self.product_inspection("INSP-P1", leaf, "PASS")
        trading.create_order(self.conn, {
            "order_id": "O-1", "customer_ref": "BUYER-JP",
            "sales_id": "P-SALES", "grade_required": "A",
            "containers_count": 1, "kg_per_container": 300,
            "conditions_json": '{"require_inspection_stage": "PRODUCT",'
                               ' "require_organic": true}'})
        trading.allocate(self.conn, {
            "allocation_id": "AL-1", "container_id": "C-O-1-1",
            "sales_id": "P-SALES",
            "lines": [{"batch_id": leaf, "qty_kg": 300}]})
        trading.ship(self.conn, {
            "shipment_id": "SH-1", "allocation_id": "AL-1",
            "qty_kg": 300, "shipped_at": "2026-09-15T08:00:00+08:00"})
        trading.receipt(self.conn, {
            "receipt_id": "R-1", "container_id": "C-O-1-1",
            "delivered_kg": 300, "received_by": "AGENT-JP",
            "delivered_at": "2026-09-17T08:00:00+08:00"})
        produce.market_sample(self.conn, {
            "sample_id": "MS-1", "batch_id": leaf,
            "market_ref": "大阪中央批发市场",
            "sampled_at": "2026-09-20T08:00:00+08:00"})
        return leaf

    def test_sample_traces_back_to_pond(self) -> None:
        leaf = self._full_chain()
        report = trace.trace_sample(self.conn, "MS-1")
        self.assertEqual(report["origin"]["pond"]["code"], "HZ-001")
        self.assertEqual(report["origin"]["water_body"]["name"], "一号水体")
        self.assertEqual(report["origin"]["farmer"]["name"], "养殖户老周")
        self.assertEqual(report["origin"]["seed_lot_id"], "SEED-1")
        self.assertEqual(report["queried_batch_id"], leaf)
        # 接受过的投入、指导与检测都在档案中。
        apps = report["pond_records"]["applications"]
        self.assertEqual([a["material_code"] for a in apps], ["FEED-A"])
        self.assertEqual(len(report["pond_records"]["guidance"]), 1)
        self.assertTrue(any(i["result"] == "PASS"
                            for i in report["product_inspections"]))
        self.assertEqual(report["sample"]["market_ref"], "大阪中央批发市场")
        # 流向可追到柜与客户回执。
        self.assertEqual(report["flows"][0]["customer_ref"], "BUYER-JP")
        self.assertEqual(report["flows"][0]["receipt_id"], "R-1")

    def test_consistency_report_clean(self) -> None:
        self._full_chain()
        report = trace.consistency_report(self.conn)
        self.assertTrue(report["ok"], report["issues"])

    def test_consistency_detects_return_and_reversal(self) -> None:
        self._full_chain()
        leaf = "B-S-1-1"
        trading.register_return(self.conn, {
            "return_id": "RT-1", "receipt_id": "R-1",
            "batch_id": leaf, "qty_kg": 30, "reason": "死亡超标",
            "created_by": "P-SALES",
            "created_at": "2026-09-18T08:00:00+08:00"})
        report = trace.trace_sample(self.conn, "MS-1")
        self.assertEqual(len(report["returns"]), 1)
        consistency = trace.consistency_report(self.conn)
        self.assertTrue(consistency["ok"], consistency["issues"])

    def test_settled_chain_explains_money(self) -> None:
        self._full_chain()
        settlement.create_contract(self.conn, {
            "contract_id": "CT-1", "farmer_id": "F-1",
            "grade_prices": {"A": 120, "B": 80},
            "support_premium_per_kg": 10,
            "effective_from": "2026-01-01T00:00:00+08:00"})
        settlement.generate(self.conn, {
            "settlement_id": "ST-1", "farmer_id": "F-1",
            "period": "2026-09", "created_by": "P-FIN"})
        settlement.confirm(self.conn, {"settlement_id": "ST-1",
                                       "signer_id": "P-FIN"})
        # 管理方同时拿得到"来自哪片塘"与"为何得到该金额"。
        trace_report = trace.trace_sample(self.conn, "MS-1")
        money_report = settlement.explain(self.conn, "ST-1")
        self.assertEqual(trace_report["origin"]["pond"]["code"], "HZ-001")
        self.assertEqual(money_report["net_payable"], 114000.0)
        self.assertTrue(trace.consistency_report(self.conn)["ok"])
