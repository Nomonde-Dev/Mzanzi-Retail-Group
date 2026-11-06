import io
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from werkzeug.security import generate_password_hash

from analysis import create_excel_report
from app import create_app


ROOT = Path(__file__).resolve().parent
PASSWORD = "correct horse battery staple"


class AuthenticatedAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "test_app.db"
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-key-" + ("x" * 48),
                "DATABASE": str(self.database_path),
                "SOURCE_CSV": str(ROOT / "clean_sales.csv"),
                "SEED_DATA": True,
            }
        )
        self.admin_id = self.add_user("Admin", "admin@example.com", "admin")
        self.analyst_id = self.add_user("Analyst", "analyst@example.com", "analyst")
        self.client = self.app.test_client()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def add_user(self, name, email, role):
        connection = sqlite3.connect(self.database_path)
        cursor = connection.execute(
            """
            INSERT INTO users (name, email, password_hash, role, active, created_at)
            VALUES (?, ?, ?, ?, 1, '2026-01-01T00:00:00+00:00')
            """,
            (name, email, generate_password_hash(PASSWORD), role),
        )
        connection.commit()
        user_id = cursor.lastrowid
        connection.close()
        return user_id

    @staticmethod
    def csrf_token(response):
        content = response.get_data(as_text=True)
        match = re.search(r'name="csrf_token" value="([^"]+)"', content)
        if match is None:
            match = re.search(r'<meta name="csrf-token" content="([^"]+)"', content)
        if match is None:
            raise AssertionError("Response did not contain a CSRF token.")
        return match.group(1)

    def sign_in(self, client, email):
        page = client.get("/login")
        token = self.csrf_token(page)
        return client.post(
            "/login",
            data={
                "csrf_token": token,
                "email": email,
                "password": PASSWORD,
            },
            follow_redirects=True,
        )

    def test_dashboard_and_reports_require_login(self):
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)
        self.assertEqual(self.client.get("/datasets").status_code, 302)
        self.assertEqual(self.client.get("/admin/users").status_code, 302)
        self.assertEqual(self.client.get("/reports/sales.xlsx").status_code, 302)
        self.assertEqual(self.client.post("/api/datasets").status_code, 401)

    def test_login_csrf_branding_and_secure_headers(self):
        page = self.client.get("/login")
        self.assertIn(b"MZANZI RETAIL GROUP", page.data)
        token = self.csrf_token(page)
        self.assertEqual(
            self.client.post(
                "/login",
                data={"email": "admin@example.com", "password": PASSWORD},
            ).status_code,
            400,
        )
        response = self.sign_in(self.client, "admin@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Smarter insights. Better decisions.", response.data)
        self.assertIn(b"Manage Users", response.data)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        nonce = re.search(rb'<script nonce="([^"]+)"', response.data).group(1).decode()
        self.assertIn(f"'nonce-{nonce}'", response.headers["Content-Security-Policy"])
        self.assertTrue(token)

    def test_self_registration_creates_analyst_and_signs_in(self):
        page = self.client.get("/login")
        self.assertIn(b"Create an account", page.data)
        register_page = self.client.get("/register")
        self.assertEqual(register_page.status_code, 200)
        self.assertIn(b"Confirm password", register_page.data)
        token = self.csrf_token(register_page)
        response = self.client.post(
            "/register",
            data={
                "csrf_token": token,
                "name": "New Mzanzi Analyst",
                "email": "new-analyst@example.com",
                "password": "a unique strong passphrase",
                "confirm_password": "a unique strong passphrase",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/dashboard"))
        with self.client.session_transaction() as browser_session:
            self.assertIn("user_id", browser_session)

        connection = sqlite3.connect(self.database_path)
        user = connection.execute(
            "SELECT role, password_hash FROM users WHERE email = ?",
            ("new-analyst@example.com",),
        ).fetchone()
        connection.close()
        self.assertEqual(user[0], "analyst")
        self.assertNotEqual(user[1], "a unique strong passphrase")
        dashboard = self.client.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotIn(b"Manage Users", dashboard.data)

    def test_self_registration_rejects_bad_csrf_mismatched_and_weak_passwords(self):
        self.assertEqual(
            self.client.post(
                "/register",
                data={
                    "name": "Example",
                    "email": "example@example.com",
                    "password": "a strong password",
                    "confirm_password": "a strong password",
                },
            ).status_code,
            400,
        )

        page = self.client.get("/register")
        token = self.csrf_token(page)
        mismatch = self.client.post(
            "/register",
            data={
                "csrf_token": token,
                "name": "Example",
                "email": "example@example.com",
                "password": "a strong password 123",
                "confirm_password": "a different password",
            },
        )
        self.assertIn(b"passwords do not match", mismatch.data)
        weak = self.client.post(
            "/register",
            data={
                "csrf_token": token,
                "name": "Example",
                "email": "example@example.com",
                "password": "short",
                "confirm_password": "short",
            },
        )
        self.assertIn(b"at least 12 characters", weak.data)

    def test_self_registration_rejects_duplicate_accounts(self):
        page = self.client.get("/register")
        response = self.client.post(
            "/register",
            data={
                "csrf_token": self.csrf_token(page),
                "name": "Duplicate Analyst",
                "email": "analyst@example.com",
                "password": "another unique strong password",
                "confirm_password": "another unique strong password",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Unable to register", response.data)

    def test_analyst_can_upload_but_cannot_manage_users_or_delete_data(self):
        self.sign_in(self.client, "analyst@example.com")
        dashboard = self.client.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertNotIn(b"Manage Users", dashboard.data)
        self.assertEqual(self.client.get("/admin/users").status_code, 403)

        dataset_id = self.client.get("/datasets").get_data(as_text=True)
        self.assertIn("Supplied sample data", dataset_id)
        connection = sqlite3.connect(self.database_path)
        sample_id = connection.execute(
            "SELECT id FROM datasets WHERE is_sample = 1"
        ).fetchone()[0]
        connection.close()
        token = self.csrf_token(dashboard)
        delete_response = self.client.post(
            f"/datasets/{sample_id}/delete",
            data={"csrf_token": token},
        )
        self.assertEqual(delete_response.status_code, 403)

        sample = pd.read_csv(ROOT / "clean_sales.csv").iloc[:1].to_csv(index=False)
        upload = self.client.post(
            "/api/datasets",
            headers={"X-CSRF-Token": token},
            data={"file": (io.BytesIO(sample.encode("utf-8")), "one-row.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(upload.status_code, 201)
        dataset_id = upload.get_json()["dataset_id"]
        connection = sqlite3.connect(self.database_path)
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM sales_transactions WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()[0],
            1,
        )
        connection.close()

    def test_admin_can_manage_users_and_delete_uploaded_datasets(self):
        self.sign_in(self.client, "admin@example.com")
        page = self.client.get("/admin/users")
        self.assertEqual(page.status_code, 200)
        token = self.csrf_token(page)
        created = self.client.post(
            "/admin/users",
            data={
                "csrf_token": token,
                "action": "create",
                "name": "New Analyst",
                "email": "new-analyst@example.com",
                "password": PASSWORD,
                "role": "analyst",
            },
            follow_redirects=True,
        )
        self.assertEqual(created.status_code, 200)
        self.assertIn(b"new-analyst@example.com", created.data)

        sample = pd.read_csv(ROOT / "clean_sales.csv").iloc[:1].to_csv(index=False)
        upload_page = self.client.get("/dashboard")
        token = self.csrf_token(upload_page)
        upload = self.client.post(
            "/api/datasets",
            headers={"X-CSRF-Token": token},
            data={"file": (io.BytesIO(sample.encode("utf-8")), "admin-upload.csv")},
            content_type="multipart/form-data",
        )
        dataset_id = upload.get_json()["dataset_id"]
        deleted = self.client.post(
            f"/datasets/{dataset_id}/delete",
            data={"csrf_token": token},
        )
        self.assertEqual(deleted.status_code, 302)

    def test_invalid_upload_is_rejected_and_password_reset_revokes_session(self):
        self.sign_in(self.client, "admin@example.com")
        dashboard = self.client.get("/dashboard")
        token = self.csrf_token(dashboard)
        bad_upload = self.client.post(
            "/api/datasets",
            headers={"X-CSRF-Token": token},
            data={"file": (io.BytesIO(b"wrong,columns\n1,2\n"), "bad.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(bad_upload.status_code, 400)
        self.assertIn("Missing required columns", bad_upload.get_json()["error"])

        analyst = self.app.test_client()
        self.sign_in(analyst, "analyst@example.com")
        self.assertEqual(analyst.get("/dashboard").status_code, 200)
        self.client.post(
            "/admin/users",
            data={
                "csrf_token": token,
                "action": "reset_password",
                "user_id": self.analyst_id,
                "password": "a new strong passphrase",
            },
        )
        revoked = analyst.get("/dashboard")
        self.assertEqual(revoked.status_code, 302)
        self.assertIn("/login", revoked.location)

    def test_admin_report_is_a_valid_workbook(self):
        self.sign_in(self.client, "admin@example.com")
        report = self.client.get("/reports/sales.xlsx")
        self.assertEqual(report.status_code, 200)
        self.assertIn(
            "spreadsheetml",
            report.headers["Content-Type"],
        )
        self.assertTrue(report.data.startswith(b"PK"))
        workbook = load_workbook(io.BytesIO(report.data), read_only=True)
        self.assertIn("Executive Summary", workbook.sheetnames)
        workbook.close()

    def test_excel_report_neutralizes_formula_like_dataset_text(self):
        data = pd.read_csv(ROOT / "clean_sales.csv").iloc[:1].copy()
        data["product"] = "=1+1"
        output = io.BytesIO()
        create_excel_report(data, output)
        workbook = load_workbook(output, data_only=False, read_only=True)
        product = workbook["Products"]["A2"]
        self.assertEqual(product.value, "'=1+1")
        self.assertEqual(product.data_type, "s")
        workbook.close()


if __name__ == "__main__":
    unittest.main()
