import tempfile
import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from ntc_recordings_app import create_app


class RecordingRequestPanelTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "MessageRecordings"
        self.root.mkdir(parents=True)
        self.worship_root = Path(self.tempdir.name) / "WorshipRecordings"
        self.worship_root.mkdir(parents=True)
        self.recording = self.root / "20260419 - Jesus Is Our Peace - Bro Blessen.mp3"
        self.recording.write_bytes(b"fake-mp3-audio")
        (self.root / "February 8, 2026 - Brother Paul's Testimony.mp3").write_bytes(b"fake-testimony-audio")
        (self.worship_root / "20260419 - NTCWorship1030 LR.wav").write_bytes(b"fake-worship-audio")
        self.db_path = Path(self.tempdir.name) / "recording-requests.db"
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "NTC_RECORDINGS_DB_PATH": str(self.db_path),
                "NTC_RECORDINGS_LIBRARY_DIRS": f"message:{self.root},worship:{self.worship_root}",
                "NTC_RECORDINGS_PUBLIC_BASE_URL": "https://recordings.example.test",
                "NTC_RECORDINGS_ADMIN_PASSWORD": "admin-password",
                "NTC_RECORDINGS_EMAIL_ENABLED": "0",
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def _login(self):
        return self.client.post("/admin/login", data={"password": "admin-password"}, follow_redirects=True)

    def _first_recording_date_from_public_form(self):
        html = self.client.get("/").data.decode("utf-8")
        marker = '<option value="">Choose an available service date</option>'
        start = html.index(marker) + len(marker)
        start = html.index('<option value="', start) + len('<option value="')
        end = html.index('"', start)
        return html[start:end]

    def _first_recording_id_from_admin_panel(self, html: str) -> str:
        marker = '<select name="recording_id" required>'
        start = html.index(marker) + len(marker)
        start = html.index('<option value="', start) + len('<option value="')
        end = html.index('"', start)
        return html[start:end]

    def test_public_form_limits_requests_to_available_recordings(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Recording Requests", response.data)
        self.assertIn(b"How Requests Work", response.data)
        self.assertIn(b"Choose an available service date", response.data)
        self.assertIn(b"Service Date", response.data)
        self.assertIn(b"Recording Type", response.data)
        self.assertIn(b"Worship recording", response.data)
        self.assertIn(b'data-kinds="message,worship"', response.data)
        self.assertIn(b"Send Copy To", response.data)
        self.assertNotIn(b"Search Recordings", response.data)
        self.assertNotIn(b"Jesus Is Our Peace - Bro Blessen", response.data)

        created = self.client.post(
            "/request",
            data={
                "requester_name": "Test Person",
                "email": "person@example.test",
                "secondary_email": "second@example.test",
                "phone": "555-111-2222",
                "requested_date": self._first_recording_date_from_public_form(),
                "notes": "Please send the message.",
            },
            follow_redirects=True,
        )

        self.assertEqual(created.status_code, 200)
        self.assertIn(b"Request submitted", created.data)

    def test_worship_request_matches_worship_recording(self):
        created = self.client.post(
            "/request",
            data={
                "requester_name": "Worship Person",
                "email": "worship@example.test",
                "recording_kind": "worship",
                "requested_date": self._first_recording_date_from_public_form(),
            },
            follow_redirects=True,
        )

        self.assertEqual(created.status_code, 200)
        self._login()
        panel = self.client.get("/admin/panel").data
        self.assertIn(b"Worship Person", panel)
        self.assertIn(b"Worship", panel)
        self.assertIn(b"NTCWorship1030 LR", panel)

    def test_admin_requires_password_and_can_prepare_share_link(self):
        self.client.post(
            "/request",
            data={
                "requester_name": "Test Person",
                "email": "person@example.test",
                "requested_date": self._first_recording_date_from_public_form(),
            },
        )

        denied = self.client.get("/admin/panel")
        self.assertEqual(denied.status_code, 302)

        logged_in = self._login()
        self.assertEqual(logged_in.status_code, 200)
        self.assertIn(b"Recording Requests", logged_in.data)
        self.assertIn(b"Pending Requests", logged_in.data)
        self.assertIn(b"Completed", logged_in.data)
        self.assertIn(b"Archived", logged_in.data)
        self.assertIn(b"Prepare Link", logged_in.data)
        self.assertIn(b"Email message", logged_in.data)
        self.assertNotIn(b"Recent Library Files", logged_in.data)
        self.assertIn(b'data-ntc-branding="ntc-bg"', logged_in.data)

        recording_id = self._first_recording_id_from_admin_panel(logged_in.data.decode("utf-8"))

        prepared = self.client.post(
            "/admin/requests/1/send",
            data={"recording_id": recording_id, "email_message": "Custom note for this request."},
            follow_redirects=True,
        )

        self.assertEqual(prepared.status_code, 200)
        self.assertIn(b"Share link is ready", prepared.data)
        self.assertIn(b"Open prepared share link", prepared.data)
        self.assertIn(b"Custom note for this request.", prepared.data)

        html = prepared.data.decode("utf-8")
        token_start = html.index("/share/") + len("/share/")
        token_end = html.index('"', token_start)
        token = html[token_start:token_end]

        share = self.client.get(f"/share/{token}")
        self.assertEqual(share.status_code, 200)
        self.assertIn(b"Download Recording", share.data)

        download = self.client.get(f"/share/{token}/download")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.data, b"fake-mp3-audio")

        revoked = self.client.post("/admin/requests/1/revoke", follow_redirects=True)
        self.assertEqual(revoked.status_code, 200)
        self.assertIn(b"Recording access revoked", revoked.data)
        self.assertIn(b"Revoked", revoked.data)
        self.assertEqual(self.client.get(f"/share/{token}").status_code, 404)

    def test_completed_request_can_be_archived(self):
        self.client.post(
            "/request",
            data={
                "requester_name": "Archive Person",
                "email": "archive@example.test",
                "requested_date": self._first_recording_date_from_public_form(),
            },
        )
        self._login()
        panel = self.client.get("/admin/panel").data.decode("utf-8")
        recording_id = self._first_recording_id_from_admin_panel(panel)

        prepared = self.client.post(
            "/admin/requests/1/send",
            data={"recording_id": recording_id},
            follow_redirects=True,
        )
        self.assertIn(b"Share link is ready", prepared.data)
        revoked = self.client.post("/admin/requests/1/revoke", follow_redirects=True)
        self.assertIn(b"Recording access revoked", revoked.data)

        archived = self.client.post("/admin/requests/1/archive", follow_redirects=True)

        self.assertEqual(archived.status_code, 200)
        self.assertIn(b"Request archived", archived.data)
        self.assertIn(b"Archived Requests", archived.data)

    def test_old_completed_requests_auto_archive(self):
        self.client.post(
            "/request",
            data={
                "requester_name": "Old Completed Person",
                "email": "old-completed@example.test",
                "requested_date": self._first_recording_date_from_public_form(),
            },
        )
        self._login()
        panel = self.client.get("/admin/panel").data.decode("utf-8")
        recording_id = self._first_recording_id_from_admin_panel(panel)
        self.client.post(
            "/admin/requests/1/send",
            data={"recording_id": recording_id},
            follow_redirects=True,
        )
        self.client.post("/admin/requests/1/revoke", follow_redirects=True)
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(timespec="seconds")
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE recording_requests SET revoked_at = ? WHERE id = 1",
                (old_timestamp,),
            )

        completed = self.client.get("/admin/panel?tab=completed")
        archived = self.client.get("/admin/panel?tab=archived")

        self.assertNotIn(b"Old Completed Person", completed.data)
        self.assertIn(b"Old Completed Person", archived.data)
        self.assertIn(b"Archived Requests", archived.data)

    def test_proxy_prefix_is_preserved_on_admin_redirects(self):
        response = self.client.post(
            "/admin/login",
            data={"password": "admin-password"},
            headers={"X-Forwarded-Prefix": "/recordings"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/recordings/admin/panel")

    def test_health_reports_recording_count(self):
        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["recording_count"], 3)
        self.assertEqual(payload["recording_counts_by_kind"]["message"], 2)
        self.assertEqual(payload["recording_counts_by_kind"]["worship"], 1)
        with sqlite3.connect(self.db_path) as connection:
            indexed_count = connection.execute("SELECT COUNT(*) FROM recording_library").fetchone()[0]
            refreshed_at = connection.execute(
                "SELECT value FROM recording_library_meta WHERE key = 'last_refresh_finished'"
            ).fetchone()
        self.assertEqual(indexed_count, 3)
        self.assertIsNotNone(refreshed_at)

    def test_nextcloud_share_provider_can_generate_public_link(self):
        self.app.config.update(
            NTC_RECORDINGS_SHARE_PROVIDER="nextcloud",
            NTC_NEXTCLOUD_BASE_URL="https://nextcloud.example.test",
            NTC_NEXTCLOUD_USERNAME="admin",
            NTC_NEXTCLOUD_APP_PASSWORD="app-password",
            NTC_NEXTCLOUD_LOCAL_PATH_PREFIX=str(self.root),
            NTC_NEXTCLOUD_PATH_PREFIX="Recordings/MessageRecordings",
        )
        self.client.post(
            "/request",
            data={
                "requester_name": "Test Person",
                "email": "person@example.test",
                "requested_date": self._first_recording_date_from_public_form(),
            },
        )
        self._login()
        panel = self.client.get("/admin/panel").data.decode("utf-8")
        recording_id = self._first_recording_id_from_admin_panel(panel)
        fake_response = Mock(status_code=200)
        fake_response.json.return_value = {"ocs": {"data": {"id": 2468, "url": "https://nextcloud.example.test/s/share-token"}}}

        with patch("ntc_recordings_app.requests.post", return_value=fake_response) as post:
            prepared = self.client.post(
                "/admin/requests/1/send",
                data={"recording_id": recording_id},
                follow_redirects=True,
            )

        self.assertEqual(prepared.status_code, 200)
        self.assertIn(b"https://nextcloud.example.test/s/share-token", prepared.data)
        self.assertIn(b"Share provider: nextcloud", prepared.data)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["data"]["path"], "/Recordings/MessageRecordings/20260419 - Jesus Is Our Peace - Bro Blessen.mp3")

        fake_delete = Mock(status_code=200)
        with patch("ntc_recordings_app.requests.delete", return_value=fake_delete) as delete:
            revoked = self.client.post("/admin/requests/1/revoke", follow_redirects=True)

        self.assertEqual(revoked.status_code, 200)
        self.assertIn(b"Recording access revoked", revoked.data)
        delete.assert_called_once()
        self.assertIn("/shares/2468", delete.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
