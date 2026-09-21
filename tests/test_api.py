"""HTTP API 冒烟检查：健康检查、建档、错误格式与追溯接口。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from src.api import make_server
from src.db import migrate


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = str(Path(cls.tmp.name) / "api.sqlite3")
        migrate(db_path)
        cls.server = make_server(0, db_path)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self) -> None:
        status, payload = self._request("GET", "/health")
        self.assertEqual((status, payload), (200, {"status": "ok"}))

    def test_create_and_trace_flow(self) -> None:
        status, _ = self._request("POST", "/farmers", {
            "farmer_ref": "FARM-API", "display_name": "农户乙", "base_region": "苏州",
        })
        self.assertEqual(status, 200)
        status, _ = self._request("POST", "/ponds", {
            "pond_ref": "POND-API", "farmer_ref": "FARM-API",
            "base_region": "苏州", "area_mu": 8.0, "water_body_ref": "WB-API",
        })
        self.assertEqual(status, 200)
        status, payload = self._request("GET", "/trace/POND-API")
        self.assertEqual(status, 200)
        self.assertEqual(payload["pond"]["pond_ref"], "POND-API")
        self.assertEqual(payload["farmer"]["farmer_ref"], "FARM-API")

    def test_error_format_and_unknown_route(self) -> None:
        status, payload = self._request("POST", "/ponds", {
            "pond_ref": "POND-X", "farmer_ref": "NOPE",
            "base_region": "湖州", "area_mu": 1.0, "water_body_ref": "WB-X",
        })
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")
        status, payload = self._request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_duplicate_ref_conflicts(self) -> None:
        body = {"farmer_ref": "FARM-DUP", "display_name": "农户丙", "base_region": "湖州"}
        self.assertEqual(self._request("POST", "/farmers", body)[0], 200)
        status, payload = self._request("POST", "/farmers", body)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "conflict")


if __name__ == "__main__":
    unittest.main()
