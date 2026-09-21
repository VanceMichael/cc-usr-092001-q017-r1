"""HTTP 端到端：通过真实端口走完整业务链。"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.api import Handler


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = str(Path(self.tmp.name) / "api.sqlite3")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None,
             expect: int = 200):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers={"Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
                self.assertEqual(resp.status, expect)
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read())
            self.assertEqual(exc.code, expect, body)
            return body
        return body

    def test_health(self) -> None:
        body = self.call("GET", "/health")
        self.assertEqual(body, {"status": "ok"})

    def test_end_to_end_chain(self) -> None:
        def post(path, payload):
            return self.call("POST", path, payload)["data"]

        for pid, role in [("P-INSP", "inspector"), ("P-INSP2", "inspector"),
                          ("P-REV", "reviewer"), ("P-SUP", "supervisor"),
                          ("P-TECH", "technician"), ("P-SALES", "sales"),
                          ("P-FIN", "finance")]:
            post("/v1/persons", {"person_id": pid, "name": pid, "role": role})
        post("/v1/bases", {"base_id": "BASE-1", "name": "基地",
                           "region": "湖州"})
        post("/v1/farmers", {"farmer_id": "F-1", "name": "老周",
                             "base_id": "BASE-1"})
        post("/v1/water-bodies", {"water_body_id": "WB-1", "base_id": "BASE-1",
                                  "name": "一号水体"})
        post("/v1/ponds", {"pond_id": "POND-1", "code": "HZ-001",
                           "water_body_id": "WB-1", "farmer_id": "F-1",
                           "area_mu": 12.5})
        post("/v1/certifications", {
            "cert_id": "CERT-1", "subject_type": "WATER_BODY",
            "subject_id": "WB-1", "cert_no": "OR-1", "issuer_ref": "ORG",
            "valid_from": "2026-01-01T00:00:00+08:00",
            "valid_to": "2027-01-01T00:00:00+08:00"})
        post("/v1/materials", {"material_code": "FEED-A", "name": "饲料",
                               "category": "FEED", "organic_allowed": 1})
        post("/v1/applications", {
            "application_id": "APP-1", "pond_id": "POND-1",
            "material_code": "FEED-A", "qty": 100, "unit": "kg",
            "applied_at": "2026-05-10T08:00:00+08:00"})
        post("/v1/harvests", {"harvest_id": "H-1", "pond_id": "POND-1",
                              "farmer_id": "F-1", "qty_kg": 1000,
                              "caught_at": "2026-09-10T06:00:00+08:00"})
        grade = post("/v1/grades", {"grade_id": "G-1", "harvest_id": "H-1",
                                    "lines": [{"grade": "A", "qty_kg": 1000}]})
        a_batch = grade["batches"][0]
        post("/v1/inspections", {
            "inspection_id": "INSP-1", "stage": "PRODUCT",
            "subject_type": "BATCH", "subject_id": a_batch,
            "result": "PASS", "inspector_id": "P-INSP",
            "sampled_at": "2026-09-11T08:00:00+08:00"})
        post("/v1/export-orders", {
            "order_id": "O-1", "customer_ref": "BUYER-JP",
            "sales_id": "P-SALES", "grade_required": "A",
            "containers_count": 1, "kg_per_container": 1000,
            "conditions_json": '{"require_inspection_stage": "PRODUCT"}'})
        post("/v1/allocations", {
            "allocation_id": "AL-1", "container_id": "C-O-1-1",
            "sales_id": "P-SALES",
            "lines": [{"batch_id": a_batch, "qty_kg": 1000}]})
        post("/v1/shipments", {"shipment_id": "SH-1", "allocation_id": "AL-1",
                               "qty_kg": 1000,
                               "shipped_at": "2026-09-15T08:00:00+08:00"})
        post("/v1/receipts", {
            "receipt_id": "R-1", "container_id": "C-O-1-1",
            "delivered_kg": 1000, "received_by": "AGENT",
            "delivered_at": "2026-09-17T08:00:00+08:00"})
        post("/v1/market-samples", {
            "sample_id": "MS-1", "batch_id": a_batch,
            "market_ref": "大阪市场",
            "sampled_at": "2026-09-20T08:00:00+08:00"})
        post("/v1/contracts", {
            "contract_id": "CT-1", "farmer_id": "F-1",
            "grade_prices": {"A": 120, "B": 80},
            "support_premium_per_kg": 10,
            "effective_from": "2026-01-01T00:00:00+08:00"})
        post("/v1/settlements/generate", {
            "settlement_id": "ST-1", "farmer_id": "F-1",
            "period": "2026-09", "created_by": "P-FIN"})
        post("/v1/settlements/ST-1/confirm", {"signer_id": "P-FIN"})

        # 追溯与结算解释。
        traced = self.call("GET", "/v1/samples/MS-1/trace")["data"]
        self.assertEqual(traced["origin"]["pond"]["code"], "HZ-001")
        explain = self.call("GET", "/v1/settlements/ST-1/explain")["data"]
        self.assertEqual(explain["net_payable"], 130000.0)
        consistency = self.call("GET", "/v1/consistency")["data"]
        self.assertTrue(consistency["ok"], consistency["issues"])

        # 冻结-复检-解封角色链：错误角色 403。
        post("/v1/inspections", {
            "inspection_id": "INSP-F", "stage": "PRODUCT",
            "subject_type": "BATCH", "subject_id": a_batch,
            "result": "FAIL", "inspector_id": "P-INSP",
            "sampled_at": "2026-09-21T08:00:00+08:00"})
        bad = self.call("POST", "/v1/freezes", {
            "freeze_id": "FZ-1", "inspection_id": "INSP-F",
            "created_by": "P-FIN", "reason": "x",
            "batch_ids": [a_batch]}, expect=403)
        self.assertEqual(bad["type"], "Forbidden")
        # 该批已全部装运，冻结范围为空产品批次也允许走流程。
        frozen = post("/v1/freezes", {
            "freeze_id": "FZ-1", "inspection_id": "INSP-F",
            "created_by": "P-SUP", "reason": "市场抽检疑似不合格",
            "batch_ids": [a_batch]})
        self.assertIn(a_batch, frozen["batches"])

    def test_external_event_sequence_gap_rejected(self) -> None:
        self.call("POST", "/v1/events", {
            "event_id": "E1", "source_ref": "LAB-A", "source_sequence": 1,
            "subject_ref": "S1", "occurred_at": "2026-09-19T09:00:00+08:00",
            "payload_digest": "sha256:abc"})
        body = self.call("POST", "/v1/events", {
            "event_id": "E2", "source_ref": "LAB-A", "source_sequence": 3,
            "subject_ref": "S1", "occurred_at": "2026-09-19T10:00:00+08:00",
            "payload_digest": "sha256:def"}, expect=409)
        self.assertEqual(body["type"], "Conflict")
