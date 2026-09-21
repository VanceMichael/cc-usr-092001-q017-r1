"""基础服务与分发的最小行为检查。"""

import os
import tempfile
import unittest
from pathlib import Path

from src.api import dispatch
from src.db import connect, initialize


class HealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = str(Path(self.tmp.name) / "h.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_health_dispatch(self) -> None:
        status, body = dispatch("GET", "/health", b"")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_schema_initializes(self) -> None:
        conn = connect()
        initialize(conn)
        version = conn.execute(
            "SELECT value FROM service_meta WHERE key='schema_version'"
        ).fetchone()["value"]
        conn.close()
        self.assertEqual(version, "2")


if __name__ == "__main__":
    unittest.main()
