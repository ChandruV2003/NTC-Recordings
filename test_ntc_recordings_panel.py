import errno
import json
import os
import tempfile
import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from ntc_recordings_app import (
    _date_from_file_metadata,
    _extract_intro_speaker,
    _normalize_recording_email_message,
    _recording_id,
    _testimony_looks_like_message_recording,
    _testimony_suggestion_targets,
    _testimony_transcript_statuses_for_filter,
    _testimony_transcript_targets,
    _save_testimony_transcript,
    _valid_person_name_suggestion,
    create_app,
)


class RecordingRequestPanelTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "MessageRecordings"
        self.root.mkdir(parents=True)
        self.worship_root = Path(self.tempdir.name) / "WorshipRecordings"
        self.worship_root.mkdir(parents=True)
        self.testimony_root = Path(self.tempdir.name) / "TestimonyRecordings"
        self.testimony_root.mkdir(parents=True)
        self.rejected_root = Path(self.tempdir.name) / "TestimonyReviewRejected"
        self.rejected_root.mkdir(parents=True)
        self.recording = self.root / "20260419 - Jesus Is Our Peace - Bro Blessen.mp3"
        self.recording.write_bytes(b"fake-mp3-audio")
        (self.testimony_root / "February 8, 2026 - Brother Paul's Testimony.mp3").write_bytes(b"fake-testimony-audio")
        (self.testimony_root / "February 8, 2026 - Sister Mary's Testimony.mp3").write_bytes(b"second-testimony-audio")
        self.worship_service = self.worship_root / "2026" / "April" / "April 19, 2026 - Sunday Service"
        (self.worship_service / "LR").mkdir(parents=True)
        (self.worship_service / "FULL").mkdir(parents=True)
        (self.worship_service / "LR" / "April 19, 2026 - NTCWorship1030 - LR.mp3").write_bytes(b"fake-worship-lr")
        (self.worship_service / "FULL" / "April 19, 2026 - NTCWorship1030 - FULL.mp3").write_bytes(b"fake-worship-full")
        self.db_path = Path(self.tempdir.name) / "recording-requests.db"
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "NTC_RECORDINGS_DB_PATH": str(self.db_path),
                "NTC_RECORDINGS_LIBRARY_DIRS": f"message:{self.root},worship:{self.worship_root},testimony:{self.testimony_root}",
                "NTC_RECORDINGS_TESTIMONY_SOURCE_DIR": str(self.root / "DN300R"),
                "NTC_RECORDINGS_TESTIMONY_LIBRARY_DIR": str(self.testimony_root),
                "NTC_RECORDINGS_TESTIMONY_REJECTED_DIR": str(self.rejected_root),
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
        return self._recording_date_options_from_public_form()[0]["date"]

    def _first_recording_date_for_kind(self, kind: str):
        for option in self._recording_date_options_from_public_form():
            if kind in option["kinds"]:
                return option["date"]
        raise AssertionError(f"No public recording date found for kind {kind!r}")

    def _recording_date_options_from_public_form(self):
        html = self.client.get("/").data.decode("utf-8")
        marker = '<script type="application/json" id="recording-date-data">'
        start = html.index(marker) + len(marker)
        end = html.index("</script>", start)
        return json.loads(html[start:end])

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
        self.assertIn(b"Choose service date", response.data)
        self.assertIn(b"Service Date", response.data)
        self.assertIn(b"Recording Type", response.data)
        self.assertIn(b"Worship recordings", response.data)
        self.assertIn(b"Testimony recording", response.data)
        self.assertIn(b'id="recording-date-data"', response.data)
        self.assertIn(b"calendar-picker", response.data)
        self.assertIn(b"data-calendar-grid", response.data)
        self.assertIn(b"data-calendar-jump-toggle", response.data)
        self.assertIn(b"data-calendar-month-select", response.data)
        self.assertIn(b"data-calendar-year-select", response.data)
        self.assertIn(b"is-unavailable", response.data)
        self.assertIn(b"Greyed-out days", response.data)
        self.assertIn(b"renderCalendar", response.data)
        self.assertIn(b"syncJumpControls", response.data)
        self.assertNotIn(b'<select name="requested_date"', response.data)
        self.assertNotIn(b"${option.count} file", response.data)
        self.assertIn(b"Send Copy To", response.data)
        self.assertNotIn(b"Search Recordings", response.data)
        self.assertNotIn(b"Jesus Is Our Peace - Bro Blessen", response.data)
        date_options = self._recording_date_options_from_public_form()
        self.assertTrue(any(option["date"] == "2026-04-19" and option["kinds"] == ["message", "worship"] for option in date_options))

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

    def test_public_mount_renders_prefixed_forms_and_redirects(self):
        self.app.config["NTC_RECORDINGS_PUBLIC_BASE_URL"] = "https://ntcnas.myftp.org/recordings"

        public = self.client.get("/", base_url="https://ntcnas.myftp.org")

        self.assertEqual(public.status_code, 200)
        self.assertIn(b'action="/recordings/request"', public.data)

        created = self.client.post(
            "/request",
            base_url="https://ntcnas.myftp.org",
            data={
                "requester_name": "Public Prefix Person",
                "email": "prefix@example.test",
                "requested_date": self._first_recording_date_from_public_form(),
            },
        )

        self.assertEqual(created.status_code, 302)
        self.assertTrue(created.headers["Location"].startswith("/recordings/?message="))

        login = self.client.post(
            "/admin/login",
            base_url="https://ntcnas.myftp.org",
            data={"password": "admin-password"},
        )
        self.assertEqual(login.status_code, 302)
        self.assertEqual(login.headers["Location"], "/recordings/admin/panel")

        testimony_source_root = self.root / "DN300R"
        testimony_source_root.mkdir(exist_ok=True)
        (testimony_source_root / "REC00123.mp3").write_bytes(b"prefix-testimony-audio")

        review = self.client.get("/admin/testimonies", base_url="https://ntcnas.myftp.org")

        self.assertEqual(review.status_code, 200)
        self.assertIn(b'href="/recordings/admin/panel"', review.data)
        self.assertIn(b'data-status-url="/recordings/admin/testimonies/suggest-status"', review.data)
        self.assertIn(b'action="/recordings/admin/testimonies/', review.data)
        self.assertIn(b'formaction="/recordings/admin/testimonies/', review.data)
        self.assertIn(b'data-src="/recordings/admin/testimonies/audio/', review.data)

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
        self.assertIn(b"April 19, 2026 - Sunday Service", panel)
        self.assertIn(b"2 files", panel)

    def test_admin_panel_groups_requests_and_omits_redundant_details(self):
        self.client.post(
            "/request",
            data={
                "requester_name": "Test Person",
                "email": "person@example.test",
                "recording_kind": "message",
                "requested_date": self._first_recording_date_from_public_form(),
                "notes": "Please send the message.",
            },
        )

        self._login()
        panel = self.client.get("/admin/panel").data

        self.assertIn(b"Message Requests", panel)
        self.assertIn(b"Additional instructions:", panel)
        self.assertIn(b"submitted-cell", panel)
        self.assertNotIn(b"No extra contact", panel)
        self.assertNotIn(b"More request details", panel)
        self.assertNotIn(b">Notes<", panel)

    def test_testimony_request_matches_testimony_recording(self):
        created = self.client.post(
            "/request",
            data={
                "requester_name": "Testimony Person",
                "email": "testimony@example.test",
                "recording_kind": "testimony",
                "requested_date": self._first_recording_date_for_kind("testimony"),
            },
            follow_redirects=True,
        )

        self.assertEqual(created.status_code, 200)
        self._login()
        panel = self.client.get("/admin/panel").data
        self.assertIn(b"Testimony Person", panel)
        self.assertIn(b"Testimony", panel)
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT recording_path, recording_title FROM recording_requests WHERE requester_name = ?",
                ("Testimony Person",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(row[0].endswith(".mp3"))
        self.assertIn("Testimony", row[1])
        self.assertIn("TestimonyRecordings", row[0])

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
        self.assertNotIn(b"Active Links", logged_in.data)
        self.assertNotIn(b"Closed Requests", logged_in.data)
        self.assertNotIn(b"Archived Requests", logged_in.data)
        self.assertIn(b"Prepare Link", logged_in.data)
        self.assertIn(b"Email message", logged_in.data)
        self.assertIn(b"Edit email message", logged_in.data)
        self.assertNotIn(b"Close Without Sending", logged_in.data)
        self.assertNotIn(b'content:"Show"', logged_in.data)
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
        self.assertIn(b"Pending Requests", prepared.data)
        self.assertNotIn(b"Open prepared share link", prepared.data)

        completed = self.client.get("/admin/panel?tab=completed")
        self.assertEqual(completed.status_code, 200)
        self.assertIn(b"Open prepared share link", completed.data)
        self.assertIn(b"Custom note for this request.", completed.data)

        html = completed.data.decode("utf-8")
        token_start = html.index("/share/") + len("/share/")
        token_end = html.index('"', token_start)
        token = html[token_start:token_end]

        share = self.client.get(f"/share/{token}")
        self.assertEqual(share.status_code, 200)
        self.assertIn(b"<audio", share.data)
        self.assertIn(b'controlsList="nodownload"', share.data)
        self.assertIn(b"Download access is disabled", share.data)

        stream = self.client.get(f"/share/{token}/stream")
        self.assertEqual(stream.status_code, 200)
        self.assertEqual(stream.data, b"fake-mp3-audio")

        download = self.client.get(f"/share/{token}/download")
        self.assertEqual(download.status_code, 403)
        self.assertEqual(download.get_json()["error"], "recording downloads are disabled for shared links")

        revoked = self.client.post("/admin/requests/1/revoke", follow_redirects=True)
        self.assertEqual(revoked.status_code, 200)
        self.assertIn(b"Recording access revoked", revoked.data)
        self.assertIn(b"Completed Requests", revoked.data)
        self.assertIn(b"Access revoked", revoked.data)
        self.assertEqual(self.client.get(f"/share/{token}").status_code, 404)

    def test_closed_request_can_be_archived(self):
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
        self.assertIn(b"Completed Requests", archived.data)
        self.assertIn(b"Archived", archived.data)

    def test_active_request_must_be_revoked_before_archive(self):
        self.client.post(
            "/request",
            data={
                "requester_name": "Active Person",
                "email": "active@example.test",
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
        self.assertIn(b"Pending Requests", prepared.data)
        self.assertIn(b"No pending requests", prepared.data)

        archived = self.client.post("/admin/requests/1/archive", follow_redirects=True)

        self.assertEqual(archived.status_code, 200)
        self.assertIn(b"Revoke access before archiving a request", archived.data)
        self.assertIn(b"Completed Requests", archived.data)
        self.assertIn(b"Open prepared share link", archived.data)

    def test_old_closed_requests_auto_archive(self):
        self.client.post(
            "/request",
            data={
                "requester_name": "Old Closed Person",
                "email": "old-closed@example.test",
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

        closed = self.client.get("/admin/panel?tab=closed")
        archived = self.client.get("/admin/panel?tab=archived")

        self.assertIn(b"Completed Requests", closed.data)
        self.assertIn(b"Old Closed Person", closed.data)
        self.assertIn(b"Completed Requests", archived.data)
        self.assertIn(b"Old Closed Person", archived.data)
        self.assertIn(b"Archived", archived.data)

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
        self.assertEqual(payload["recording_count"], 5)
        self.assertEqual(payload["recording_counts_by_kind"]["message"], 1)
        self.assertEqual(payload["recording_counts_by_kind"]["worship"], 2)
        self.assertEqual(payload["recording_counts_by_kind"]["testimony"], 2)
        with sqlite3.connect(self.db_path) as connection:
            indexed_count = connection.execute("SELECT COUNT(*) FROM recording_library").fetchone()[0]
            refreshed_at = connection.execute(
                "SELECT value FROM recording_library_meta WHERE key = 'last_refresh_finished'"
            ).fetchone()
        self.assertEqual(indexed_count, 5)
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
        fake_get = Mock(status_code=200)
        fake_get.json.return_value = {"ocs": {"data": []}}
        fake_response = Mock(status_code=200)
        fake_response.json.return_value = {"ocs": {"data": {"id": 2468, "url": "https://nextcloud.example.test/s/share-token"}}}
        fake_put = Mock(status_code=200)

        with (
            patch("ntc_recordings_app.requests.get", return_value=fake_get) as get,
            patch("ntc_recordings_app.requests.post", return_value=fake_response) as post,
            patch("ntc_recordings_app.requests.put", return_value=fake_put) as put,
        ):
            prepared = self.client.post(
                "/admin/requests/1/send",
                data={"recording_id": recording_id},
                follow_redirects=True,
            )

        self.assertEqual(prepared.status_code, 200)
        self.assertIn(b"Pending Requests", prepared.data)
        completed = self.client.get("/admin/panel?tab=completed")
        self.assertIn(b"https://nextcloud.example.test/s/share-token", completed.data)
        self.assertIn(b"Link prepared", completed.data)
        self.assertNotIn(b"Share provider: nextcloud", completed.data)
        self.assertIn(b"Completed Requests", completed.data)
        get.assert_called_once()
        post.assert_called_once()
        put.assert_called_once()
        self.assertEqual(post.call_args.kwargs["data"]["path"], "/Recordings/MessageRecordings/20260419 - Jesus Is Our Peace - Bro Blessen.mp3")
        self.assertEqual(post.call_args.kwargs["data"]["shareType"], 3)
        self.assertEqual(post.call_args.kwargs["data"]["permissions"], 1)
        self.assertEqual(
            json.loads(post.call_args.kwargs["data"]["attributes"]),
            [{"scope": "permissions", "key": "download", "value": False}],
        )
        self.assertIn("/shares/2468", put.call_args.args[0])
        self.assertEqual(put.call_args.kwargs["data"]["permissions"], 1)
        self.assertEqual(
            json.loads(put.call_args.kwargs["data"]["attributes"]),
            [{"scope": "permissions", "key": "download", "value": False}],
        )

        fake_delete = Mock(status_code=200)
        with patch("ntc_recordings_app.requests.delete", return_value=fake_delete) as delete:
            revoked = self.client.post("/admin/requests/1/revoke", follow_redirects=True)

        self.assertEqual(revoked.status_code, 200)
        self.assertIn(b"Recording access revoked", revoked.data)
        delete.assert_called_once()
        self.assertIn("/shares/2468", delete.call_args.args[0])

    def test_worship_nextcloud_share_uses_service_folder(self):
        self.app.config.update(
            NTC_RECORDINGS_SHARE_PROVIDER="nextcloud",
            NTC_NEXTCLOUD_BASE_URL="https://nextcloud.example.test",
            NTC_NEXTCLOUD_USERNAME="admin",
            NTC_NEXTCLOUD_APP_PASSWORD="app-password",
            NTC_NEXTCLOUD_LOCAL_PATH_PREFIX=str(self.worship_root),
            NTC_NEXTCLOUD_PATH_PREFIX="Worship Recordings",
            NTC_NEXTCLOUD_PATH_MAPPINGS=f"{self.worship_root}=Worship Recordings",
        )
        self.client.post(
            "/request",
            data={
                "requester_name": "Worship Folder Person",
                "email": "worship-folder@example.test",
                "recording_kind": "worship",
                "requested_date": self._first_recording_date_from_public_form(),
            },
        )
        self._login()
        panel = self.client.get("/admin/panel").data.decode("utf-8")
        recording_id = self._first_recording_id_from_admin_panel(panel)
        fake_get = Mock(status_code=200)
        fake_get.json.return_value = {"ocs": {"data": []}}
        fake_response = Mock(status_code=200)
        fake_response.json.return_value = {"ocs": {"data": {"id": 1357, "url": "https://nextcloud.example.test/s/worship-folder"}}}
        fake_put = Mock(status_code=200)

        with (
            patch("ntc_recordings_app.requests.get", return_value=fake_get),
            patch("ntc_recordings_app.requests.post", return_value=fake_response) as post,
            patch("ntc_recordings_app.requests.put", return_value=fake_put) as put,
        ):
            prepared = self.client.post(
                "/admin/requests/1/send",
                data={"recording_id": recording_id},
                follow_redirects=True,
        )

        self.assertEqual(prepared.status_code, 200)
        completed = self.client.get("/admin/panel?tab=completed")
        self.assertIn(b"https://nextcloud.example.test/s/worship-folder", completed.data)
        self.assertEqual(
            post.call_args.kwargs["data"]["path"],
            "/Worship Recordings/2026/April/April 19, 2026 - Sunday Service",
        )
        self.assertEqual(post.call_args.kwargs["data"]["permissions"], 1)
        self.assertEqual(
            json.loads(post.call_args.kwargs["data"]["attributes"]),
            [{"scope": "permissions", "key": "download", "value": False}],
        )
        put.assert_called_once()
        self.assertIn("/shares/1357", put.call_args.args[0])

    def test_nextcloud_share_provider_reuses_existing_public_link(self):
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
                "requester_name": "Reuse Person",
                "email": "reuse@example.test",
                "requested_date": self._first_recording_date_from_public_form(),
            },
        )
        self._login()
        panel = self.client.get("/admin/panel").data.decode("utf-8")
        recording_id = self._first_recording_id_from_admin_panel(panel)
        fake_get = Mock(status_code=200)
        fake_get.json.return_value = {
            "ocs": {
                "data": [
                    {"id": 9753, "url": "https://nextcloud.example.test/s/existing-share"},
                ]
            }
        }

        fake_put = Mock(status_code=200)

        with (
            patch("ntc_recordings_app.requests.get", return_value=fake_get) as get,
            patch("ntc_recordings_app.requests.post") as post,
            patch("ntc_recordings_app.requests.put", return_value=fake_put) as put,
        ):
            prepared = self.client.post(
                "/admin/requests/1/send",
                data={"recording_id": recording_id},
                follow_redirects=True,
        )

        self.assertEqual(prepared.status_code, 200)
        completed = self.client.get("/admin/panel?tab=completed")
        self.assertIn(b"https://nextcloud.example.test/s/existing-share", completed.data)
        get.assert_called_once()
        post.assert_not_called()
        put.assert_called_once()
        self.assertIn("/shares/9753", put.call_args.args[0])
        self.assertEqual(put.call_args.kwargs["data"]["permissions"], 1)
        self.assertEqual(
            json.loads(put.call_args.kwargs["data"]["attributes"]),
            [{"scope": "permissions", "key": "download", "value": False}],
        )

        fake_delete = Mock(status_code=200)
        with patch("ntc_recordings_app.requests.delete", return_value=fake_delete) as delete:
            revoked = self.client.post("/admin/requests/1/revoke", follow_redirects=True)

        self.assertEqual(revoked.status_code, 200)
        self.assertIn(b"Recording access revoked", revoked.data)
        delete.assert_called_once()
        self.assertIn("/shares/9753", delete.call_args.args[0])

    def test_testimony_review_tracks_source_speaker_identification(self):
        testimony_source_root = self.root / "DN300R"
        testimony_source_root.mkdir()
        raw_recording = testimony_source_root / "REC00042.mp3"
        raw_recording.write_bytes(b"raw-testimony-audio")
        service_timestamp = datetime(2026, 4, 19, 12, tzinfo=timezone.utc).timestamp()
        os.utime(raw_recording, (service_timestamp, service_timestamp))
        (testimony_source_root / "20250413 - Sister Rachel's Testimony.mp3").write_bytes(b"named-testimony-audio")

        denied = self.client.get("/admin/testimonies")
        self.assertEqual(denied.status_code, 302)

        self._login()
        review = self.client.get("/admin/testimonies")

        self.assertEqual(review.status_code, 200)
        self.assertIn(b"Testimony Review", review.data)
        self.assertIn(b"REC00042", review.data)
        self.assertIn(b"Check Durations", review.data)
        self.assertIn(b"Save Speaker", review.data)
        self.assertIn(b"Mark Duplicate", review.data)
        self.assertIn(b"Listen, confirm the service date", review.data)
        self.assertIn(b'preload="none" data-src="/admin/testimonies/audio/', review.data)
        self.assertNotIn(b'preload="metadata" src="/admin/testimonies/audio/', review.data)
        self.assertIn(b"Suggest Speaker", review.data)
        self.assertIn(b"Process Suggestions", review.data)
        self.assertIn(b"Group Title", review.data)
        self.assertIn(b"Grouped", review.data)
        self.assertIn(b"Process Transcripts", review.data)
        self.assertNotIn(b"Quarantine Rejected", review.data)
        self.assertIn(b'data-suggestion-job', review.data)
        self.assertIn(b'data-status-url="/admin/testimonies/suggest-status"', review.data)
        self.assertIn(b'data-transcript-job', review.data)
        self.assertIn(b'data-status-url="/admin/testimonies/transcript-status"', review.data)
        self.assertIn(b'data-review-id="', review.data)
        self.assertIn(b'data-row-number aria-label="Row 1">#1</span>', review.data)
        self.assertIn(b"renumberReviewRows", review.data)
        self.assertIn(b"pauseCardAudio", review.data)
        self.assertIn(b"pauseOtherReviewAudio", review.data)
        self.assertIn(b'document.addEventListener("play"', review.data)
        self.assertIn(b"ntc-testimony-open-cards", review.data)
        self.assertIn(b"X-Requested-With", review.data)
        self.assertIn(b'id="speaker-name-options"', review.data)
        self.assertNotIn(b"DN300R folder", review.data)
        self.assertNotIn(b"Final Title", review.data)
        self.assertNotIn(b"Voice / ID Notes", review.data)
        self.assertNotIn(b"Proposed Destination", review.data)
        self.assertNotIn(b"Already Named", review.data)
        self.assertNotIn(b"20250413 - Sister Rachel", review.data)

        identified = self.client.get("/admin/testimonies?status=identified")
        self.assertEqual(identified.status_code, 200)
        self.assertIn(b"20250413 - Sister Rachel", identified.data)
        self.assertIn(b"Process Transcripts", identified.data)

        recording_id = _recording_id(raw_recording)
        audio = self.client.get(f"/admin/testimonies/audio/{recording_id}")
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.data, b"raw-testimony-audio")

        with patch(
            "ntc_recordings_app._transcribe_testimony_intro",
            return_value=("For those of you who do not know me, my name is Kevin. I want to thank the Lord.", ""),
        ):
            suggested = self.client.post(
                f"/admin/testimonies/{recording_id}/suggest",
                data={
                    "status_filter": "needs_review",
                    "sort": "shortest",
                    "source_path": str(raw_recording),
                    "service_date": "2026-04-19",
                },
                follow_redirects=True,
            )

        self.assertEqual(suggested.status_code, 200)
        self.assertIn(b"Suggested speaker: Kevin.", suggested.data)
        self.assertIn(b"Suggested Speaker", suggested.data)
        self.assertIn(b"Kevin", suggested.data)
        self.assertIn(b"from intro transcript", suggested.data)
        self.assertIn(b"Use Suggestion", suggested.data)
        self.assertIn(b"Type speaker name", suggested.data)

        with sqlite3.connect(self.db_path) as connection:
            suggestion_row = connection.execute(
                "SELECT status, suggested_speaker, suggestion_source, suggestion_text FROM testimony_reviews WHERE recording_id = ?",
                (recording_id,),
            ).fetchone()

        self.assertIsNotNone(suggestion_row)
        self.assertEqual(suggestion_row[0], "needs_review")
        self.assertEqual(suggestion_row[1], "Kevin")
        self.assertEqual(suggestion_row[2], "transcript_intro")
        self.assertIn("my name is Kevin", suggestion_row[3])

        with patch("ntc_recordings_app._probe_audio_duration", return_value=65):
            probed = self.client.post(
                "/admin/testimonies/probe",
                data={"status": "needs_review", "sort": "shortest", "limit": "1"},
                follow_redirects=True,
            )

        self.assertEqual(probed.status_code, 200)
        with sqlite3.connect(self.db_path) as connection:
            preserved_suggestion_row = connection.execute(
                "SELECT suggested_speaker, suggestion_source, suggestion_text, duration_seconds FROM testimony_reviews WHERE recording_id = ?",
                (recording_id,),
            ).fetchone()
        self.assertEqual(preserved_suggestion_row[0], "Kevin")
        self.assertEqual(preserved_suggestion_row[1], "transcript_intro")
        self.assertIn("my name is Kevin", preserved_suggestion_row[2])
        self.assertEqual(preserved_suggestion_row[3], 65)

        with patch("os.rename", side_effect=OSError(errno.EXDEV, "Invalid cross-device link")):
            saved = self.client.post(
                f"/admin/testimonies/{recording_id}/review",
                data={
                    "status": "identified",
                    "status_filter": "needs_review",
                    "source_path": str(raw_recording),
                    "speaker_name": "Sister Test",
                },
                follow_redirects=True,
            )

        self.assertEqual(saved.status_code, 200)
        self.assertIn(b"Testimony review saved and renamed", saved.data)
        self.assertIn(b"Needs Review", saved.data)
        self.assertNotIn(b"20260419 - Sister Test&#39;s Testimony.mp3", saved.data)
        self.assertNotIn(b"Sunday Testimonies", saved.data)

        renamed_path = self.testimony_root / "2026" / "Sunday Testimonies" / "April 19, 2026 - Sister Test's Testimony.mp3"
        self.assertFalse(raw_recording.exists())
        self.assertTrue(renamed_path.exists())
        self.assertEqual(renamed_path.read_bytes(), b"raw-testimony-audio")

        with sqlite3.connect(self.db_path) as connection:
            old_row = connection.execute(
                "SELECT recording_id FROM testimony_reviews WHERE recording_id = ?",
                (recording_id,),
            ).fetchone()
            new_recording_id = _recording_id(renamed_path)
            row = connection.execute(
                "SELECT service_date, testimony_title, proposed_path FROM testimony_reviews WHERE recording_id = ?",
                (new_recording_id,),
            ).fetchone()

        self.assertIsNone(old_row)
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "2026-04-19")
        self.assertEqual(row[1], "Sister Test's Testimony")
        self.assertIn("Sunday Testimonies", row[2])
        self.assertIn("TestimonyRecordings", row[2])
        self.assertTrue(row[2].endswith("April 19, 2026 - Sister Test's Testimony.mp3"))

        identified_after_save = self.client.get("/admin/testimonies?status=identified")
        self.assertEqual(identified_after_save.status_code, 200)
        self.assertIn(b"Sister Test", identified_after_save.data)
        self.assertIn(b"April 19, 2026 - Sister Test", identified_after_save.data)

        renamed_audio = self.client.get(f"/admin/testimonies/audio/{new_recording_id}")
        self.assertEqual(renamed_audio.status_code, 200)
        self.assertEqual(renamed_audio.data, b"raw-testimony-audio")

    def test_testimony_review_can_mark_duplicate_recordings(self):
        testimony_source_root = self.root / "DN300R"
        testimony_source_root.mkdir()
        primary_recording = testimony_source_root / "REC00198.wav"
        duplicate_recording = testimony_source_root / "REC10199.wav"
        primary_recording.write_bytes(b"same-testimony-content-primary")
        duplicate_recording.write_bytes(b"same-testimony-content-duplicate")
        service_timestamp = datetime(2025, 8, 3, 12, tzinfo=timezone.utc).timestamp()
        os.utime(primary_recording, (service_timestamp, service_timestamp))
        os.utime(duplicate_recording, (service_timestamp, service_timestamp))
        duplicate_id = _recording_id(duplicate_recording)

        self._login()
        response = self.client.post(
            f"/admin/testimonies/{duplicate_id}/review",
            data={
                "status": "duplicate",
                "status_filter": "needs_review",
                "source_path": str(duplicate_recording),
                "service_date": "2025-08-03",
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "duplicate")
        self.assertEqual(payload["status_label"], "Duplicate")
        self.assertEqual(payload["source_label"], "REC10199.wav")

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT status, service_date, speaker_name FROM testimony_reviews WHERE recording_id = ?",
                (duplicate_id,),
            ).fetchone()
        self.assertEqual(row, ("duplicate", "2025-08-03", ""))

        needs_review = self.client.get("/admin/testimonies?status=needs_review").data
        self.assertIn(b"REC00198", needs_review)
        self.assertNotIn(b"REC10199", needs_review)

        duplicate = self.client.get("/admin/testimonies?status=duplicate").data
        self.assertIn(b"REC10199", duplicate)
        self.assertIn(b"Duplicate", duplicate)
        self.assertIn(b"Quarantine Duplicates", duplicate)

        all_items = self.client.get("/admin/testimonies?status=all").data
        self.assertIn(b"REC00198", all_items)
        self.assertIn(b"REC10199", all_items)

    def test_funeral_date_testimony_saves_to_funeral_folder(self):
        testimony_source_root = self.root / "DN300R"
        testimony_source_root.mkdir()
        raw_recording = testimony_source_root / "REC00090.mp3"
        raw_recording.write_bytes(b"funeral-testimony-audio")
        recording_id = _recording_id(raw_recording)

        self._login()
        response = self.client.post(
            f"/admin/testimonies/{recording_id}/review",
            data={
                "status": "identified",
                "status_filter": "needs_review",
                "source_path": str(raw_recording),
                "service_date": "2025-04-20",
                "speaker_name": "Brother Blessen",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        funeral_path = (
            self.testimony_root
            / "2025"
            / "Funeral Testimonies"
            / "April 20-21, 2025 - Brother K.T. Varghese's Funeral"
            / "April 20, 2025 - Brother Blessen's Testimony.mp3"
        )
        self.assertFalse(raw_recording.exists())
        self.assertTrue(funeral_path.exists())
        self.assertEqual(funeral_path.read_bytes(), b"funeral-testimony-audio")

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT source_path, proposed_path FROM testimony_reviews WHERE recording_id = ?",
                (_recording_id(funeral_path),),
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], str(funeral_path))
        self.assertIn("Funeral Testimonies", row[1])
        self.assertIn("Brother K.T. Varghese", row[1])

    def test_funeral_date_grouped_testimony_saves_part_title(self):
        testimony_source_root = self.root / "DN300R"
        testimony_source_root.mkdir()
        raw_recording = testimony_source_root / "REC00088.mp3"
        raw_recording.write_bytes(b"grouped-funeral-testimony-audio")
        recording_id = _recording_id(raw_recording)

        self._login()
        response = self.client.post(
            f"/admin/testimonies/{recording_id}/review",
            data={
                "status": "grouped",
                "status_filter": "needs_review",
                "source_path": str(raw_recording),
                "service_date": "2025-04-20",
                "group_title": "Brother K.T. Varghese Memorial Service Testimonies Part 1",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        grouped_path = (
            self.testimony_root
            / "2025"
            / "Funeral Testimonies"
            / "April 20-21, 2025 - Brother K.T. Varghese's Funeral"
            / "April 20, 2025 - Brother K.T. Varghese Memorial Service Testimonies Part 1.mp3"
        )
        self.assertFalse(raw_recording.exists())
        self.assertTrue(grouped_path.exists())
        self.assertEqual(grouped_path.read_bytes(), b"grouped-funeral-testimony-audio")

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT status, speaker_name, testimony_title, source_path FROM testimony_reviews WHERE recording_id = ?",
                (_recording_id(grouped_path),),
            ).fetchone()

        self.assertEqual(row, ("grouped", "", "Brother K.T. Varghese Memorial Service Testimonies Part 1", str(grouped_path)))
        grouped = self.client.get("/admin/testimonies?status=grouped").data
        self.assertIn(b"Testimonies Part 1", grouped)
        self.assertIn(b"Grouped", grouped)
        self.assertIn(b"Process Transcripts", grouped)

    def test_testimony_review_quarantines_rejected_recordings(self):
        testimony_source_root = self.root / "DN300R"
        testimony_source_root.mkdir()
        duplicate_recording = testimony_source_root / "REC10199.wav"
        duplicate_recording.write_bytes(b"same-testimony-content-duplicate")
        service_timestamp = datetime(2025, 8, 3, 12, tzinfo=timezone.utc).timestamp()
        os.utime(duplicate_recording, (service_timestamp, service_timestamp))
        duplicate_id = _recording_id(duplicate_recording)

        self._login()
        marked = self.client.post(
            f"/admin/testimonies/{duplicate_id}/review",
            data={
                "status": "duplicate",
                "status_filter": "needs_review",
                "source_path": str(duplicate_recording),
                "service_date": "2025-08-03",
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )
        self.assertEqual(marked.status_code, 200)

        quarantined = self.client.post(
            "/admin/testimonies/quarantine",
            data={"status": "duplicate", "sort": "shortest"},
            follow_redirects=True,
        )

        self.assertEqual(quarantined.status_code, 200)
        self.assertIn(b"Moved 1 duplicate file to quarantine", quarantined.data)
        self.assertIn(b"Moved to rejected holding folder", quarantined.data)
        quarantine_path = self.rejected_root / "Duplicate" / "2025" / "REC10199.wav"
        self.assertFalse(duplicate_recording.exists())
        self.assertTrue(quarantine_path.exists())
        self.assertEqual(quarantine_path.read_bytes(), b"same-testimony-content-duplicate")

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT status, source_path, quarantined_from_path, quarantined_path, quarantined_at
                FROM testimony_reviews
                WHERE recording_id = ?
                """,
                (duplicate_id,),
            ).fetchone()
        self.assertEqual(row[0], "duplicate")
        self.assertEqual(row[1], str(quarantine_path))
        self.assertEqual(row[2], str(duplicate_recording))
        self.assertEqual(row[3], str(quarantine_path))
        self.assertTrue(row[4])

        audio = self.client.get(f"/admin/testimonies/audio/{duplicate_id}")
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.data, b"same-testimony-content-duplicate")

    def test_testimony_review_supports_json_row_updates(self):
        testimony_source_root = self.root / "DN300R"
        testimony_source_root.mkdir()
        raw_recording = testimony_source_root / "REC00077.mp3"
        raw_recording.write_bytes(b"async-testimony-audio")
        service_timestamp = datetime(2026, 7, 23, 12, tzinfo=timezone.utc).timestamp()
        os.utime(raw_recording, (service_timestamp, service_timestamp))
        recording_id = _recording_id(raw_recording)

        self._login()
        response = self.client.post(
            f"/admin/testimonies/{recording_id}/review",
            data={
                "status": "identified",
                "status_filter": "needs_review",
                "source_path": str(raw_recording),
                "service_date": "2026-07-23",
                "speaker_name": "Kevin",
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["previous_recording_id"], recording_id)
        self.assertNotEqual(payload["recording_id"], recording_id)
        self.assertEqual(payload["status"], "identified")
        self.assertEqual(payload["status_label"], "Identified")
        self.assertEqual(payload["speaker_name"], "Kevin")
        self.assertEqual(payload["service_date_label"], "July 23, 2026")
        self.assertIn("July 23, 2026 - Kevin", payload["source_label"])
        self.assertIn("/admin/testimonies/audio/", payload["audio_url"])
        self.assertIn("/admin/testimonies/", payload["review_url"])
        self.assertFalse(raw_recording.exists())
        self.assertTrue(Path(payload["source_path"]).exists())
        self.assertIn("TestimonyRecordings", payload["source_path"])

    def test_testimony_review_converts_wav_to_mp3_when_saving_speaker(self):
        testimony_source_root = self.root / "DN300R"
        testimony_source_root.mkdir()
        raw_recording = testimony_source_root / "REC00078.wav"
        raw_recording.write_bytes(b"fake-wav-audio")
        recording_id = _recording_id(raw_recording)

        def fake_ffmpeg(args, **kwargs):
            output = Path(args[-1])
            output.write_bytes(b"fake-mp3-audio")
            return Mock(returncode=0, stdout="", stderr="")

        self._login()
        with patch("ntc_recordings_app.subprocess.run", side_effect=fake_ffmpeg):
            response = self.client.post(
                f"/admin/testimonies/{recording_id}/review",
                data={
                    "status": "identified",
                    "status_filter": "needs_review",
                    "source_path": str(raw_recording),
                    "service_date": "2026-07-23",
                    "speaker_name": "Kevin",
                },
                headers={"Accept": "application/json", "X-Requested-With": "fetch"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["source_path"].endswith(".mp3"))
        self.assertTrue(payload["source_label"].endswith(".mp3"))
        self.assertFalse(raw_recording.exists())
        self.assertEqual(Path(payload["source_path"]).read_bytes(), b"fake-mp3-audio")

    def test_testimony_review_ajax_auth_failure_returns_json(self):
        response = self.client.post(
            "/admin/testimonies/missing-recording/review",
            data={
                "status": "identified",
                "source_path": "/missing/file.mp3",
                "speaker_name": "Nobody",
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.content_type, "application/json")
        self.assertEqual(response.get_json()["error"], "Admin session expired. Sign in again, then retry the testimony update.")

    def test_testimony_review_uses_form_action_for_regular_save_buttons(self):
        testimony_source_root = self.root / "DN300R"
        testimony_source_root.mkdir()
        (testimony_source_root / "REC00088.mp3").write_bytes(b"async-testimony-audio")

        self._login()
        response = self.client.get("/admin/testimonies")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"function submissionUrl(form, submitter)", response.data)
        self.assertIn(b'getAttribute("formaction")', response.data)
        self.assertNotIn(b"submitter.formAction ? submitter.formAction : form.action", response.data)

    def test_bulk_testimony_suggestions_route_starts_background_job(self):
        testimony_source_root = self.root / "DN300R"
        testimony_source_root.mkdir()
        (testimony_source_root / "REC00100.mp3").write_bytes(b"raw-testimony-audio")

        self._login()
        with patch("ntc_recordings_app._start_testimony_suggestion_job", return_value=True) as starter:
            started = self.client.post(
                "/admin/testimonies/suggest-all",
                data={"status": "needs_review", "sort": "shortest"},
                follow_redirects=True,
            )

        self.assertEqual(started.status_code, 200)
        self.assertIn(b"Started testimony speaker suggestion processing", started.data)
        starter.assert_called_once()

        status = self.client.get("/admin/testimonies/suggest-status")
        self.assertEqual(status.status_code, 200)
        self.assertIn("state", status.get_json())

    def test_identified_testimony_transcript_route_starts_background_job(self):
        testimony_source_root = self.root / "DN300R"
        testimony_source_root.mkdir()
        (testimony_source_root / "REC00200.mp3").write_bytes(b"raw-testimony-audio")

        self._login()
        with patch("ntc_recordings_app._start_testimony_transcript_job", return_value=True) as starter:
            started = self.client.post(
                "/admin/testimonies/transcribe-identified",
                data={"status": "identified", "sort": "name"},
                follow_redirects=True,
            )

        self.assertEqual(started.status_code, 200)
        self.assertIn(b"Started testimony transcript processing", started.data)
        starter.assert_called_once()

        status = self.client.get("/admin/testimonies/transcript-status")
        self.assertEqual(status.status_code, 200)
        self.assertIn("state", status.get_json())

    def test_needs_review_testimony_transcript_route_targets_needs_review_rows(self):
        testimony_source_root = self.root / "DN300R"
        testimony_source_root.mkdir()
        recording = testimony_source_root / "REC00203.mp3"
        recording.write_bytes(b"needs-review-testimony-audio")
        recording_id = _recording_id(recording)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO testimony_reviews (
                    recording_id,
                    source_path,
                    status,
                    service_date,
                    updated_at
                )
                VALUES (?, ?, 'needs_review', '2026-05-24', ?)
                """,
                (recording_id, str(recording), datetime.now(timezone.utc).isoformat()),
            )

        statuses = _testimony_transcript_statuses_for_filter("needs_review")
        targets = _testimony_transcript_targets(self.app, statuses=statuses)
        self.assertEqual([Path(item["candidate"].path).name for item in targets], ["REC00203.mp3"])

        self._login()
        with patch("ntc_recordings_app._start_testimony_transcript_job", return_value=True) as starter:
            started = self.client.post(
                "/admin/testimonies/transcribe-identified",
                data={"status": "needs_review", "sort": "shortest"},
                follow_redirects=True,
            )

        self.assertEqual(started.status_code, 200)
        self.assertIn(b"Started testimony transcript processing", started.data)
        starter.assert_called_once()
        self.assertEqual(starter.call_args.kwargs["statuses"], {"needs_review"})

    def test_identified_testimony_transcripts_are_saved_and_skipped_afterwards(self):
        testimony_source_root = self.root / "DN300R"
        testimony_source_root.mkdir()
        recording = testimony_source_root / "REC00201.mp3"
        recording.write_bytes(b"identified-testimony-audio")
        recording_id = _recording_id(recording)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO testimony_reviews (
                    recording_id,
                    source_path,
                    status,
                    service_date,
                    speaker_name,
                    testimony_title,
                    updated_at
                )
                VALUES (?, ?, 'identified', '2026-05-24', 'Brother Prabhu', "Brother Prabhu's Testimony", ?)
                """,
                (recording_id, str(recording), datetime.now(timezone.utc).isoformat()),
            )

        targets = _testimony_transcript_targets(self.app)
        self.assertEqual([Path(item["candidate"].path).name for item in targets], ["REC00201.mp3"])

        self._login()
        review_before = self.client.get("/admin/testimonies?status=identified")
        self.assertEqual(review_before.status_code, 200)
        self.assertIn(b"Not processed yet", review_before.data)

        _save_testimony_transcript(
            self.app,
            recording_id,
            "Praise the Lord. I would like to thank God for helping me this week.",
            "transcript_excerpt",
            "",
        )

        self.assertEqual(_testimony_transcript_targets(self.app), [])
        review_after = self.client.get("/admin/testimonies?status=identified")
        self.assertIn(b"Stored testimony excerpt", review_after.data)
        self.assertIn(b"thank God for helping me", review_after.data)

    def test_identified_transcript_survives_testimony_rename(self):
        testimony_source_root = self.root / "DN300R"
        testimony_source_root.mkdir()
        recording = testimony_source_root / "REC00202.mp3"
        recording.write_bytes(b"identified-testimony-audio")
        recording_id = _recording_id(recording)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO testimony_reviews (
                    recording_id,
                    source_path,
                    status,
                    service_date,
                    speaker_name,
                    testimony_title,
                    transcript_text,
                    transcript_source,
                    transcript_updated_at,
                    updated_at
                )
                VALUES (?, ?, 'identified', '2026-05-24', 'Brother Prabhu', "Brother Prabhu's Testimony", ?, 'transcript_excerpt', ?, ?)
                """,
                (
                    recording_id,
                    str(recording),
                    "Praise the Lord. This transcript should stay with the renamed file.",
                    datetime.now(timezone.utc).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

        self._login()
        saved = self.client.post(
            f"/admin/testimonies/{recording_id}/review",
            data={
                "source_path": str(recording),
                "status_filter": "identified",
                "status": "identified",
                "service_date": "2026-05-24",
                "speaker_name": "Brother Prabhu Varghese",
            },
            follow_redirects=True,
        )

        self.assertEqual(saved.status_code, 200)
        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT speaker_name, transcript_text FROM testimony_reviews WHERE speaker_name = 'Brother Prabhu Varghese'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("transcript should stay", rows[0][1])

    def test_bulk_testimony_suggestions_skip_named_message_files(self):
        testimony_source_root = self.root / "DN300R"
        testimony_source_root.mkdir()
        raw_recording = testimony_source_root / "REC00101.mp3"
        raw_recording.write_bytes(b"raw-testimony-audio")
        named_message = testimony_source_root / "20260610 - God Is Able - Sis Judith.mp3"
        named_message.write_bytes(b"named-message-audio")

        with patch("ntc_recordings_app._probe_audio_duration", return_value=120):
            targets = _testimony_suggestion_targets(self.app)

        self.assertEqual([Path(item["candidate"].path).name for item in targets], ["REC00101.mp3"])
        with sqlite3.connect(self.db_path) as connection:
            named_row = connection.execute(
                "SELECT status FROM testimony_reviews WHERE recording_id = ?",
                (_recording_id(named_message),),
            ).fetchone()

        self.assertIsNotNone(named_row)
        self.assertEqual(named_row[0], "not_testimony")

    def test_bulk_testimony_suggestions_mark_long_message_like_rows(self):
        testimony_source_root = self.root / "DN300R"
        testimony_source_root.mkdir()
        message_recording = testimony_source_root / "REC00485.mp3"
        message_recording.write_bytes(b"long-message-audio")
        recording_id = _recording_id(message_recording)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO testimony_reviews (
                    recording_id,
                    source_path,
                    status,
                    service_date,
                    speaker_name,
                    testimony_title,
                    notes,
                    proposed_path,
                    duration_seconds,
                    suggested_speaker,
                    suggestion_source,
                    suggestion_text,
                    suggestion_updated_at,
                    updated_at
                )
                VALUES (?, ?, 'needs_review', '2026-05-31', '', '', '', '', ?, '', 'transcript_intro', ?, '', ?)
                """,
                (
                    recording_id,
                    str(message_recording),
                    3696,
                    "You may be seated. Shall we turn to 2 Samuel? This whole chapter is very interesting.",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )

        targets = _testimony_suggestion_targets(self.app)

        self.assertNotIn(recording_id, [item["candidate"].id for item in targets])
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT status, suggestion_text FROM testimony_reviews WHERE recording_id = ?",
                (recording_id,),
            ).fetchone()
        self.assertEqual(row[0], "not_testimony")
        self.assertIn("Shall we turn", row[1])

    def test_intro_speaker_suggestions_require_person_names(self):
        self.assertEqual(
            _extract_intro_speaker("Praise the Lord. For those of you who do not know me, my name is Kevin.", []),
            "Kevin",
        )
        self.assertEqual(
            _extract_intro_speaker("Praise the Lord, I'm Sister Shirley and I want to thank the Lord.", []),
            "Sister Shirley",
        )
        self.assertEqual(
            _extract_intro_speaker("Praise the Lord, my name is Rachel and I want to testify.", ["Sister Rachel"]),
            "Sister Rachel",
        )
        self.assertEqual(_extract_intro_speaker("Praise the Lord, my name is John C.", []), "John C")
        self.assertEqual(_extract_intro_speaker("This is for all of us as we worship today.", []), "")
        self.assertEqual(_extract_intro_speaker("I'm not going to give a long testimony today.", []), "")
        self.assertEqual(_extract_intro_speaker("I am deeply thankful for what God has done.", []), "")
        self.assertEqual(_extract_intro_speaker("Praise the Lord. I am happening to me in this situation.", []), "")
        self.assertEqual(_valid_person_name_suggestion("Happening To Me", []), "")

    def test_email_message_normalizes_escaped_newlines(self):
        message = "Praise the Lord,\\n\\nYour recording is ready.\\n\\nGod bless,\\nNTC Newark"

        normalized = _normalize_recording_email_message(message)

        self.assertEqual(normalized, "Praise the Lord,\n\nYour recording is ready.\n\nGod bless,\nNTC Newark")
        self.assertNotIn("\\n", normalized)

    def test_long_message_like_recordings_are_not_testimonies(self):
        self.assertTrue(
            _testimony_looks_like_message_recording(
                self.app,
                3700,
                "You may be seated. Shall we turn to Philippians chapter 2 in verses 12 and 13.",
            )
        )
        self.assertTrue(
            _testimony_looks_like_message_recording(
                self.app,
                1916,
                "Praise God. It is wonderful to see the wonderful work that God has done in our children. "
                "Shall we pray? Thank you for your word helping us, guiding us, directing us daily.",
                Path("/mnt/MainRecordings/Recordings/MessageRecordings/DN300R/REC00500.mp3"),
            )
        )
        self.assertTrue(
            _testimony_looks_like_message_recording(
                self.app,
                3548,
                "Oh, hallelujah. Those are wonderful words. Soon our Lord shall come in glory. "
                "Hallelujah. Are you ready, Brother Gerald? There is a pure river.",
                Path("/mnt/MainRecordings/Recordings/MessageRecordings/DN300R/REC00499.mp3"),
            )
        )
        self.assertTrue(_testimony_looks_like_message_recording(self.app, 15000, "Thank you."))
        self.assertFalse(
            _testimony_looks_like_message_recording(
                self.app,
                980,
                "Praise the Lord. My name is Nancy and I want to testify.",
            )
        )
        self.assertFalse(
            _testimony_looks_like_message_recording(
                self.app,
                3600,
                "Praise the Lord. My name is Cyril Joshua, husband to Sister Jenny.",
                Path("/mnt/MainRecordings/Recordings/TestimonyRecordings/2026/June 7, 2026 - Brother Cyril's Testimony.mp3"),
            )
        )
        self.assertFalse(
            _testimony_looks_like_message_recording(
                self.app,
                3700,
                "Praise the Lord. We thank God for each person sharing what the Lord has done.",
                Path("/mnt/MainRecordings/Recordings/TestimonyRecordings/2021/Funeral Testimonies/August 30, 2021 - Sister Marg's Funeral/Testimonies Part 1.mp3"),
            )
        )

    def test_legacy_testimony_source_config_still_works(self):
        legacy_root = self.root / "LegacyRecorder"
        legacy_root.mkdir()
        raw_recording = legacy_root / "REC00099.mp3"
        raw_recording.write_bytes(b"legacy-source-audio")

        app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "legacy-test-secret",
                "NTC_RECORDINGS_DB_PATH": str(Path(self.tempdir.name) / "legacy-recording-requests.db"),
                "NTC_RECORDINGS_LIBRARY_DIRS": f"message:{self.root},worship:{self.worship_root}",
                "NTC_RECORDINGS_DN300R_DIR": str(legacy_root),
                "NTC_RECORDINGS_ADMIN_PASSWORD": "admin-password",
            }
        )
        client = app.test_client()
        client.post("/admin/login", data={"password": "admin-password"})

        review = client.get("/admin/testimonies")

        self.assertEqual(review.status_code, 200)
        self.assertIn(b"REC00099", review.data)
        self.assertIn(b"Testimony Review", review.data)

    def test_metadata_dates_use_local_church_day(self):
        raw_recording = self.root / "REC00494.mp3"
        raw_recording.write_bytes(b"evening-service-audio")
        service_timestamp = datetime(2026, 6, 11, 0, 8, 32, tzinfo=timezone.utc).timestamp()
        os.utime(raw_recording, (service_timestamp, service_timestamp))

        self.assertEqual(_date_from_file_metadata(raw_recording.stat()), "2026-06-10")


if __name__ == "__main__":
    unittest.main()
