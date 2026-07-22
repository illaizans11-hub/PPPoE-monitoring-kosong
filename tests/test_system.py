import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


class MonitoringSystemTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        os.environ["PPPOE_ROOT"] = cls.temp_dir.name
        os.environ.pop("MIKROTIK_API_TOKEN", None)
        project_root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(project_root))
        import app as app_module

        cls.module = app_module
        cls.client = app_module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_01_empty_system_is_safe(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/api/pppoes").get_json(), [])
        self.assertEqual(self.client.get("/api/history").get_json(), [])
        for url in ("/", "/dashboard", "/admin", "/analytics", "/notifications"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_02_import_new_region(self):
        records = [
            {
                "user": "kediri-ayu",
                "id": "KDR-001",
                "lokasi": "Kecamatan A",
                "latitude": -7.81,
                "longitude": 112.01,
                "router": "ROUTER KEDIRI",
                "profile": "10M",
            },
            {
                "user": "kediri-adi",
                "id": "KDR-002",
                "lokasi": "Kecamatan B",
                "router": "ROUTER KEDIRI",
            },
        ]
        payload = io.BytesIO(json.dumps(records).encode())
        response = self.client.post(
            "/api/import-customers?mode=replace",
            data={"file": (payload, "kediri.json")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        data = self.client.get("/api/pppoes").get_json()
        self.assertEqual(len(data), 2)
        ayu = next(row for row in data if row["user"] == "kediri-ayu")
        self.assertEqual(ayu["id"], "KDR-001")
        self.assertIsInstance(ayu["latitude"], float)
        self.assertEqual(ayu["status"], "OFFLINE")

        html = self.client.get("/admin").get_data(as_text=True)
        self.assertIn("Kediri", html)
        self.assertIn("ROUTER KEDIRI", html)
        self.assertNotIn("SALAM UTAMA", html)

    def test_03_mikrotik_update_keeps_exact_metadata(self):
        response = self.client.post(
            "/api/update-pppoe",
            json={
                "router": "ROUTER KEDIRI",
                "pppoe": [
                    {
                        "user": "kediri-ayu",
                        "ip": "10.20.0.2",
                        "mac": "00:11:22:33:44:55",
                        "uptime": "2h",
                        "status": "ONLINE",
                    },
                    {
                        "user": "kediri-aditya",
                        "ip": "10.20.0.9",
                        "mac": "00:11:22:33:44:99",
                        "uptime": "1h",
                        "status": "ONLINE",
                    },
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        data = self.client.get("/api/pppoes").get_json()
        ayu = next(row for row in data if row["user"] == "kediri-ayu")
        aditya = next(row for row in data if row["user"] == "kediri-aditya")
        self.assertEqual(ayu["id"], "KDR-001")
        self.assertEqual(ayu["lokasi"], "Kecamatan A")
        self.assertEqual(ayu["status"], "ONLINE")
        self.assertEqual(ayu["ip"], "10.20.0.2")
        self.assertEqual(aditya["id"], "-")
        self.assertEqual(aditya["latitude"], "-")

    def test_04_queue_is_filtered_by_router(self):
        response = self.client.post(
            "/edit/kediri-ayu",
            data={
                "router_name": "ROUTER KEDIRI",
                "username_pppoe": "kediri-ayu",
                "id_pelanggan": "KDR-001",
                "lokasi": "Kecamatan A",
                "ip": "10.20.0.2",
                "latitude": "-7.81",
                "longitude": "112.01",
                "profile": "20M",
                "password": "",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        right_router = self.client.get("/api/get-queue?router=ROUTER%20KEDIRI").get_data(as_text=True)
        wrong_router = self.client.get("/api/get-queue?router=ROUTER%20LAIN").get_data(as_text=True)
        self.assertIn('name="kediri-ayu"', right_router)
        self.assertIn('profile="20M"', right_router)
        self.assertNotIn('password=""', right_router)
        self.assertEqual(wrong_router, "")

    def test_05_csv_import_merge(self):
        csv_data = (
            "user,id,lokasi,latitude,longitude,router,status\n"
            "malang-rina,MLG-001,Kecamatan C,-7.98,112.63,ROUTER MALANG,OFFLINE\n"
        )
        response = self.client.post(
            "/api/import-customers?mode=merge",
            data={"file": (io.BytesIO(csv_data.encode()), "malang.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        data = self.client.get("/api/pppoes").get_json()
        rina = next(row for row in data if row["user"] == "malang-rina")
        self.assertEqual(rina["id"], "MLG-001")
        self.assertEqual(rina["router"], "ROUTER MALANG")

    def test_06_analytics_uses_current_customer_data(self):
        response = self.client.get("/api/analytics-data")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        current = self.client.get("/api/pppoes").get_json()
        online = sum(1 for row in current if row.get("status") == "ONLINE")
        self.assertEqual(payload["realtime"]["total"], len(current))
        self.assertEqual(payload["realtime"]["online"], online)
        self.assertEqual(payload["realtime"]["offline"], len(current) - online)

    def test_07_admin_import_is_merge_safe(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "list.html").read_text(encoding="utf-8")
        self.assertIn("/api/import-customers?mode=merge", template)
        self.assertNotIn("/api/import-customers?mode=replace", template)

    def test_08_dashboard_realtime_dataset_index_is_valid(self):
        template = (Path(__file__).resolve().parents[1] / "templates" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("chart.data.datasets[0].data.push(pppoeData)", template)
        self.assertNotIn("chart.data.datasets[1].data.push(pppoeData)", template)


if __name__ == "__main__":
    unittest.main()
