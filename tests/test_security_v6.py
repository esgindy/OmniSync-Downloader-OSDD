import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from starlette.testclient import TestClient
import app as web_app


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(web_app.app)

    def test_local_ok(self):
        self.assertEqual(self.c.get("/api/health").status_code, 200)

    def test_foreign_origin_blocked(self):
        r = self.c.post("/api/system/shutdown", headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_foreign_host_blocked(self):
        r = self.c.get("/api/health", headers={"Host": "evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_cross_site_fetch_blocked(self):
        r = self.c.get("/api/health", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)

    def test_local_origin_ok(self):
        r = self.c.get("/api/health", headers={"Origin": "http://127.0.0.1:8765"})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
