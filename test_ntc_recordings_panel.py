import errno
import json
import os
import tempfile
import unittest
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from ntc_recordings_app import (
    RecordingCandidate,
    _automatic_review_analysis_ids,
    _background_review_analysis_ids,
    _date_from_file_metadata,
    _display_transcript_text,
    _extract_intro_speaker,
    _humanize_classifier_evidence,
    _normalize_recording_email_message,
    _post_transcription_audio,
    _queue_testimony_deliveries,
    _record_testimony_review_history,
    _recorder_agent_reason_label,
    _recorder_review_display_kind,
    _recorder_transcript_windows,
    _recording_id,
    _run_testimony_transcript_job,
    _run_testimony_delivery_job,
    _save_service_exception,
    _save_testimony_delivery_rule,
    _save_testimony_review,
    _sanitize_existing_testimony_transcript_errors,
    _sanitize_existing_testimony_transcripts,
    _sort_testimony_items,
    _service_completeness_rows,
    _sync_testimony_recorder_manifest_reviews,
    _testimony_filename_speaker_suggestion,
    _testimony_looks_like_message_recording,
    _testimony_review_items,
    _testimony_suggestion_targets,
    _testimony_transcript_statuses_for_filter,
    _testimony_transcript_targets,
    _transcribe_testimony_review_excerpt,
    _save_testimony_transcript,
    _transcript_contains_prompt_echo,
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
                "NTC_RECORDINGS_TESTIMONY_SOURCE_DIR": str(self.root / "TestimonyReviewQueue"),
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
        return self.client.post(
            "/admin/login",
            data={
                "password": "admin-password",
            },
            follow_redirects=True,
        )

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

        testimony_source_root = self.root / "TestimonyReviewQueue"
        testimony_source_root.mkdir(exist_ok=True)
        (testimony_source_root / "REC00123.mp3").write_bytes(b"prefix-testimony-audio")

        review = self.client.get("/admin/recorder-review", base_url="https://ntcnas.myftp.org")

        self.assertEqual(review.status_code, 200)
        self.assertIn(b'href="/recordings/admin/panel"', review.data)
        self.assertIn(b'data-status-url="/recordings/admin/testimonies/transcript-status"', review.data)
        self.assertIn(b'action="/recordings/admin/testimonies/', review.data)
        self.assertNotIn(b'formaction="/recordings/admin/testimonies/', review.data)
        self.assertIn(b'data-src="/recordings/admin/testimonies/audio/', review.data)
        legacy_review = self.client.get("/admin/testimonies?status=all", base_url="https://ntcnas.myftp.org")
        self.assertEqual(legacy_review.status_code, 302)
        self.assertEqual(legacy_review.headers["Location"], "/recordings/admin/recorder-review?status=all")

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
        self.assertEqual(put.call_args.kwargs["data"]["hideDownload"], "true")
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
        self.assertEqual(put.call_args.kwargs["data"]["hideDownload"], "true")

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
        self.assertEqual(put.call_args.kwargs["data"]["hideDownload"], "true")
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
        testimony_source_root = self.root / "TestimonyReviewQueue"
        testimony_source_root.mkdir()
        raw_recording = testimony_source_root / "REC00042.mp3"
        raw_recording.write_bytes(b"raw-testimony-audio")
        service_timestamp = datetime(2026, 4, 19, 12, tzinfo=timezone.utc).timestamp()
        os.utime(raw_recording, (service_timestamp, service_timestamp))
        (testimony_source_root / "20250413 - Sister Rachel's Testimony.mp3").write_bytes(b"named-testimony-audio")

        denied = self.client.get("/admin/recorder-review")
        self.assertEqual(denied.status_code, 302)

        self._login()
        review = self.client.get("/admin/recorder-review")

        self.assertEqual(review.status_code, 200)
        self.assertIn(b"Recorder Review", review.data)
        self.assertIn(b"REC00042", review.data)
        self.assertNotIn(b"Check Durations", review.data)
        self.assertNotIn(b">Probe</span>", review.data)
        self.assertIn(b"Save Review", review.data)
        self.assertIn(b"Save Selected Reviews", review.data)
        self.assertIn(b"Discard Selected", review.data)
        self.assertIn(b'<div class="bulk-toolbar" data-bulk-toolbar hidden>', review.data)
        self.assertIn(b'<div class="bulk-selection" data-bulk-selection>', review.data)
        self.assertNotIn(b'<div class="bulk-selection" data-bulk-selection hidden>', review.data)
        self.assertIn(b'data-expand-all>Expand All</button>', review.data)
        self.assertIn(b'data-close-all>Close All</button>', review.data)
        analysis_position = review.data.index(b'data-transcript-job')
        expand_position = review.data.index(b'data-expand-all')
        bulk_position = review.data.index(b'data-bulk-toolbar')
        self.assertLess(analysis_position, expand_position)
        self.assertLess(expand_position, bulk_position)
        self.assertIn(b".bulk-toolbar[hidden] { display:none; }", review.data)
        self.assertIn(b"min-height:2.35rem;", review.data)
        self.assertNotIn(b"Keep for Review", review.data)
        self.assertNotIn(b"Return to Review", review.data)
        self.assertIn(b"Mark Duplicate", review.data)
        self.assertIn(b"Listen, confirm the service date", review.data)
        self.assertIn(b'preload="none" data-src="/admin/testimonies/audio/', review.data)
        self.assertNotIn(b'preload="metadata" src="/admin/testimonies/audio/', review.data)
        self.assertNotIn(b">Suggest Speaker</button>", review.data)
        self.assertNotIn(b"Process Suggestions", review.data)
        self.assertIn(b"Group / Event Title", review.data)
        self.assertIn(b"Grouped", review.data)
        self.assertNotIn(b"Retry Missing Analysis", review.data)
        self.assertNotIn(b"Recording Shape", review.data)
        self.assertNotIn(b"Recording Structure", review.data)
        self.assertNotIn(b">Retry Analysis</button>", review.data)
        self.assertIn(b"Recording Type", review.data)
        self.assertNotIn(b">Save Type</button>", review.data)
        self.assertNotIn(b">Save Grouped</button>", review.data)
        self.assertNotIn(b">Save Speaker</button>", review.data)
        self.assertNotIn(b">Message/Event", review.data)
        self.assertNotIn(b"Process Transcripts", review.data)
        self.assertNotIn(b"Quarantine Rejected", review.data)
        self.assertNotIn(b'data-suggestion-job', review.data)
        self.assertNotIn(b'data-status-url="/admin/testimonies/suggest-status"', review.data)
        self.assertIn(b'data-transcript-job', review.data)
        self.assertIn(b'data-status-url="/admin/testimonies/transcript-status"', review.data)
        self.assertIn(b'data-review-id="', review.data)
        self.assertIn(b'data-row-selector role="checkbox" aria-checked="false"', review.data)
        self.assertIn(b'data-row-select tabindex="-1" aria-hidden="true"', review.data)
        self.assertIn(b'<span class="row-number" data-row-number>#1</span>', review.data)
        self.assertIn(b"runBulkReview", review.data)
        self.assertIn(b"setAllReviewCardsOpen", review.data)
        self.assertIn(b"card.open = isOpen", review.data)
        self.assertIn(b"renumberReviewRows", review.data)
        self.assertIn(b"pauseCardAudio", review.data)
        self.assertIn(b"pauseOtherReviewAudio", review.data)
        self.assertIn(b'document.addEventListener("play"', review.data)
        self.assertIn(b"ntc-recorder-review-open-cards", review.data)
        self.assertIn(b"flex-wrap:nowrap;", review.data)
        self.assertIn(b"overflow-x:auto;", review.data)
        self.assertIn(b".tab { width:auto; justify-content:flex-start; }", review.data)
        self.assertIn(b"grid-template-columns:repeat(6,minmax(0,1fr));", review.data)
        self.assertNotIn(b".metric:last-child { grid-column:1 / -1; }", review.data)
        self.assertIn(b"header { position:relative; display:block; }", review.data)
        self.assertIn(b"header h1 { margin-top:.48rem; font-size:2rem; line-height:1; letter-spacing:0; }", review.data)
        self.assertIn(b"header .actions a { width:auto; min-width:0; }", review.data)
        self.assertIn(b"grid-template-columns:minmax(0,1fr) 4.7rem auto;", review.data)
        self.assertIn(b".metrics { grid-template-columns:repeat(2,minmax(0,1fr)); }", review.data)
        self.assertIn(b".listen-panel audio { display:block; max-width:100%; min-width:0; }", review.data)
        self.assertIn(b"grid-template-columns:minmax(0,1fr);", review.data)
        self.assertIn(b".listen-panel > * { min-width:0; max-width:100%; }", review.data)
        self.assertIn(b".file-facts { grid-template-columns:minmax(0,1fr); }", review.data)
        self.assertIn(b"@media (max-width:350px)", review.data)
        self.assertIn(b".panel-controls > .probe-form > button { grid-column:1 / -1; }", review.data)
        self.assertIn(b"X-Requested-With", review.data)
        self.assertIn(b'id="speaker-name-options"', review.data)
        self.assertNotIn(b"legacy source folder", review.data)
        self.assertNotIn(b"Final Title", review.data)
        self.assertNotIn(b"Voice / ID Notes", review.data)
        self.assertNotIn(b"Proposed Destination", review.data)
        self.assertNotIn(b"Already Named", review.data)
        self.assertNotIn(b"20250413 - Sister Rachel", review.data)

        recording_id = _recording_id(raw_recording)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO testimony_reviews (
                    recording_id,
                    source_path,
                    recorder_segment_kind,
                    recorder_segment_count,
                    updated_at
                )
                VALUES (?, ?, 'combined', 2, ?)
                """,
                (recording_id, str(raw_recording), datetime.now(timezone.utc).isoformat()),
            )
            connection.commit()
        classified = self.client.get("/admin/recorder-review")
        self.assertIn(b"Sections", classified.data)
        self.assertNotIn(b"Recording Structure", classified.data)
        self.assertNotIn(b"Recording Shape", classified.data)

        identified = self.client.get("/admin/recorder-review?status=identified")
        self.assertEqual(identified.status_code, 200)
        self.assertIn(b"20250413 - Sister Rachel", identified.data)
        self.assertNotIn(b"Retry Missing Analysis", identified.data)

        audio = self.client.get(f"/admin/testimonies/audio/{recording_id}")
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.data, b"raw-testimony-audio")

        with patch("ntc_recordings_app._start_testimony_transcript_job", return_value=True) as starter:
            queued = self.client.post(
                f"/admin/testimonies/{recording_id}/suggest",
                data={
                    "status_filter": "needs_review",
                    "sort": "shortest",
                    "source_path": str(raw_recording),
                    "service_date": "2026-04-19",
                },
                headers={"Accept": "application/json", "X-Requested-With": "fetch"},
            )

        self.assertEqual(queued.status_code, 202)
        self.assertTrue(queued.get_json()["analysis_queued"])
        self.assertIn("Recorder analysis queued", queued.get_json()["message"])
        starter.assert_called_once()
        self.assertEqual(starter.call_args.kwargs["recording_ids"], {recording_id})

        with patch(
            "ntc_recordings_app._transcribe_testimony_review_excerpt",
            return_value=("For those of you who do not know me, my name is Kevin. I want to thank the Lord.", ""),
        ):
            _run_testimony_transcript_job(
                self.app,
                limit=1,
                statuses={"needs_review"},
                recording_ids={recording_id},
            )
        suggested = self.client.get("/admin/recorder-review")
        self.assertEqual(suggested.status_code, 200)
        self.assertIn(b"Suggested Speaker", suggested.data)
        self.assertIn(b"Kevin", suggested.data)
        self.assertIn(b"from transcript", suggested.data)
        self.assertIn(b"Use Suggestion", suggested.data)
        self.assertNotIn(b"Intro transcript", suggested.data)
        self.assertNotIn(b"Intro checked", suggested.data)
        self.assertLess(
            suggested.data.index(b"<span>Suggested Speaker</span>"),
            suggested.data.index(b"<span>Transcript</span>"),
        )
        self.assertIn(b"Type speaker name", suggested.data)

        with sqlite3.connect(self.db_path) as connection:
            suggestion_row = connection.execute(
                "SELECT status, suggested_speaker, suggestion_source, suggestion_text FROM testimony_reviews WHERE recording_id = ?",
                (recording_id,),
            ).fetchone()

        self.assertIsNotNone(suggestion_row)
        self.assertEqual(suggestion_row[0], "needs_review")
        self.assertEqual(suggestion_row[1], "Kevin")
        self.assertEqual(suggestion_row[2], "transcript_excerpt")
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
        self.assertEqual(preserved_suggestion_row[1], "transcript_excerpt")
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
        self.assertIn(b"Recorder review saved and filed as", saved.data)
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

        identified_after_save = self.client.get("/admin/recorder-review?status=identified")
        self.assertEqual(identified_after_save.status_code, 200)
        self.assertIn(b"Sister Test", identified_after_save.data)
        self.assertIn(b"April 19, 2026 - Sister Test", identified_after_save.data)

        renamed_audio = self.client.get(f"/admin/testimonies/audio/{new_recording_id}")
        self.assertEqual(renamed_audio.status_code, 200)
        self.assertEqual(renamed_audio.data, b"raw-testimony-audio")

    def test_recorder_review_uses_one_context_aware_save_and_rejects_stale_edits(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
        testimony_source_root.mkdir()
        raw_recording = testimony_source_root / "REC00077.mp3"
        raw_recording.write_bytes(b"recorder-review-audio")
        recording_id = _recording_id(raw_recording)

        self._login()
        review = self.client.get("/admin/recorder-review")
        self.assertIn(b'name="review_revision" value="0"', review.data)

        first = self.client.post(
            f"/admin/testimonies/{recording_id}/review",
            data={
                "action": "save_review",
                "status_filter": "needs_review",
                "source_path": str(raw_recording),
                "service_date": "2026-07-26",
                "recording_kind": "message",
                "speaker_name": "Brother Blessen",
                "group_title": "The Lord Is Faithful",
                "review_revision": "0",
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )

        self.assertEqual(first.status_code, 200)
        first_payload = first.get_json()
        self.assertTrue(first_payload["ok"])
        self.assertEqual(first_payload["status"], "identified")
        self.assertEqual(first_payload["recording_kind"], "message")
        self.assertEqual(first_payload["group_title"], "The Lord Is Faithful")
        self.assertEqual(first_payload["review_revision"], 1)
        filed_path = (
            self.root
            / "2026"
            / "Sunday Messages"
            / "July"
            / "July 26, 2026 - The Lord Is Faithful - Brother Blessen.mp3"
        )
        filed_recording_id = _recording_id(filed_path)
        self.assertFalse(raw_recording.exists())
        self.assertTrue(filed_path.exists())

        stale = self.client.post(
            f"/admin/testimonies/{filed_recording_id}/review",
            data={
                "action": "save_review",
                "status_filter": "identified",
                "source_path": str(filed_path),
                "service_date": "2026-07-26",
                "recording_kind": "message",
                "speaker_name": "Brother Blessen",
                "group_title": "Stale overwrite",
                "review_revision": "0",
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )

        self.assertEqual(stale.status_code, 409)
        self.assertIn("Another reviewer updated", stale.get_json()["error"])
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT status,
                       testimony_title,
                       recorder_agent_kind,
                       review_revision,
                       reviewed_by,
                       review_source,
                       reviewed_at
                FROM testimony_reviews
                WHERE recording_id = ?
                """,
                (filed_recording_id,),
            ).fetchone()
            history = connection.execute(
                """
                SELECT action,
                       previous_status,
                       new_status,
                       recording_kind,
                       reviewed_by,
                       review_source,
                       reviewed_at
                FROM recorder_review_history
                WHERE recording_id = ?
                """,
                (filed_recording_id,),
            ).fetchall()
            self.assertEqual(
                row[:6],
                (
                    "identified",
                    "The Lord Is Faithful",
                    "message",
                    1,
                    "Recordings Admin",
                    "recorder_review_ui",
                ),
            )
            self.assertTrue(row[6])
            self.assertEqual(
                history[0][:6],
                (
                    "save_review",
                    "needs_review",
                    "identified",
                    "message",
                    "Recordings Admin",
                    "recorder_review_ui",
                ),
            )
            self.assertTrue(history[0][6])

    def test_ntc_recorder_review_files_confirmed_worship_in_worship_library(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
        testimony_source_root.mkdir()
        raw_recording = testimony_source_root / "07262026115900_DN-700R.mp3"
        raw_recording.write_bytes(b"ntc-dn700r-audio")
        recording_id = _recording_id(raw_recording)

        self._login()
        response = self.client.post(
            f"/admin/testimonies/{recording_id}/review",
            data={
                "action": "save_review",
                "status_filter": "needs_review",
                "source_path": str(raw_recording),
                "service_date": "2026-07-26",
                "recording_kind": "worship",
                "review_revision": "0",
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["recording_kind"], "worship")
        self.assertEqual(payload["status"], "identified")
        filed_path = (
            self.worship_root
            / "2026"
            / "July"
            / "July 26, 2026 - Sunday Service"
            / "LR"
            / "July 26, 2026 - NTCWorship1159 - LR.mp3"
        )
        filed_recording_id = _recording_id(filed_path)
        self.assertFalse(raw_recording.exists())
        self.assertTrue(filed_path.exists())
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT status, recorder_agent_kind, source_path FROM testimony_reviews WHERE recording_id = ?",
                (filed_recording_id,),
            ).fetchone()
        self.assertEqual(row, ("identified", "worship", str(filed_path)))

    def test_combined_recording_stays_in_review_for_splitting(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
        testimony_source_root.mkdir()
        raw_recording = testimony_source_root / "07262026150000_DN-700R.mp3"
        raw_recording.write_bytes(b"combined-recorder-audio")
        recording_id = _recording_id(raw_recording)

        _save_testimony_review(
            self.app,
            recording_id=recording_id,
            source_path=str(raw_recording),
            status="needs_review",
            service_date="2026-07-26",
            speaker_name="",
            testimony_title="Sunday Service",
            notes="",
            proposed_path="",
            duration_seconds=3600,
            recorder_agent_kind="combined",
            recorder_agent_action="split",
            recorder_agent_reason="Multiple recording sections were detected.",
        )
        self._login()
        review = self.client.get("/admin/recorder-review")
        self.assertIn(b'<option value="combined" selected>Combined - Needs Splitting</option>', review.data)
        self.assertIn(b'value="Sunday Service"', review.data)

        response = self.client.post(
            f"/admin/testimonies/{recording_id}/review",
            data={
                "action": "save_review",
                "status_filter": "needs_review",
                "source_path": str(raw_recording),
                "service_date": "2026-07-26",
                "recording_kind": "combined",
                "group_title": "Sunday Service",
                "review_revision": "0",
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["recording_kind"], "combined")
        self.assertEqual(payload["status"], "needs_review")
        self.assertTrue(raw_recording.exists())
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT status, testimony_title, recorder_agent_kind, source_path FROM testimony_reviews WHERE recording_id = ?",
                (recording_id,),
            ).fetchone()
        self.assertEqual(row, ("needs_review", "Sunday Service", "combined", str(raw_recording)))

    def test_background_analysis_preserves_human_review_fields(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
        testimony_source_root.mkdir()
        raw_recording = testimony_source_root / "REC00078.mp3"
        raw_recording.write_bytes(b"recorder-review-audio")
        recording_id = _recording_id(raw_recording)

        _save_testimony_review(
            self.app,
            recording_id=recording_id,
            source_path=str(raw_recording),
            status="identified",
            service_date="2026-07-26",
            speaker_name="Jeffrey Jeeva",
            testimony_title="Jeffrey Jeeva's Testimony",
            notes="human review",
            proposed_path="/final/testimony.mp3",
            duration_seconds=90,
        )
        _save_testimony_review(
            self.app,
            recording_id=recording_id,
            source_path=str(raw_recording),
            status="needs_review",
            service_date="2026-07-27",
            speaker_name="",
            testimony_title="",
            notes="stale background row",
            proposed_path="",
            duration_seconds=91,
            suggested_speaker="Jeff",
            suggestion_source="recorder_agent",
            suggestion_text="Automatic analysis",
            recorder_agent_kind="testimony",
            update_review_fields=False,
        )

        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT status, service_date, speaker_name, testimony_title, notes,
                       proposed_path, duration_seconds, suggested_speaker
                FROM testimony_reviews
                WHERE recording_id = ?
                """,
                (recording_id,),
            ).fetchone()
        self.assertEqual(
            row,
            (
                "identified",
                "2026-07-26",
                "Jeffrey Jeeva",
                "Jeffrey Jeeva's Testimony",
                "human review",
                "/final/testimony.mp3",
                91,
                "Jeff",
            ),
        )

    def test_testimony_review_can_mark_duplicate_recordings(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
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

        needs_review = self.client.get("/admin/recorder-review?status=needs_review").data
        self.assertIn(b"REC00198", needs_review)
        self.assertNotIn(b"REC10199", needs_review)

        duplicate = self.client.get("/admin/recorder-review?status=duplicate").data
        self.assertIn(b"REC10199", duplicate)
        self.assertIn(b"Duplicate", duplicate)
        self.assertIn(b"Quarantine Duplicates", duplicate)

        all_items = self.client.get("/admin/recorder-review?status=all").data
        self.assertIn(b"REC00198", all_items)
        self.assertIn(b"REC10199", all_items)

    def test_testimony_review_requires_speaker_before_identifying(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
        testimony_source_root.mkdir()
        raw_recording = testimony_source_root / "REC00088.mp3"
        raw_recording.write_bytes(b"raw-testimony-audio")
        recording_id = _recording_id(raw_recording)

        self._login()
        response = self.client.post(
            f"/admin/testimonies/{recording_id}/review",
            data={
                "status": "identified",
                "status_filter": "needs_review",
                "source_path": str(raw_recording),
                "service_date": "2026-03-15",
                "speaker_name": "",
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertIn("Enter a speaker or group/event title", payload["error"])
        self.assertTrue(raw_recording.exists())
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT status FROM testimony_reviews WHERE recording_id = ?",
                (recording_id,),
            ).fetchone()
        self.assertIsNone(row)

    def test_funeral_date_testimony_saves_to_funeral_folder(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
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
        testimony_source_root = self.root / "TestimonyReviewQueue"
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
        grouped = self.client.get("/admin/recorder-review?status=grouped").data
        self.assertIn(b"Testimonies Part 1", grouped)
        self.assertIn(b"Grouped", grouped)
        self.assertNotIn(b"Retry Missing Analysis", grouped)

    def test_testimony_review_quarantines_rejected_recordings(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
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

        duplicate = self.client.get("/admin/recorder-review?status=duplicate").data
        self.assertNotIn(b"REC10199", duplicate)

        audio = self.client.get(f"/admin/testimonies/audio/{duplicate_id}")
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.data, b"same-testimony-content-duplicate")

    def test_testimony_review_supports_json_row_updates(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
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
        testimony_source_root = self.root / "TestimonyReviewQueue"
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

    def test_recorder_intake_file_can_be_saved_to_testimony_library(self):
        intake_root = Path(self.tempdir.name) / "_IncomingRecorderIntake"
        recorder_root = intake_root / "DN700R-primary"
        recorder_root.mkdir(parents=True)
        raw_recording = recorder_root / "07262026115900_DN-700R.mp3"
        raw_recording.write_bytes(b"recorder-intake-audio")
        recording_id = _recording_id(raw_recording)
        self.app.config["NTC_RECORDINGS_TESTIMONY_ALLOWED_DIRS"] = str(intake_root)

        self._login()
        response = self.client.post(
            f"/admin/testimonies/{recording_id}/review",
            data={
                "status": "identified",
                "status_filter": "needs_review",
                "source_path": str(raw_recording),
                "service_date": "2026-07-26",
                "speaker_name": "Jeffrey Paul Johnson",
            },
            headers={"Accept": "application/json", "X-Requested-With": "fetch"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertFalse(raw_recording.exists())
        saved_path = Path(payload["source_path"])
        self.assertTrue(saved_path.exists())
        self.assertEqual(saved_path.parent, self.testimony_root / "2026" / "Sunday Testimonies")
        self.assertEqual(saved_path.name, "July 26, 2026 - Jeffrey Paul Johnson's Testimony.mp3")

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
        testimony_source_root = self.root / "TestimonyReviewQueue"
        testimony_source_root.mkdir()
        (testimony_source_root / "REC00088.mp3").write_bytes(b"async-testimony-audio")

        self._login()
        response = self.client.get("/admin/recorder-review")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"function submissionUrl(form, submitter)", response.data)
        self.assertIn(b'getAttribute("formaction")', response.data)
        self.assertNotIn(b"submitter.formAction ? submitter.formAction : form.action", response.data)

    def test_bulk_testimony_suggestions_route_starts_background_job(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
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
        self.assertIn(b"Started recorder suggestion processing", started.data)
        starter.assert_called_once()

        status = self.client.get("/admin/testimonies/suggest-status")
        self.assertEqual(status.status_code, 200)
        self.assertIn("state", status.get_json())

    def test_identified_testimony_transcript_route_starts_background_job(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
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
        self.assertIn(b"Started retrying missing recorder analysis", started.data)
        starter.assert_called_once()

        status = self.client.get("/admin/testimonies/transcript-status")
        self.assertEqual(status.status_code, 200)
        self.assertIn("state", status.get_json())

    def test_needs_review_testimony_transcript_route_targets_needs_review_rows(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
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
        self.assertIn(b"Started retrying missing recorder analysis", started.data)
        starter.assert_called_once()
        self.assertEqual(starter.call_args.kwargs["statuses"], {"needs_review", "message_review"})

    def test_identified_testimony_transcripts_are_saved_and_skipped_afterwards(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
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
        review_before = self.client.get("/admin/recorder-review?status=identified")
        self.assertEqual(review_before.status_code, 200)
        self.assertIn(b"Automatic analysis is queued.", review_before.data)
        self.assertIn(b"<span>Recording Type</span>", review_before.data)
        self.assertIn(b'data-field="recording-kind-label">Testimony</strong>', review_before.data)
        self.assertIn(b"No alternate suggestion", review_before.data)
        self.assertIn(b"choose Testimony, Message, Worship, or Combined", review_before.data)
        self.assertIn(b'<option value="worship"', review_before.data)
        self.assertIn(b'<option value="combined"', review_before.data)

        _save_testimony_transcript(
            self.app,
            recording_id,
            "Praise the Lord. I would like to thank God for helping me this week.",
            "chunked-transcript-v1",
            "",
        )

        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE testimony_reviews
                SET recorder_agent_reason = ?
                WHERE recording_id = ?
                """,
                (
                    "recorder-review-v4: Automatic transcript classification completed.",
                    recording_id,
                ),
            )

        self.assertEqual(_testimony_transcript_targets(self.app), [])
        review_after = self.client.get("/admin/recorder-review?status=identified")
        self.assertNotIn(b"Stored testimony excerpt", review_after.data)
        self.assertNotIn(b"Intro checked", review_after.data)
        self.assertIn(b"<span>Transcript</span>", review_after.data)
        self.assertIn(b"View full transcript", review_after.data)
        self.assertIn(b"thank God for helping me", review_after.data)

    def test_transcript_display_hides_internal_window_markers(self):
        cleaned = _display_transcript_text(
            "[start] Praise the Lord. I thank God for His faithfulness.\n\n"
            "[+158s] [no transcription returned]\n\n"
            "[+417s] God has helped our family through every season."
        )

        self.assertEqual(
            cleaned,
            "Praise the Lord. I thank God for His faithfulness.\n\n"
            "God has helped our family through every season.",
        )

    def test_recorder_review_display_type_uses_configured_physical_library(self):
        candidate = RecordingCandidate(
            id=_recording_id(self.recording),
            path=str(self.recording),
            title=self.recording.stem,
            recording_date="2026-04-19",
            kind="message",
            size_bytes=self.recording.stat().st_size,
            modified_at=datetime.now(timezone.utc).isoformat(),
            relative_path=self.recording.name,
        )

        self.assertEqual(
            _recorder_review_display_kind(self.app, candidate, None, "identified"),
            "message",
        )

    def test_prompt_only_transcript_is_rejected_and_retried(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
        testimony_source_root.mkdir()
        recording = testimony_source_root / "07262026115802_DN-700R.mp3"
        recording.write_bytes(b"prompt-echo-testimony-audio")
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
                VALUES (?, ?, 'needs_review', '2026-07-26', ?)
                """,
                (recording_id, str(recording), datetime.now(timezone.utc).isoformat()),
            )

        prompt_echo = (
            "[start] introductions, and whether this sounds like a personal testimony "
            "or a preached message.\n"
            "introductions, and whether this sounds like a personal testimony "
            "or a preached message."
        )
        self.assertTrue(_transcript_contains_prompt_echo(prompt_echo))
        self.assertEqual(_display_transcript_text(prompt_echo), "")

        _save_testimony_transcript(
            self.app,
            recording_id,
            prompt_echo,
            "recorder_manifest",
            "",
        )

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT transcript_text, transcript_source, transcript_error
                FROM testimony_reviews
                WHERE recording_id = ?
                """,
                (recording_id,),
            ).fetchone()
        self.assertEqual(row["transcript_text"], "")
        self.assertEqual(row["transcript_source"], "")
        self.assertIn("transcription instructions", row["transcript_error"])
        targets = _testimony_transcript_targets(self.app, statuses={"needs_review"})
        self.assertEqual([Path(item["candidate"].path).name for item in targets], [recording.name])
        self._login()
        review = self.client.get("/admin/recorder-review?status=needs_review")
        self.assertNotIn(b"whether this sounds like", review.data)
        self.assertNotIn(b">Testimony</strong>", review.data)
        self.assertIn(b"Automatic transcript was rejected", review.data)
        self.assertNotIn(b">Retry Analysis</button>", review.data)

    def test_existing_prompt_only_transcript_is_migrated_to_retry(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
        testimony_source_root.mkdir()
        recording = testimony_source_root / "REC00999.mp3"
        recording.write_bytes(b"prompt-echo-testimony-audio")
        recording_id = _recording_id(recording)
        prompt_echo = (
            "[start] Preserve speaker names, scripture references, sermon introductions, "
            "and whether this sounds like a personal testimony or a preached message."
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO testimony_reviews (
                    recording_id,
                    source_path,
                    status,
                    service_date,
                    transcript_text,
                    transcript_source,
                    recorder_agent_kind,
                    recorder_agent_action,
                    updated_at
                )
                VALUES (?, ?, 'needs_review', '2026-07-26', ?, 'recorder_manifest', 'testimony', 'review', ?)
                """,
                (
                    recording_id,
                    str(recording),
                    prompt_echo,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.row_factory = sqlite3.Row
            migrated = _sanitize_existing_testimony_transcripts(connection)

        self.assertEqual(migrated, 1)
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT transcript_text, transcript_source, transcript_error,
                       recorder_agent_kind, recorder_agent_action
                FROM testimony_reviews
                WHERE recording_id = ?
                """,
                (recording_id,),
            ).fetchone()
        self.assertEqual(row["transcript_text"], "")
        self.assertEqual(row["transcript_source"], "")
        self.assertIn("transcription instructions", row["transcript_error"])
        self.assertEqual(row["recorder_agent_kind"], "unknown")
        self.assertEqual(row["recorder_agent_action"], "review")

    def test_transcription_transport_error_is_sanitized_and_migrated(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
        testimony_source_root.mkdir()
        recording = testimony_source_root / "07262026115802_DN-700R.mp3"
        recording.write_bytes(b"rate-limited-testimony-audio")
        recording_id = _recording_id(recording)
        raw_error = (
            "Transcription request failed: 429 Client Error: Too Many Requests for url: "
            "http://100.109.220.95:8766/transcription?prompt=internal"
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO testimony_reviews (
                    recording_id,
                    source_path,
                    status,
                    service_date,
                    transcript_error,
                    updated_at
                )
                VALUES (?, ?, 'needs_review', '2026-07-26', ?, ?)
                """,
                (
                    recording_id,
                    str(recording),
                    raw_error,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.row_factory = sqlite3.Row
            migrated = _sanitize_existing_testimony_transcript_errors(connection)

        self.assertEqual(migrated, 1)
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT transcript_error
                FROM testimony_reviews
                WHERE recording_id = ?
                """,
                (recording_id,),
            ).fetchone()
        self.assertEqual(
            row["transcript_error"],
            "Automatic transcription is busy. This recording remains queued for retry.",
        )

        _save_testimony_transcript(self.app, recording_id, "", "", raw_error)
        self._login()
        review = self.client.get("/admin/recorder-review?status=needs_review")
        self.assertIn(b"Automatic transcription is busy", review.data)
        self.assertNotIn(b"429 Client Error", review.data)
        self.assertNotIn(b"100.109.220.95", review.data)

    def test_transcription_request_waits_for_capacity_then_succeeds(self):
        busy_response = requests.Response()
        busy_response.status_code = 429
        busy_response.headers["Retry-After"] = "0"
        busy_response.url = "http://transcription.example.test/transcription"
        success_response = requests.Response()
        success_response.status_code = 200
        success_response._content = b'{"text":"Praise the Lord."}'
        self.app.config["NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_BUSY_WAIT"] = 30

        with (
            patch(
                "ntc_recordings_app.requests.post",
                side_effect=[busy_response, success_response],
            ) as post,
            patch("ntc_recordings_app.time.sleep") as sleep,
        ):
            response = _post_transcription_audio(
                self.app,
                "http://transcription.example.test/transcription",
                params={"language": "en"},
                data=b"audio",
                timeout=600,
            )

        self.assertIs(response, success_response)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once()

    def test_transcription_request_stops_after_busy_wait_budget(self):
        busy_response = requests.Response()
        busy_response.status_code = 429
        busy_response.url = "http://transcription.example.test/transcription"
        self.app.config["NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_BUSY_WAIT"] = 0

        with patch("ntc_recordings_app.requests.post", return_value=busy_response) as post:
            with self.assertRaises(requests.HTTPError):
                _post_transcription_audio(
                    self.app,
                    "http://transcription.example.test/transcription",
                    params={"language": "en"},
                    data=b"audio",
                    timeout=600,
                )

        post.assert_called_once()

    def test_identified_transcript_survives_testimony_rename(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
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

    def test_identified_view_bulk_renames_exact_speaker_and_preserves_metadata(self):
        first = self.testimony_root / "2025" / "Sunday Testimonies" / "May 4, 2025 - Sister Renny's Testimony.mp3"
        second = self.testimony_root / "2026" / "Sunday Testimonies" / "May 3, 2026 - Sister Renny's Testimony.mp3"
        unrelated = self.testimony_root / "2026" / "Sunday Testimonies" / "May 10, 2026 - Sister Rachel's Testimony.mp3"
        for path, payload in (
            (first, b"first-testimony"),
            (second, b"second-testimony"),
            (unrelated, b"unrelated-testimony"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as connection:
            for path, service_date, speaker, transcript in (
                (first, "2025-05-04", "Sister Renny", "first transcript"),
                (second, "2026-05-03", "Sister Renny", "second transcript"),
                (unrelated, "2026-05-10", "Sister Rachel", "unrelated transcript"),
            ):
                connection.execute(
                    """
                    INSERT INTO testimony_reviews (
                        recording_id, source_path, status, service_date, speaker_name,
                        testimony_title, transcript_text, transcript_source,
                        transcript_updated_at, recorder_agent_kind, updated_at
                    )
                    VALUES (?, ?, 'identified', ?, ?, ?, ?, 'full_transcript', ?, 'testimony', ?)
                    """,
                    (
                        _recording_id(path),
                        str(path),
                        service_date,
                        speaker,
                        f"{speaker}'s Testimony",
                        transcript,
                        now,
                        now,
                    ),
                )
            connection.execute(
                """
                INSERT INTO testimony_delivery_rules (
                    canonical_name, aliases_json, emails_json, effective_from,
                    enabled, created_at, updated_at
                )
                VALUES ('Sister Renny', '[]', '["renny@example.test"]', '2026-01-01', 1, ?, ?)
                """,
                (now, now),
            )
            rule_id = connection.execute(
                "SELECT id FROM testimony_delivery_rules WHERE canonical_name = 'Sister Renny'"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO testimony_deliveries (
                    rule_id, source_recording_id, recording_id, recording_path,
                    recording_title, speaker_name, service_date, status,
                    created_at, updated_at
                )
                VALUES (?, 'original-source', ?, ?, ?, 'Sister Renny', '2026-05-03', 'sent', ?, ?)
                """,
                (rule_id, _recording_id(second), str(second), second.stem, now, now),
            )

        self._login()
        identified = self.client.get("/admin/recorder-review?status=identified")
        self.assertIn(b"Rename Speaker", identified.data)
        self.assertNotIn(
            b"Rename Speaker",
            self.client.get("/admin/recorder-review?status=needs_review").data,
        )
        response = self.client.post(
            "/admin/recorder-review/speakers/rename",
            data={
                "old_speaker_name": "Sister Renny",
                "new_speaker_name": "Renny Thomas",
                "sort": "newest",
                "limit": "100",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Renamed 2 recordings from Sister Renny to Renny Thomas.", response.data)
        renamed_first = first.with_name("May 4, 2025 - Renny Thomas's Testimony.mp3")
        renamed_second = second.with_name("May 3, 2026 - Renny Thomas's Testimony.mp3")
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertTrue(renamed_first.exists())
        self.assertTrue(renamed_second.exists())
        self.assertTrue(unrelated.exists())
        with sqlite3.connect(self.db_path) as connection:
            renamed_rows = connection.execute(
                """
                SELECT speaker_name, transcript_text
                FROM testimony_reviews
                WHERE speaker_name = 'Renny Thomas'
                ORDER BY service_date
                """
            ).fetchall()
            unrelated_row = connection.execute(
                "SELECT speaker_name FROM testimony_reviews WHERE source_path = ?",
                (str(unrelated),),
            ).fetchone()
            delivery = connection.execute(
                "SELECT speaker_name, recording_path, status FROM testimony_deliveries"
            ).fetchone()
            rule = connection.execute(
                "SELECT canonical_name, aliases_json FROM testimony_delivery_rules"
            ).fetchone()
            history_count = connection.execute(
                "SELECT COUNT(*) FROM recorder_review_history WHERE action = 'bulk_rename_speaker'"
            ).fetchone()[0]
        self.assertEqual(renamed_rows, [("Renny Thomas", "first transcript"), ("Renny Thomas", "second transcript")])
        self.assertEqual(unrelated_row[0], "Sister Rachel")
        self.assertEqual(delivery, ("Renny Thomas", str(renamed_second), "sent"))
        self.assertEqual(rule[0], "Renny Thomas")
        self.assertIn("Sister Renny", json.loads(rule[1]))
        self.assertEqual(history_count, 2)

    def test_bulk_testimony_suggestions_skip_named_message_files(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
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
                "SELECT status, recorder_agent_kind FROM testimony_reviews WHERE recording_id = ?",
                (_recording_id(named_message),),
            ).fetchone()

        self.assertIsNotNone(named_row)
        self.assertEqual(named_row, ("needs_review", "message"))

    def test_bulk_testimony_suggestions_mark_long_message_like_rows(self):
        testimony_source_root = self.root / "TestimonyReviewQueue"
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
                "SELECT status, suggestion_text, recorder_agent_kind FROM testimony_reviews WHERE recording_id = ?",
                (recording_id,),
            ).fetchone()
        self.assertEqual(row[0], "needs_review")
        self.assertIn("Shall we turn", row[1])
        self.assertEqual(row[2], "message")

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
            _extract_intro_speaker("Praise the Lord, I'm John Prabu and I want to thank the Lord.", []),
            "John Prabu",
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
        self.assertEqual(_extract_intro_speaker("Praise the Lord. I'm really, really blessed to testify today.", []), "")
        self.assertEqual(_extract_intro_speaker("I'm excited to see what God has in store.", []), "")
        self.assertEqual(_extract_intro_speaker("I am holy and grateful for this opportunity.", []), "")
        self.assertEqual(_valid_person_name_suggestion("Happening To Me", []), "")
        self.assertEqual(_valid_person_name_suggestion("Really", []), "")
        self.assertEqual(_valid_person_name_suggestion("Testimony", []), "")
        self.assertEqual(_valid_person_name_suggestion("Definitely One", []), "")
        self.assertEqual(_valid_person_name_suggestion("Really Awkward", []), "")
        self.assertEqual(_valid_person_name_suggestion("Something That", []), "")
        self.assertEqual(_valid_person_name_suggestion("Standing Here", []), "")
        self.assertEqual(_valid_person_name_suggestion("Tasting", []), "")
        self.assertEqual(_valid_person_name_suggestion("Holy", []), "")
        self.assertEqual(_valid_person_name_suggestion("Excited", []), "")
        self.assertEqual(_valid_person_name_suggestion("Faithful", []), "")
        self.assertEqual(_testimony_filename_speaker_suggestion(Path("March 15, 2026 - Testimony.mp3")), "")
        self.assertEqual(
            _testimony_filename_speaker_suggestion(Path("March 15, 2026 - Sister Shirley's Testimony.mp3")),
            "Sister Shirley",
        )

    def test_recorder_agent_reason_label_hides_internal_rule_versions(self):
        self.assertEqual(_recorder_agent_reason_label("recorder-review-v2: Applied recorder decision rules."), "")
        self.assertEqual(
            _recorder_agent_reason_label(
                "recorder-review-v2: Used metadata speaker evidence for testimony title."
            ),
            "Speaker confirmed from recording metadata.",
        )
        self.assertEqual(
            _recorder_agent_reason_label(
                "recorder-review-v2: Used explicit_name speaker evidence for testimony title."
            ),
            "Speaker name found in the transcript.",
        )
        self.assertEqual(
            _recorder_agent_reason_label("Classification confirmed in Recorder Review."),
            "Classification confirmed in Recorder Review.",
        )

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
                Path("/mnt/MainRecordings/Recordings/_IncomingRecorderIntake/TestimonyReviewQueue/REC00500.mp3"),
            )
        )
        self.assertTrue(
            _testimony_looks_like_message_recording(
                self.app,
                3548,
                "Oh, hallelujah. Those are wonderful words. Soon our Lord shall come in glory. "
                "Hallelujah. Are you ready, Brother Gerald? There is a pure river.",
                Path("/mnt/MainRecordings/Recordings/_IncomingRecorderIntake/TestimonyReviewQueue/REC00499.mp3"),
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

    def test_testimony_audio_streams_external_recorder_review_source(self):
        external_root = Path(self.tempdir.name) / "_IncomingRecorderIntake" / "TestimonyReviewQueue"
        external_root.mkdir(parents=True)
        raw_recording = external_root / "REC00999.mp3"
        raw_recording.write_bytes(b"external-review-audio")

        app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "external-review-test-secret",
                "NTC_RECORDINGS_DB_PATH": str(Path(self.tempdir.name) / "external-review-requests.db"),
                "NTC_RECORDINGS_LIBRARY_DIRS": f"message:{self.root},worship:{self.worship_root},testimony:{self.testimony_root}",
                "NTC_RECORDINGS_TESTIMONY_SOURCE_DIR": str(external_root),
                "NTC_RECORDINGS_TESTIMONY_LIBRARY_DIR": str(self.testimony_root),
                "NTC_RECORDINGS_TESTIMONY_REJECTED_DIR": str(self.rejected_root),
                "NTC_RECORDINGS_ADMIN_PASSWORD": "admin-password",
            }
        )
        client = app.test_client()
        client.post("/admin/login", data={"password": "admin-password"})

        audio = client.get(f"/admin/testimonies/audio/{_recording_id(raw_recording)}")

        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.data, b"external-review-audio")
        audio.close()

    def test_recorder_review_imports_unknown_manifest_rows_after_transcription_failure(self):
        intake_root = Path(self.tempdir.name) / "_IncomingRecorderIntake"
        review_root = intake_root / "TestimonyReviewQueue"
        staged_root = intake_root / "DN700R-primary"
        review_root.mkdir(parents=True)
        staged_root.mkdir(parents=True)
        raw_recording = staged_root / "07262026120053_DN-700R.mp3"
        raw_recording.write_bytes(b"unresolved-recorder-audio")
        manifest_path = Path(self.tempdir.name) / "manifest.sqlite3"
        with sqlite3.connect(manifest_path) as connection:
            connection.execute(
                """
                CREATE TABLE recorder_files (
                    staged_path TEXT NOT NULL,
                    duration_seconds REAL,
                    transcript_text TEXT NOT NULL DEFAULT '',
                    transcript_source TEXT NOT NULL DEFAULT '',
                    transcript_at TEXT NOT NULL DEFAULT '',
                    classification TEXT NOT NULL,
                    status TEXT NOT NULL,
                    matched_path TEXT NOT NULL DEFAULT '',
                    matched_kind TEXT NOT NULL DEFAULT '',
                    agent_decision_json TEXT NOT NULL DEFAULT '',
                    agent_review_reason TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO recorder_files (
                    staged_path,
                    duration_seconds,
                    classification,
                    status,
                    last_seen_at
                ) VALUES (?, ?, 'unknown', 'staged', ?)
                """,
                (str(raw_recording), 237.144, datetime.now(timezone.utc).isoformat()),
            )

        db_path = Path(self.tempdir.name) / "unknown-manifest-requests.db"
        app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "unknown-manifest-test-secret",
                "NTC_RECORDINGS_DB_PATH": str(db_path),
                "NTC_RECORDINGS_LIBRARY_DIRS": f"message:{self.root},worship:{self.worship_root},testimony:{self.testimony_root}",
                "NTC_RECORDINGS_TESTIMONY_SOURCE_DIR": str(review_root),
                "NTC_RECORDINGS_TESTIMONY_ALLOWED_DIRS": str(intake_root),
                "NTC_RECORDINGS_TESTIMONY_RECORDER_MANIFESTS": str(manifest_path),
                "NTC_RECORDINGS_TESTIMONY_LIBRARY_DIR": str(self.testimony_root),
                "NTC_RECORDINGS_TESTIMONY_REJECTED_DIR": str(self.rejected_root),
                "NTC_RECORDINGS_ADMIN_PASSWORD": "admin-password",
            }
        )
        client = app.test_client()
        client.post("/admin/login", data={"password": "admin-password"})

        response = client.get("/admin/recorder-review?status=all&sort=newest")

        self.assertEqual(response.status_code, 200)
        self.assertIn(raw_recording.name.encode(), response.data)
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "SELECT status FROM testimony_reviews WHERE source_path = ?",
                (str(raw_recording),),
            ).fetchone()
        self.assertEqual(row, ("needs_review",))

    def test_recorder_manifest_sync_is_reused_while_manifest_is_unchanged(self):
        app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "manifest-cache-test-secret",
                "NTC_RECORDINGS_DB_PATH": str(Path(self.tempdir.name) / "manifest-cache-requests.db"),
                "NTC_RECORDINGS_LIBRARY_DIRS": f"message:{self.root},worship:{self.worship_root},testimony:{self.testimony_root}",
                "NTC_RECORDINGS_TESTIMONY_RECORDER_MANIFESTS": str(Path(self.tempdir.name) / "missing-manifest.sqlite3"),
            }
        )
        app.testing = False

        with patch(
            "ntc_recordings_app._sync_testimony_recorder_manifest_reviews_uncached",
            return_value={"archived-path"},
        ) as sync:
            first = _sync_testimony_recorder_manifest_reviews(app)
            second = _sync_testimony_recorder_manifest_reviews(app)

        self.assertEqual(first, {"archived-path"})
        self.assertEqual(second, {"archived-path"})
        sync.assert_called_once()

    def test_recorder_review_items_are_reused_for_concurrent_refreshes(self):
        app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "review-cache-test-secret",
                "NTC_RECORDINGS_DB_PATH": str(Path(self.tempdir.name) / "review-cache-requests.db"),
                "NTC_RECORDINGS_LIBRARY_DIRS": f"message:{self.root},worship:{self.worship_root},testimony:{self.testimony_root}",
                "NTC_RECORDINGS_TESTIMONY_RECORDER_MANIFESTS": str(Path(self.tempdir.name) / "missing-review-manifest.sqlite3"),
            }
        )
        app.testing = False

        with patch(
            "ntc_recordings_app._testimony_review_items_uncached",
            return_value=[{"id": "recording-one"}],
        ) as build:
            first = _testimony_review_items(app, known_speakers=())
            second = _testimony_review_items(app, known_speakers=())

        self.assertEqual(first, [{"id": "recording-one"}])
        self.assertEqual(second, [{"id": "recording-one"}])
        self.assertIsNot(first, second)
        build.assert_called_once()

    def test_recorder_review_rejects_prompt_echo_manifest_decision(self):
        intake_root = Path(self.tempdir.name) / "_IncomingRecorderIntake"
        review_root = intake_root / "TestimonyReviewQueue"
        staged_root = intake_root / "DN700R-primary"
        review_root.mkdir(parents=True)
        staged_root.mkdir(parents=True)
        raw_recording = staged_root / "07262026115802_DN-700R.mp3"
        raw_recording.write_bytes(b"prompt-echo-recorder-audio")
        manifest_path = Path(self.tempdir.name) / "prompt-echo-manifest.sqlite3"
        prompt_echo = (
            "[start] introductions, and whether this sounds like a personal testimony "
            "or a preached message."
        )
        stale_decision = {
            "action": "review",
            "recording_kind": "testimony",
            "speaker": "",
            "title": "",
            "service_date": "2026-07-26",
        }
        with sqlite3.connect(manifest_path) as connection:
            connection.execute(
                """
                CREATE TABLE recorder_files (
                    staged_path TEXT NOT NULL,
                    duration_seconds REAL,
                    transcript_text TEXT NOT NULL DEFAULT '',
                    transcript_source TEXT NOT NULL DEFAULT '',
                    transcript_at TEXT NOT NULL DEFAULT '',
                    classification TEXT NOT NULL,
                    status TEXT NOT NULL,
                    matched_path TEXT NOT NULL DEFAULT '',
                    matched_kind TEXT NOT NULL DEFAULT '',
                    agent_decision_json TEXT NOT NULL DEFAULT '',
                    agent_review_reason TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO recorder_files (
                    staged_path,
                    duration_seconds,
                    transcript_text,
                    transcript_source,
                    transcript_at,
                    classification,
                    status,
                    agent_decision_json,
                    agent_review_reason,
                    last_seen_at
                ) VALUES (?, ?, ?, 'local_transcription', ?, 'testimony_candidate', 'staged', ?, ?, ?)
                """,
                (
                    str(raw_recording),
                    45.84,
                    prompt_echo,
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(stale_decision),
                    "No testimony speaker/name was confirmed.",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

        db_path = Path(self.tempdir.name) / "prompt-echo-manifest-requests.db"
        app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "prompt-echo-manifest-test-secret",
                "NTC_RECORDINGS_DB_PATH": str(db_path),
                "NTC_RECORDINGS_LIBRARY_DIRS": f"message:{self.root},worship:{self.worship_root},testimony:{self.testimony_root}",
                "NTC_RECORDINGS_TESTIMONY_SOURCE_DIR": str(review_root),
                "NTC_RECORDINGS_TESTIMONY_ALLOWED_DIRS": str(intake_root),
                "NTC_RECORDINGS_TESTIMONY_RECORDER_MANIFESTS": str(manifest_path),
                "NTC_RECORDINGS_TESTIMONY_LIBRARY_DIR": str(self.testimony_root),
                "NTC_RECORDINGS_TESTIMONY_REJECTED_DIR": str(self.rejected_root),
                "NTC_RECORDINGS_ADMIN_PASSWORD": "admin-password",
            }
        )
        client = app.test_client()
        client.post("/admin/login", data={"password": "admin-password"})

        response = client.get("/admin/recorder-review?status=needs_review")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"whether this sounds like", response.data)
        self.assertNotIn(b">Testimony</strong>", response.data)
        self.assertIn(b"Automatic transcript was rejected", response.data)
        self.assertNotIn(b">Retry Analysis</button>", response.data)
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT recording_id, transcript_text, transcript_source, transcript_error,
                       recorder_agent_kind, recorder_agent_action, recorder_agent_reason
                FROM testimony_reviews
                WHERE source_path = ?
                """,
                (str(raw_recording),),
            ).fetchone()
        self.assertEqual(row["transcript_text"], "")
        self.assertEqual(row["transcript_source"], "")
        self.assertIn("transcription instructions", row["transcript_error"])
        self.assertEqual(row["recorder_agent_kind"], "unknown")
        self.assertEqual(row["recorder_agent_action"], "review")
        self.assertIn("transcription instructions", row["recorder_agent_reason"])

        retry_error = (
            "Transcription request failed: 429 Client Error: Too Many Requests for url: "
            "http://100.109.220.95:8766/transcription?prompt=internal"
        )
        _save_testimony_transcript(
            app,
            row["recording_id"],
            "",
            "",
            retry_error,
        )
        refreshed = client.get("/admin/recorder-review?status=needs_review")
        self.assertIn(b"Automatic transcription is busy", refreshed.data)
        self.assertNotIn(b"Automatic transcript was rejected", refreshed.data)
        self.assertNotIn(b"whether this sounds like", refreshed.data)
        self.assertNotIn(b"429 Client Error", refreshed.data)
        self.assertNotIn(b"100.109.220.95", refreshed.data)
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            retried_row = connection.execute(
                """
                SELECT transcript_text, transcript_error
                FROM testimony_reviews
                WHERE recording_id = ?
                """,
                (row["recording_id"],),
            ).fetchone()
        self.assertEqual(retried_row["transcript_text"], "")
        self.assertEqual(
            retried_row["transcript_error"],
            "Automatic transcription is busy. This recording remains queued for retry.",
        )

    def test_cached_transcript_is_reclassified_by_recorder_agent(self):
        review_root = self.root / "TestimonyReviewQueue"
        review_root.mkdir()
        recording = review_root / "07262026115900_DN-700R.mp3"
        recording.write_bytes(b"cached-transcript-audio")
        recording_id = _recording_id(recording)
        transcript = (
            "Praise the Lord. I want to thank and praise God for bringing me through "
            "this trial and for answering our family's prayers."
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO testimony_reviews (
                    recording_id,
                    source_path,
                    status,
                    service_date,
                    transcript_text,
                    transcript_source,
                    recorder_agent_kind,
                    recorder_agent_action,
                    recorder_agent_reason,
                    recorder_agent_updated_at,
                    updated_at
                )
                VALUES (?, ?, 'needs_review', '2026-07-26', ?, 'chunked-transcript-v1',
                        'message', 'review', 'Legacy manifest decision.', ?, ?)
                """,
                (
                    recording_id,
                    str(recording),
                    transcript,
                    "2026-07-26T18:00:00+00:00",
                    "2026-07-26T18:00:00+00:00",
                ),
            )

        self.app.config["NTC_RECORDINGS_AGENT_URL"] = "http://agent.test"
        agent_response = Mock()
        agent_response.raise_for_status.return_value = None
        agent_response.json.return_value = {
            "ok": True,
            "recording_kind": "testimony",
            "action": "review",
            "reasons": ["Personal testimony evidence was found."],
            "speaker": "",
            "decision_version": "recording-decision-v3",
            "confidence": 0.91,
            "evidence": {
                "traits": ["personal_narrative", "explicit_testimony", "preachimony"],
                "historical_references": [
                    {
                        "recording_id": "known-testimony",
                        "recording_kind": "testimony",
                        "similarity": 0.73,
                    }
                ],
            },
        }
        with patch("ntc_recordings_app.requests.post", return_value=agent_response) as post:
            _run_testimony_transcript_job(
                self.app,
                statuses={"needs_review"},
                recording_ids={recording_id},
            )

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT recorder_agent_kind, recorder_agent_action,
                       recorder_agent_reason, transcript_text,
                       recorder_agent_version, recorder_agent_confidence,
                       recorder_agent_traits_json, recorder_agent_evidence_json
                FROM testimony_reviews
                WHERE recording_id = ?
                """,
                (recording_id,),
            ).fetchone()
        self.assertEqual(row["recorder_agent_kind"], "testimony")
        self.assertEqual(row["recorder_agent_action"], "review")
        self.assertIn("recorder-review-v4", row["recorder_agent_reason"])
        self.assertIn("Personal testimony evidence", row["recorder_agent_reason"])
        self.assertEqual(row["transcript_text"], transcript)
        self.assertEqual(row["recorder_agent_version"], "recording-decision-v3")
        self.assertAlmostEqual(row["recorder_agent_confidence"], 0.91)
        self.assertEqual(
            json.loads(row["recorder_agent_traits_json"]),
            ["personal_narrative", "explicit_testimony", "preachimony"],
        )
        self.assertEqual(
            json.loads(row["recorder_agent_evidence_json"])["historical_references"][0]["recording_id"],
            "known-testimony",
        )
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["recording_id"], recording_id)
        self.assertEqual(payload["classification"], "message_candidate")
        self.assertEqual(payload["transcript_text"], transcript)
        self.assertEqual(payload["recorder_lane"], "ntc-dn700r")
        self.assertEqual(payload["allowed_recording_kinds"], ["testimony", "message", "worship", "noise"])
        self.assertEqual(payload["transcript_windows"], [])
        self._login()
        review = self.client.get("/admin/recorder-review?status=needs_review")
        self.assertIn(b"Personal testimony with extended doctrinal exhortation.", review.data)
        self.assertIn(b"Compared with 1 similar reviewed recording.", review.data)
        self.assertIn(b"91% confidence", review.data)

    def test_manual_recording_type_survives_forced_transcript_reanalysis(self):
        review_root = self.root / "TestimonyReviewQueue"
        review_root.mkdir()
        recording = review_root / "REC00248.mp3"
        recording.write_bytes(b"long-testimony-audio")
        recording_id = _recording_id(recording)
        transcript = (
            "Praise the Lord. Hallelujah. Before I enter the testimony, "
            "I want to thank God for everything he has done in my life."
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO testimony_reviews (
                    recording_id,
                    source_path,
                    status,
                    service_date,
                    transcript_text,
                    transcript_source,
                    recorder_agent_kind,
                    recorder_agent_action,
                    recorder_agent_reason,
                    recorder_agent_updated_at,
                    updated_at
                )
                VALUES (?, ?, 'needs_review', '2025-08-31', ?, 'transcript_excerpt',
                        'testimony', 'review', 'Classification confirmed in Recorder Review.', ?, ?)
                """,
                (
                    recording_id,
                    str(recording),
                    transcript,
                    "2026-07-29T13:00:00+00:00",
                    "2026-07-29T13:00:00+00:00",
                ),
            )

        self.app.config["NTC_RECORDINGS_AGENT_URL"] = "http://agent.test"
        agent_response = Mock()
        agent_response.raise_for_status.return_value = None
        agent_response.json.return_value = {
            "ok": True,
            "recording_kind": "worship",
            "action": "review",
            "reasons": ["Praise language was found."],
            "speaker": "",
        }
        with patch("ntc_recordings_app.requests.post", return_value=agent_response):
            _run_testimony_transcript_job(
                self.app,
                statuses={"needs_review"},
                recording_ids={recording_id},
            )

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT recorder_agent_kind, recorder_agent_action, recorder_agent_reason
                FROM testimony_reviews
                WHERE recording_id = ?
                """,
                (recording_id,),
            ).fetchone()
        self.assertEqual(row["recorder_agent_kind"], "testimony")
        self.assertEqual(row["recorder_agent_action"], "review")
        self.assertEqual(
            row["recorder_agent_reason"],
            "Classification confirmed in Recorder Review.",
        )

    def test_recorder_transcript_windows_preserve_timed_and_segment_context(self):
        row = {
            "recorder_segments_json": json.dumps(
                [
                    {"start_seconds": 0, "end_seconds": 60, "snippet": "Praise and worship."},
                    {"start_seconds": 60, "end_seconds": 240, "snippet": "My name is Rachel. I want to testify."},
                ]
            )
        }
        timed = _recorder_transcript_windows(
            row,
            "[start] Praise and worship.\n\n[+60s] My name is Rachel. I want to testify.",
        )
        self.assertEqual(
            timed,
            [
                {"start_seconds": 0.0, "end_seconds": 60.0, "text": "Praise and worship."},
                {"start_seconds": 60.0, "end_seconds": None, "text": "My name is Rachel. I want to testify."},
            ],
        )

        segmented = _recorder_transcript_windows(row, "Flattened transcript")
        self.assertEqual(segmented[0]["start_seconds"], 0.0)
        self.assertEqual(segmented[0]["end_seconds"], 60.0)
        self.assertEqual(segmented[1]["text"], "My name is Rachel. I want to testify.")

    def test_newer_recorder_review_decision_survives_stale_manifest_sync(self):
        intake_root = Path(self.tempdir.name) / "_IncomingRecorderIntake"
        review_root = intake_root / "TestimonyReviewQueue"
        staged_root = intake_root / "DN700R-primary"
        review_root.mkdir(parents=True)
        staged_root.mkdir(parents=True)
        raw_recording = staged_root / "07262026115900_DN-700R.mp3"
        raw_recording.write_bytes(b"stale-manifest-audio")
        manifest_path = Path(self.tempdir.name) / "stale-decision-manifest.sqlite3"
        stale_decision = {
            "action": "review",
            "recording_kind": "message",
            "reason": "Legacy duration-based decision.",
        }
        with sqlite3.connect(manifest_path) as connection:
            connection.execute(
                """
                CREATE TABLE recorder_files (
                    staged_path TEXT NOT NULL,
                    duration_seconds REAL,
                    transcript_text TEXT NOT NULL DEFAULT '',
                    transcript_source TEXT NOT NULL DEFAULT '',
                    transcript_at TEXT NOT NULL DEFAULT '',
                    classification TEXT NOT NULL,
                    status TEXT NOT NULL,
                    matched_path TEXT NOT NULL DEFAULT '',
                    matched_kind TEXT NOT NULL DEFAULT '',
                    agent_decision_json TEXT NOT NULL DEFAULT '',
                    agent_decision_at TEXT NOT NULL DEFAULT '',
                    agent_review_reason TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO recorder_files (
                    staged_path,
                    duration_seconds,
                    transcript_text,
                    transcript_source,
                    transcript_at,
                    classification,
                    status,
                    agent_decision_json,
                    agent_decision_at,
                    agent_review_reason,
                    last_seen_at
                )
                VALUES (?, 98.0, ?, 'local_transcription', ?, 'message_candidate',
                        'staged', ?, ?, 'Legacy duration-based decision.', ?)
                """,
                (
                    str(raw_recording),
                    "I want to thank and praise God for answering my prayer.",
                    "2026-07-26T18:00:00+00:00",
                    json.dumps(stale_decision),
                    "2026-07-26T18:00:00+00:00",
                    "2026-07-26T18:00:00+00:00",
                ),
            )

        db_path = Path(self.tempdir.name) / "stale-decision-requests.db"
        app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "stale-decision-test-secret",
                "NTC_RECORDINGS_DB_PATH": str(db_path),
                "NTC_RECORDINGS_LIBRARY_DIRS": f"message:{self.root},worship:{self.worship_root},testimony:{self.testimony_root}",
                "NTC_RECORDINGS_TESTIMONY_SOURCE_DIR": str(review_root),
                "NTC_RECORDINGS_TESTIMONY_ALLOWED_DIRS": str(intake_root),
                "NTC_RECORDINGS_TESTIMONY_RECORDER_MANIFESTS": str(manifest_path),
                "NTC_RECORDINGS_TESTIMONY_LIBRARY_DIR": str(self.testimony_root),
                "NTC_RECORDINGS_TESTIMONY_REJECTED_DIR": str(self.rejected_root),
            }
        )
        _sync_testimony_recorder_manifest_reviews(app)
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """
                UPDATE testimony_reviews
                SET recorder_agent_kind = 'testimony',
                    recorder_agent_action = 'review',
                    recorder_agent_reason = 'recorder-review-v2: Personal testimony evidence.',
                    recorder_agent_updated_at = '2026-07-27T18:00:00+00:00',
                    updated_at = '2026-07-27T18:00:00+00:00'
                WHERE source_path = ?
                """,
                (str(raw_recording),),
            )

        _sync_testimony_recorder_manifest_reviews(app)

        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT recorder_agent_kind, recorder_agent_action,
                       recorder_agent_reason, recorder_agent_updated_at
                FROM testimony_reviews
                WHERE source_path = ?
                """,
                (str(raw_recording),),
            ).fetchone()
        self.assertEqual(row["recorder_agent_kind"], "testimony")
        self.assertEqual(row["recorder_agent_action"], "review")
        self.assertIn("recorder-review-v2", row["recorder_agent_reason"])
        self.assertEqual(row["recorder_agent_updated_at"], "2026-07-27T18:00:00+00:00")

    def test_recorder_review_reconciles_promoted_testimony_as_identified(self):
        intake_root = Path(self.tempdir.name) / "_IncomingRecorderIntake"
        review_root = intake_root / "TestimonyReviewQueue"
        staged_root = intake_root / "DN700R-primary"
        review_root.mkdir(parents=True)
        staged_root.mkdir(parents=True)
        staged_recording = staged_root / "07262026114801_DN-700R.mp3"
        staged_recording.write_bytes(b"raw-john-testimony")
        final_recording = self.testimony_root / "2026" / "Sunday Testimonies" / "July 26, 2026 - John's Testimony.mp3"
        final_recording.parent.mkdir(parents=True)
        final_recording.write_bytes(b"final-john-testimony")
        manifest_path = Path(self.tempdir.name) / "identified-manifest.sqlite3"
        decision = {
            "action": "promote",
            "recording_kind": "testimony",
            "speaker": "John",
            "title": "John's Testimony",
            "service_date": "2026-07-26",
        }
        with sqlite3.connect(manifest_path) as connection:
            connection.execute(
                """
                CREATE TABLE recorder_files (
                    staged_path TEXT NOT NULL,
                    matched_path TEXT NOT NULL DEFAULT '',
                    matched_kind TEXT NOT NULL DEFAULT '',
                    duration_seconds REAL,
                    transcript_text TEXT NOT NULL DEFAULT '',
                    transcript_source TEXT NOT NULL DEFAULT '',
                    transcript_at TEXT NOT NULL DEFAULT '',
                    classification TEXT NOT NULL,
                    status TEXT NOT NULL,
                    agent_decision_json TEXT NOT NULL DEFAULT '',
                    agent_review_reason TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO recorder_files (
                    staged_path,
                    matched_path,
                    matched_kind,
                    duration_seconds,
                    transcript_text,
                    transcript_source,
                    transcript_at,
                    classification,
                    status,
                    agent_decision_json,
                    last_seen_at
                ) VALUES (?, ?, 'testimony', ?, ?, 'local_transcription', ?, 'testimony_candidate', 'already_archived', ?, ?)
                """,
                (
                    str(staged_recording),
                    str(final_recording),
                    29.88,
                    "Praise the Lord. My name is John.",
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(decision),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

        db_path = Path(self.tempdir.name) / "identified-manifest-requests.db"
        app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "identified-manifest-test-secret",
                "NTC_RECORDINGS_DB_PATH": str(db_path),
                "NTC_RECORDINGS_LIBRARY_DIRS": f"message:{self.root},worship:{self.worship_root},testimony:{self.testimony_root}",
                "NTC_RECORDINGS_TESTIMONY_SOURCE_DIR": str(review_root),
                "NTC_RECORDINGS_TESTIMONY_ALLOWED_DIRS": str(intake_root),
                "NTC_RECORDINGS_TESTIMONY_RECORDER_MANIFESTS": str(manifest_path),
                "NTC_RECORDINGS_TESTIMONY_LIBRARY_DIR": str(self.testimony_root),
                "NTC_RECORDINGS_TESTIMONY_REJECTED_DIR": str(self.rejected_root),
                "NTC_RECORDINGS_ADMIN_PASSWORD": "admin-password",
            }
        )
        staged_recording_id = _recording_id(staged_recording)
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """
                INSERT INTO testimony_reviews (
                    recording_id,
                    source_path,
                    status,
                    service_date,
                    updated_at
                ) VALUES (?, ?, 'needs_review', '2026-07-26', ?)
                """,
                (
                    staged_recording_id,
                    str(staged_recording),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        client = app.test_client()
        client.post("/admin/login", data={"password": "admin-password"})

        response = client.get("/admin/recorder-review?status=identified&sort=newest")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"July 26, 2026 - John&#39;s Testimony.mp3", response.data)
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                """
                SELECT status, source_path, speaker_name, testimony_title
                FROM testimony_reviews
                WHERE recording_id = ?
                """,
                (staged_recording_id,),
            ).fetchone()
        self.assertEqual(
            row,
            ("identified", str(final_recording), "John", "John's Testimony"),
        )

    def test_recorder_review_does_not_restore_completed_staged_recording(self):
        intake_root = Path(self.tempdir.name) / "_IncomingRecorderIntake"
        review_root = intake_root / "TestimonyReviewQueue"
        staged_root = intake_root / "DN700R-primary"
        review_root.mkdir(parents=True)
        staged_root.mkdir(parents=True)
        staged_recording = staged_root / "07262026114845_DN-700R.mp3"
        staged_recording.write_bytes(b"raw-edmond-testimony")
        final_recording = self.testimony_root / "2026" / "Sunday Testimonies" / "July 26, 2026 - Edmond Spencer's Testimony.mp3"
        final_recording.parent.mkdir(parents=True)
        final_recording.write_bytes(b"final-edmond-testimony")
        manifest_path = Path(self.tempdir.name) / "stale-staged-manifest.sqlite3"
        with sqlite3.connect(manifest_path) as connection:
            connection.execute(
                """
                CREATE TABLE recorder_files (
                    staged_path TEXT NOT NULL,
                    matched_path TEXT NOT NULL DEFAULT '',
                    matched_kind TEXT NOT NULL DEFAULT '',
                    duration_seconds REAL,
                    transcript_text TEXT NOT NULL DEFAULT '',
                    transcript_source TEXT NOT NULL DEFAULT '',
                    transcript_at TEXT NOT NULL DEFAULT '',
                    classification TEXT NOT NULL,
                    status TEXT NOT NULL,
                    agent_decision_json TEXT NOT NULL DEFAULT '',
                    agent_decision_at TEXT NOT NULL DEFAULT '',
                    agent_review_reason TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO recorder_files (
                    staged_path,
                    duration_seconds,
                    classification,
                    status,
                    last_seen_at
                ) VALUES (?, 26.784, 'testimony_candidate', 'staged', ?)
                """,
                (str(staged_recording), datetime.now(timezone.utc).isoformat()),
            )

        db_path = Path(self.tempdir.name) / "stale-staged-requests.db"
        app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "stale-staged-test-secret",
                "NTC_RECORDINGS_DB_PATH": str(db_path),
                "NTC_RECORDINGS_LIBRARY_DIRS": f"message:{self.root},worship:{self.worship_root},testimony:{self.testimony_root}",
                "NTC_RECORDINGS_TESTIMONY_SOURCE_DIR": str(review_root),
                "NTC_RECORDINGS_TESTIMONY_ALLOWED_DIRS": str(intake_root),
                "NTC_RECORDINGS_TESTIMONY_RECORDER_MANIFESTS": str(manifest_path),
                "NTC_RECORDINGS_TESTIMONY_LIBRARY_DIR": str(self.testimony_root),
                "NTC_RECORDINGS_TESTIMONY_REJECTED_DIR": str(self.rejected_root),
            }
        )
        staged_recording_id = _recording_id(staged_recording)
        final_recording_id = _recording_id(final_recording)
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """
                INSERT INTO testimony_reviews (
                    recording_id,
                    source_path,
                    status,
                    service_date,
                    duration_seconds,
                    updated_at
                ) VALUES (?, ?, 'needs_review', '2026-07-26', 26.784, ?)
                """,
                (staged_recording_id, str(staged_recording), now),
            )
            connection.execute(
                """
                INSERT INTO testimony_reviews (
                    recording_id,
                    source_path,
                    status,
                    service_date,
                    speaker_name,
                    testimony_title,
                    proposed_path,
                    duration_seconds,
                    updated_at
                ) VALUES (?, ?, 'identified', '2026-07-26', 'Edmond Spencer',
                          'Edmond Spencer''s Testimony', ?, 26.784, ?)
                """,
                (final_recording_id, str(final_recording), str(final_recording), now),
            )
            connection.execute(
                """
                INSERT INTO recorder_review_history (
                    recording_id,
                    previous_recording_id,
                    action,
                    previous_status,
                    new_status,
                    service_date,
                    speaker_name,
                    recording_kind,
                    source_path,
                    target_path,
                    created_at
                ) VALUES (?, ?, 'save_review', 'needs_review', 'identified',
                          '2026-07-26', 'Edmond Spencer', 'testimony', ?, ?, ?)
                """,
                (
                    final_recording_id,
                    staged_recording_id,
                    str(staged_recording),
                    str(final_recording),
                    now,
                ),
            )

        ignored_paths = _sync_testimony_recorder_manifest_reviews(app)

        self.assertIn(str(staged_recording), ignored_paths)
        with sqlite3.connect(db_path) as connection:
            stale_row = connection.execute(
                "SELECT 1 FROM testimony_reviews WHERE recording_id = ?",
                (staged_recording_id,),
            ).fetchone()
            final_row = connection.execute(
                "SELECT status, speaker_name FROM testimony_reviews WHERE recording_id = ?",
                (final_recording_id,),
            ).fetchone()
        self.assertIsNone(stale_row)
        self.assertEqual(final_row, ("identified", "Edmond Spencer"))

    def test_recorder_review_hides_promoted_message_and_removes_stale_review(self):
        intake_root = Path(self.tempdir.name) / "_IncomingRecorderIntake"
        review_root = intake_root / "TestimonyReviewQueue"
        staged_root = intake_root / "DN700R-primary"
        review_root.mkdir(parents=True)
        staged_root.mkdir(parents=True)
        staged_recording = staged_root / "07262026121125_DN-700R.mp3"
        staged_recording.write_bytes(b"raw-message")
        final_recording = self.root / "2026" / "Sunday Messages" / "July" / "July 26, 2026 - The Victories of the Lord - Bro Blessen.mp3"
        final_recording.parent.mkdir(parents=True)
        final_recording.write_bytes(b"final-message")
        manifest_path = Path(self.tempdir.name) / "promoted-message-manifest.sqlite3"
        decision = {
            "action": "promote",
            "recording_kind": "message",
            "speaker": "Bro Blessen",
            "title": "The Victories of the Lord",
            "service_date": "2026-07-26",
        }
        with sqlite3.connect(manifest_path) as connection:
            connection.execute(
                """
                CREATE TABLE recorder_files (
                    staged_path TEXT NOT NULL,
                    matched_path TEXT NOT NULL DEFAULT '',
                    matched_kind TEXT NOT NULL DEFAULT '',
                    duration_seconds REAL,
                    transcript_text TEXT NOT NULL DEFAULT '',
                    transcript_source TEXT NOT NULL DEFAULT '',
                    transcript_at TEXT NOT NULL DEFAULT '',
                    classification TEXT NOT NULL,
                    status TEXT NOT NULL,
                    agent_decision_json TEXT NOT NULL DEFAULT '',
                    agent_review_reason TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO recorder_files (
                    staged_path,
                    matched_path,
                    matched_kind,
                    duration_seconds,
                    transcript_text,
                    transcript_source,
                    transcript_at,
                    classification,
                    status,
                    agent_decision_json,
                    last_seen_at
                ) VALUES (?, ?, 'message', ?, ?, 'local_transcription', ?, 'message_candidate', 'already_archived', ?, ?)
                """,
                (
                    str(staged_recording),
                    str(final_recording),
                    3792.0,
                    "The battlefield, don't give room to the enemy.",
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(decision),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

        db_path = Path(self.tempdir.name) / "promoted-message-requests.db"
        app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "promoted-message-test-secret",
                "NTC_RECORDINGS_DB_PATH": str(db_path),
                "NTC_RECORDINGS_LIBRARY_DIRS": f"message:{self.root},worship:{self.worship_root},testimony:{self.testimony_root}",
                "NTC_RECORDINGS_TESTIMONY_SOURCE_DIR": str(staged_root),
                "NTC_RECORDINGS_TESTIMONY_ALLOWED_DIRS": str(intake_root),
                "NTC_RECORDINGS_TESTIMONY_RECORDER_MANIFESTS": str(manifest_path),
                "NTC_RECORDINGS_TESTIMONY_LIBRARY_DIR": str(self.testimony_root),
                "NTC_RECORDINGS_TESTIMONY_REJECTED_DIR": str(self.rejected_root),
                "NTC_RECORDINGS_ADMIN_PASSWORD": "admin-password",
            }
        )
        staged_recording_id = _recording_id(staged_recording)
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                """
                INSERT INTO testimony_reviews (
                    recording_id,
                    source_path,
                    status,
                    service_date,
                    recorder_agent_kind,
                    updated_at
                ) VALUES (?, ?, 'needs_review', '2026-07-26', 'worship', ?)
                """,
                (
                    staged_recording_id,
                    str(staged_recording),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        client = app.test_client()
        client.post("/admin/login", data={"password": "admin-password"})

        response = client.get("/admin/recorder-review?status=all&sort=newest")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(staged_recording.name.encode(), response.data)
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "SELECT status FROM testimony_reviews WHERE recording_id = ?",
                (staged_recording_id,),
            ).fetchone()
        self.assertIsNone(row)

    def test_metadata_dates_use_local_church_day(self):
        raw_recording = self.root / "REC00494.mp3"
        raw_recording.write_bytes(b"evening-service-audio")
        service_timestamp = datetime(2026, 6, 11, 0, 8, 32, tzinfo=timezone.utc).timestamp()
        os.utime(raw_recording, (service_timestamp, service_timestamp))

        self.assertEqual(_date_from_file_metadata(raw_recording.stat()), "2026-06-10")

    def test_classifier_evidence_is_human_readable(self):
        raw = r"testimony language: \btestif(?:y|ies|ied|ying)\b"

        label = _humanize_classifier_evidence(raw)

        self.assertEqual(label, "Testimony language detected")
        self.assertNotIn(r"\b", label)
        self.assertNotIn("(?:", label)

    def test_classifier_evidence_combines_plain_language_signals(self):
        raw = r"worship language: \bpraise the lord\b"

        self.assertEqual(
            _humanize_classifier_evidence(raw),
            "Praise or worship language detected",
        )

        self.assertEqual(
            _humanize_classifier_evidence(r"personal experience: \bgod (has|had|did|was)\b"),
            "Personal experience language detected",
        )

    def test_recorder_review_defaults_to_newest_service_date(self):
        self._login()

        response = self.client.get("/admin/recorder-review")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<option value="newest" selected>Newest first</option>', response.data)
        items = [
            {"service_date": "2025-12-31", "modified_at": "2026-07-28T12:00:00+00:00", "title": "Older"},
            {"service_date": "2026-07-26", "modified_at": "2026-07-26T12:00:00+00:00", "title": "Newer"},
        ]
        _sort_testimony_items(items, "newest")
        self.assertEqual([item["title"] for item in items], ["Newer", "Older"])

    def test_completed_review_gets_current_analysis_marker_when_agent_is_unavailable(self):
        source_root = self.root / "TestimonyReviewQueue"
        source_root.mkdir()
        recording = source_root / "REC00991.mp3"
        recording.write_bytes(b"completed-testimony")
        recording_id = _recording_id(recording)
        _save_testimony_review(
            self.app,
            recording_id=recording_id,
            source_path=str(recording),
            status="identified",
            service_date="2026-07-26",
            speaker_name="Sister Test",
            testimony_title="Sister Test's Testimony",
            notes="",
            proposed_path="",
            duration_seconds=75,
            suggested_speaker="Sister Test",
            suggestion_source="history",
            suggestion_text="Confirmed previously.",
        )
        _save_testimony_transcript(
            self.app,
            recording_id,
            transcript_text="Praise the Lord. I want to thank God.",
            transcript_source="chunked-transcript-v1",
            transcript_error="",
        )

        with patch("ntc_recordings_app._classify_recorder_review_transcript", return_value=None):
            _run_testimony_transcript_job(
                self.app,
                limit=1,
                statuses={"identified"},
                recording_ids={recording_id},
            )

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM testimony_reviews WHERE recording_id = ?",
                (recording_id,),
            ).fetchone()
        self.assertIn("recorder-review-v4", row["recorder_agent_reason"])
        self.assertEqual(_automatic_review_analysis_ids([dict(row)]), set())

    def test_recorder_review_get_does_not_start_analysis_for_visible_rows(self):
        self.app.config["NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_URL"] = "http://transcription.example.test"
        self._login()

        with patch("ntc_recordings_app._start_testimony_transcript_job") as start_job:
            response = self.client.get("/admin/recorder-review?status=identified&limit=1")

        self.assertEqual(response.status_code, 200)
        start_job.assert_not_called()

    def test_background_analysis_selects_missing_rows_across_review_statuses(self):
        items = [
            {
                "id": "needs-review",
                "status": "needs_review",
                "transcript_text": "",
                "transcript_error": "",
                "recorder_agent_reason": "",
            },
            {
                "id": "identified",
                "status": "identified",
                "transcript_text": "",
                "transcript_error": "",
                "recorder_agent_reason": "",
            },
            {
                "id": "discarded",
                "status": "not_testimony",
                "transcript_text": "",
                "transcript_error": "",
                "recorder_agent_reason": "",
            },
        ]
        with (
            patch("ntc_recordings_app._testimony_known_speakers", return_value=[]),
            patch("ntc_recordings_app._testimony_review_items", return_value=items),
        ):
            recording_ids = _background_review_analysis_ids(self.app, limit=20)

        self.assertEqual(recording_ids, {"needs-review", "identified"})

    def test_background_analysis_reprocesses_legacy_transcripts_once(self):
        base_item = {
            "id": "legacy-transcript",
            "status": "identified",
            "transcript_text": "Praise the Lord. This transcript was stored by the old single-block pass.",
            "transcript_error": "",
            "recorder_agent_reason": "recorder-review-v4: Automatic transcript classification completed.",
        }

        self.assertEqual(
            _automatic_review_analysis_ids(
                [{**base_item, "transcript_source": "transcript_excerpt"}]
            ),
            {"legacy-transcript"},
        )
        self.assertEqual(
            _automatic_review_analysis_ids(
                [{**base_item, "transcript_source": "chunked-transcript-v1"}]
            ),
            set(),
        )

    def test_chunked_transcript_preserves_start_and_covers_full_duration(self):
        recording = self.root / "REC00430.mp3"
        recording.write_bytes(b"audio")
        self.app.config.update(
            NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_URL="http://transcription.example.test",
            NTC_RECORDINGS_TESTIMONY_TRANSCRIPT_CHUNK_SECONDS=60,
            NTC_RECORDINGS_TESTIMONY_TRANSCRIPT_MAX_SECONDS=180,
        )

        def prepare_chunk(command, **_kwargs):
            Path(command[-1]).write_bytes(b"wav")
            return Mock(returncode=0, stderr="", stdout="")

        responses = []
        for text in (
            "Can I just personally confess that I am really blessed to be here.",
            "Today I had a simple desire to attend a few songs of worship.",
            (
                "I am here to request prayers for my auntie. "
                "Keep names exactly as spoken. Do not summarize or add instructions."
            ),
        ):
            response = Mock()
            response.json.return_value = {"text": text}
            response.text = ""
            responses.append(response)

        with (
            patch("ntc_recordings_app._probe_audio_duration", return_value=130),
            patch("ntc_recordings_app.subprocess.run", side_effect=prepare_chunk) as ffmpeg,
            patch(
                "ntc_recordings_app._post_transcription_audio",
                side_effect=responses,
            ) as transcribe,
        ):
            transcript, error = _transcribe_testimony_review_excerpt(
                self.app,
                recording,
            )

        self.assertEqual(error, "")
        self.assertIn("[start] Can I just personally confess", transcript)
        self.assertIn("[+60s] Today I had a simple desire", transcript)
        self.assertIn("[+120s] I am here to request prayers", transcript)
        self.assertNotIn("Keep names exactly as spoken", transcript)
        self.assertNotIn("Do not summarize", transcript)
        self.assertEqual(transcribe.call_count, 3)
        commands = [call.args[0] for call in ffmpeg.call_args_list]
        self.assertEqual(
            [command[command.index("-ss") + 1] for command in commands],
            ["0", "60", "120"],
        )
        self.assertTrue(
            all("adelay=500:all=1" in command for command in commands)
        )

    def test_chunked_transcript_skips_silent_chunks_and_keeps_later_speech(self):
        recording = self.root / "REC00431.mp3"
        recording.write_bytes(b"audio")
        self.app.config.update(
            NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_URL="http://transcription.example.test",
            NTC_RECORDINGS_TESTIMONY_TRANSCRIPT_CHUNK_SECONDS=60,
            NTC_RECORDINGS_TESTIMONY_TRANSCRIPT_MAX_SECONDS=180,
        )

        def prepare_chunk(command, **_kwargs):
            Path(command[-1]).write_bytes(b"wav")
            return Mock(returncode=0, stderr="", stdout="")

        responses = []
        for text in (
            "",
            "Praise the Lord. I want to thank God for what He has done in my life.",
            "",
        ):
            response = Mock()
            response.json.return_value = {"text": text}
            response.text = ""
            responses.append(response)

        with (
            patch("ntc_recordings_app._probe_audio_duration", return_value=130),
            patch("ntc_recordings_app.subprocess.run", side_effect=prepare_chunk),
            patch(
                "ntc_recordings_app._post_transcription_audio",
                side_effect=responses,
            ) as transcribe,
        ):
            transcript, error = _transcribe_testimony_review_excerpt(
                self.app,
                recording,
            )

        self.assertEqual(error, "")
        self.assertEqual(
            transcript,
            "[+60s] Praise the Lord. I want to thank God for what He has done in my life.",
        )
        self.assertEqual(transcribe.call_count, 3)

    def test_chunked_transcript_reports_empty_only_when_every_chunk_is_empty(self):
        recording = self.root / "REC00432.mp3"
        recording.write_bytes(b"audio")
        self.app.config.update(
            NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_URL="http://transcription.example.test",
            NTC_RECORDINGS_TESTIMONY_TRANSCRIPT_CHUNK_SECONDS=60,
            NTC_RECORDINGS_TESTIMONY_TRANSCRIPT_MAX_SECONDS=120,
        )

        def prepare_chunk(command, **_kwargs):
            Path(command[-1]).write_bytes(b"wav")
            return Mock(returncode=0, stderr="", stdout="")

        responses = []
        for _ in range(2):
            response = Mock()
            response.json.return_value = {"text": ""}
            response.text = ""
            responses.append(response)

        with (
            patch("ntc_recordings_app._probe_audio_duration", return_value=120),
            patch("ntc_recordings_app.subprocess.run", side_effect=prepare_chunk),
            patch(
                "ntc_recordings_app._post_transcription_audio",
                side_effect=responses,
            ) as transcribe,
        ):
            transcript, error = _transcribe_testimony_review_excerpt(
                self.app,
                recording,
            )

        self.assertEqual(transcript, "")
        self.assertEqual(error, "Transcript was empty.")
        self.assertEqual(transcribe.call_count, 2)

    def test_background_analysis_builds_review_items_without_browser_request(self):
        review_root = self.root / "TestimonyReviewQueue"
        review_root.mkdir()
        recording = review_root / "REC00992.mp3"
        recording.write_bytes(b"background-analysis-audio")
        recording_id = _recording_id(recording)
        _save_testimony_review(
            self.app,
            recording_id=recording_id,
            source_path=str(recording),
            status="needs_review",
            service_date="2026-07-29",
            speaker_name="",
            testimony_title="",
            notes="",
            proposed_path="",
            duration_seconds=90,
        )

        recording_ids = _background_review_analysis_ids(self.app, limit=20)

        self.assertIn(recording_id, recording_ids)

    def test_finished_recorder_analysis_is_hidden_after_reload(self):
        self.app.testimony_transcript_job = {
            "state": "finished",
            "started_at": "",
            "finished_at": "",
            "current": "",
            "processed": 1,
            "saved": 1,
            "errors": 0,
            "skipped": 0,
            "total": 1,
            "message": "Finished. Saved 1; errors 0.",
        }
        self._login()

        response = self.client.get("/admin/recorder-review")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-state="finished" hidden', response.data)
        self.assertIn(
            b'panel.hidden = !["running", "failed"].includes(job.state);',
            response.data,
        )

    def test_testimony_delivery_rule_is_future_only_idempotent_and_uses_both_recipients(self):
        effective_from = "2026-07-28"
        _save_testimony_delivery_rule(
            self.app,
            rule_id=None,
            canonical_name="Rachel George",
            aliases=["Sister Rachel", "Sister Rachel George"],
            emails=["rachelgeorge106@gmail.com", "rachelgeorge106@yahoo.com"],
            effective_from=effective_from,
            enabled=True,
        )
        recording = self.testimony_root / "July 28, 2026 - Sister Rachel's Testimony.mp3"
        recording.write_bytes(b"future-testimony")
        recording_id = _recording_id(recording)
        source_recording_id = "raw-intake-id"
        _record_testimony_review_history(
            self.app,
            recording_id=recording_id,
            previous_recording_id=source_recording_id,
            action="save_speaker",
            previous_status="needs_review",
            new_status="identified",
            service_date=effective_from,
            speaker_name="Sister Rachel",
            recording_kind="testimony",
            source_path="/incoming/raw.mp3",
            target_path=str(recording),
        )
        with patch("ntc_recordings_app._start_testimony_delivery_job") as starter:
            historical = _queue_testimony_deliveries(
                self.app,
                recording_id=recording_id,
                recording_path=str(recording),
                recording_title=recording.stem,
                speaker_name="Sister Rachel",
                service_date="2026-07-05",
            )
            queued = _queue_testimony_deliveries(
                self.app,
                recording_id=recording_id,
                recording_path=str(recording),
                recording_title=recording.stem,
                speaker_name="Sister Rachel",
                service_date=effective_from,
            )
            renamed_recording = self.testimony_root / "July 28, 2026 - Rachel George's Testimony.mp3"
            renamed_recording.write_bytes(recording.read_bytes())
            renamed_recording_id = _recording_id(renamed_recording)
            _record_testimony_review_history(
                self.app,
                recording_id=renamed_recording_id,
                previous_recording_id=recording_id,
                action="save_speaker",
                previous_status="identified",
                new_status="identified",
                service_date=effective_from,
                speaker_name="Rachel George",
                recording_kind="testimony",
                source_path=str(recording),
                target_path=str(renamed_recording),
            )
            duplicate = _queue_testimony_deliveries(
                self.app,
                recording_id=renamed_recording_id,
                recording_path=str(renamed_recording),
                recording_title=renamed_recording.stem,
                speaker_name="Rachel George",
                service_date=effective_from,
            )
        self.assertEqual((historical, queued, duplicate), (0, 1, 0))
        starter.assert_called_once()

        with (
            patch(
                "ntc_recordings_app._create_nextcloud_share_link",
                return_value=("https://nextcloud.example.test/s/rachel", "42", ""),
            ),
            patch("ntc_recordings_app._send_html_email", return_value=(True, "")) as sender,
        ):
            _run_testimony_delivery_job(self.app)

        self.assertEqual(
            sender.call_args.kwargs["recipients"],
            ["rachelgeorge106@gmail.com", "rachelgeorge106@yahoo.com"],
        )
        with sqlite3.connect(self.db_path) as connection:
            delivery = connection.execute(
                "SELECT status, share_url, sent_at FROM testimony_deliveries"
            ).fetchone()
        self.assertEqual(delivery[0], "sent")
        self.assertEqual(delivery[1], "https://nextcloud.example.test/s/rachel")
        self.assertTrue(delivery[2])

    def test_delivery_rules_page_can_save_rule(self):
        self._login()

        response = self.client.post(
            "/admin/testimony-delivery/rules",
            data={
                "canonical_name": "Rachel George",
                "aliases": "Sister Rachel\nSister Rachel George",
                "emails": "rachelgeorge106@gmail.com\nrachelgeorge106@yahoo.com",
                "enabled": "1",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Automatic delivery rule saved for Rachel George", response.data)
        self.assertIn(b"<h1>Automatic Delivery</h1>", response.data)
        self.assertIn(b"<h2>Automatic Email Rules</h2>", response.data)
        self.assertIn(b"<h2>Recent Deliveries</h2>", response.data)
        self.assertIn(b"rachelgeorge106@gmail.com", response.data)
        self.assertIn(b"rachelgeorge106@yahoo.com", response.data)
        self.assertNotIn(b"Effective From", response.data)

    def test_delivery_rule_edit_preserves_hidden_effective_date(self):
        _save_testimony_delivery_rule(
            self.app,
            rule_id=None,
            canonical_name="Rachel George",
            aliases=["Sister Rachel"],
            emails=["rachelgeorge106@gmail.com"],
            effective_from="2026-07-28",
            enabled=True,
        )
        with sqlite3.connect(self.db_path) as connection:
            rule_id = connection.execute(
                "SELECT id FROM testimony_delivery_rules WHERE canonical_name = ?",
                ("Rachel George",),
            ).fetchone()[0]

        self._login()
        response = self.client.post(
            "/admin/testimony-delivery/rules",
            data={
                "rule_id": str(rule_id),
                "canonical_name": "Rachel George",
                "aliases": "Sister Rachel\nSister Rachel George",
                "emails": "rachelgeorge106@gmail.com\nrachelgeorge106@yahoo.com",
                "enabled": "1",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        with sqlite3.connect(self.db_path) as connection:
            effective_from = connection.execute(
                "SELECT effective_from FROM testimony_delivery_rules WHERE id = ?",
                (rule_id,),
            ).fetchone()[0]
        self.assertEqual(effective_from, "2026-07-28")

    def test_service_coverage_derives_messages_and_tracks_confirmed_exceptions(self):
        self.app.config["NTC_RECORDINGS_SERVICE_LEDGER_START_DATE"] = "2026-07-01"
        recordings = [
            RecordingCandidate(
                id="message-july-1",
                path=str(self.root / "July 1, 2026 - Wednesday Message.mp3"),
                title="July 1, 2026 - Wednesday Message",
                recording_date="2026-07-01",
                kind="message",
                size_bytes=100,
                modified_at="2026-07-01T20:00:00+00:00",
                relative_path="July 1, 2026 - Wednesday Message.mp3",
            ),
            RecordingCandidate(
                id="message-july-5",
                path=str(self.root / "July 5, 2026 - Sunday Message.mp3"),
                title="July 5, 2026 - Sunday Message",
                recording_date="2026-07-05",
                kind="message",
                size_bytes=100,
                modified_at="2026-07-05T20:00:00+00:00",
                relative_path="July 5, 2026 - Sunday Message.mp3",
            ),
        ]
        for service_date in ("2026-07-08", "2026-07-12", "2026-07-15"):
            _save_service_exception(
                self.app,
                service_date=service_date,
                service_kind="sunday" if service_date == "2026-07-12" else "wednesday",
                exception_type="convention",
                exception_note="Church convention; no regular local service was expected.",
                reviewed_by="Chandru Test",
                review_source="user_confirmed",
            )

        rows = _service_completeness_rows(
            self.app,
            recordings,
            through_date=date(2026, 7, 15),
        )

        self.assertEqual(len(rows), 5)
        self.assertEqual(
            {row["service_date"]: row["status"] for row in rows},
            {
                "2026-07-01": "complete",
                "2026-07-05": "complete",
                "2026-07-08": "exception",
                "2026-07-12": "exception",
                "2026-07-15": "exception",
            },
        )
        july_12 = next(row for row in rows if row["service_date"] == "2026-07-12")
        self.assertEqual(july_12["reviewed_by"], "Recordings Admin")
        self.assertEqual(july_12["review_source"], "user_confirmed")

    def test_service_coverage_is_internal_and_login_does_not_identify_reviewers(self):
        login_page = self.client.get("/admin/login")
        self.assertEqual(login_page.status_code, 200)
        self.assertNotIn(b'name="reviewer_name"', login_page.data)

        self._login()
        coverage = self.client.get("/admin/service-completeness", follow_redirects=False)

        self.assertEqual(coverage.status_code, 302)
        self.assertTrue(coverage.headers["Location"].endswith("/admin/panel"))

        admin_page = self.client.get("/admin/panel")
        review_page = self.client.get("/admin/recorder-review")
        self.assertNotIn(b">Coverage<", admin_page.data)
        self.assertNotIn(b">Coverage<", review_page.data)
        self.assertNotIn(b"Reviewed by", review_page.data)


if __name__ == "__main__":
    unittest.main()
