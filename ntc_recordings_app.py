"""Standalone recording request and share-link panel.

This panel intentionally uses its own database and service port so the
recording-request workflow cannot destabilize the WebCall/phone-call path.
"""

from __future__ import annotations

import hashlib
import html
import hmac
import json
import os
import re
import secrets
import shutil
import smtplib
import sqlite3
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from flask import Flask, has_request_context, jsonify, redirect, render_template_string, request, send_file, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from ntc_env import install_legacy_env_aliases
from ntc_branding import install_branding

install_legacy_env_aliases()


AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".aac"}
LIBRARY_EXCLUDED_DIR_NAMES = {
    "GoogleMessageTakeout",
    "_IncomingRecorderIntake",
    "_ImportAudit",
    "_RenameAudit",
    "DN300R",
    "_NeedsDate",
    "Diagnostics",
}
DEFAULT_MESSAGE_RECORDING_DIR = "/mnt/MainRecordings/Recordings/MessageRecordings"
DEFAULT_WORSHIP_RECORDING_DIR = "/mnt/MainRecordings/Recordings/WorshipRecordings"
DEFAULT_TESTIMONY_RECORDING_DIR = "/mnt/MainRecordings/Recordings/TestimonyRecordings"
DEFAULT_TESTIMONY_REJECTED_DIR = f"{DEFAULT_TESTIMONY_RECORDING_DIR}/.review-rejected"
DEFAULT_RECORDING_DIR = DEFAULT_MESSAGE_RECORDING_DIR
DEFAULT_RECORDING_DIRS = f"message:{DEFAULT_MESSAGE_RECORDING_DIR},worship:{DEFAULT_WORSHIP_RECORDING_DIR},testimony:{DEFAULT_TESTIMONY_RECORDING_DIR}"
TESTIMONY_REVIEW_FILTERS = {"needs_review", "message_review", "identified", "grouped", "not_testimony", "duplicate", "all"}
TESTIMONY_REVIEW_STATUSES = {"needs_review", "message_review", "identified", "grouped", "not_testimony", "duplicate", "already_named"}
TESTIMONY_REVIEW_EDITABLE_STATUSES = {"needs_review", "message_review", "identified", "grouped", "not_testimony", "duplicate"}
TESTIMONY_FINAL_AUDIO_EXTENSION = ".mp3"
TESTIMONY_EVENT_FOLDERS = {
    "2021-08-30": ("Funeral Testimonies", "August 30, 2021 - Sister Marg's Funeral"),
    "2025-04-11": ("Funeral Testimonies", "April 11-12, 2025 - Sister Marykutty's Funeral"),
    "2025-04-12": ("Funeral Testimonies", "April 11-12, 2025 - Sister Marykutty's Funeral"),
    "2025-04-20": ("Funeral Testimonies", "April 20-21, 2025 - Brother K.T. Varghese's Funeral"),
    "2025-04-21": ("Funeral Testimonies", "April 20-21, 2025 - Brother K.T. Varghese's Funeral"),
    "2025-04-22": ("Funeral Testimonies", "April 22-23, 2025 - Sister Kathy's Funeral"),
    "2025-04-23": ("Funeral Testimonies", "April 22-23, 2025 - Sister Kathy's Funeral"),
    "2022-11-22": ("Funeral Testimonies", "November 22-23, 2022 - Sister Olinka's Funeral Service"),
    "2022-11-23": ("Funeral Testimonies", "November 22-23, 2022 - Sister Olinka's Funeral Service"),
}
TESTIMONY_MESSAGE_INTRO_PATTERNS = [
    r"\bshall\s+we\s+turn\s+to\b",
    r"\blet'?s\s+turn\s+to\b",
    r"\bturn\s+with\s+me\s+to\b",
    r"\bturn\s+to\s+(?:the\s+book\s+of\s+)?[1-3]?\s*[a-z]+\b",
    r"\b[a-z]+\s+chapter\s+\d+\b",
    r"\bchapter\s+\d+\s+(?:and|in|verse|verses)\b",
    r"\bverse\s+\d+\b",
    r"\bverses\s+\d+\s*(?:and|-|through|to)\s*\d+\b",
    r"\byou\s+may\s+be\s+seated\b",
    r"\bplease\s+be\s+seated\b",
    r"\bbe\s+seated\b",
    r"\bword\s+of\s+god\s+says\b",
    r"\bshall\s+we\s+pray\b",
    r"\bthank\s+you\s+for\s+your\s+word\b",
    r"\bspeak(?:ing)?\s+to\s+us\s+from\s+your\s+word\b",
    r"\bwonderful\s+work\s+that\s+god\s+has\s+done\s+in\s+our\s+children\b",
]
TESTIMONY_EXPLICIT_INTRO_PATTERNS = [
    r"\bmy\s+name\s+is\b",
    r"\bfor\s+those\s+of\s+you\s+who\s+do\s+not\s+know\s+me\b",
    r"\bi\s+am\s+here\s+to\s+testify\b",
    r"\bi\s+want\s+to\s+(?:thank|praise)\s+(?:and\s+)?(?:thank\s+)?god\b",
    r"\bmy\s+testimony\b",
    r"\btestimony\s+is\b",
]
MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
TRANSCRIPT_NAME_BOUNDARY_WORDS = {
    "actually",
    "also",
    "and",
    "are",
    "as",
    "at",
    "because",
    "been",
    "being",
    "but",
    "can",
    "could",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "he",
    "i",
    "in",
    "is",
    "it",
    "may",
    "might",
    "must",
    "of",
    "on",
    "she",
    "should",
    "so",
    "that",
    "then",
    "they",
    "this",
    "to",
    "today",
    "tonight",
    "uh",
    "um",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "will",
    "with",
    "would",
    "you",
}
TRANSCRIPT_NAME_REJECT_FIRST_WORDS = {
    "a",
    "able",
    "all",
    "before",
    "blessed",
    "completely",
    "deeply",
    "driving",
    "for",
    "going",
    "grateful",
    "happy",
    "here",
    "his",
    "living",
    "looking",
    "not",
    "on",
    "set",
    "she",
    "sorry",
    "such",
    "sure",
    "thankful",
    "that",
    "the",
    "this",
    "to",
    "waiting",
    "with",
    "your",
}
TRANSCRIPT_NAME_REJECT_WORDS = {
    "able",
    "all",
    "always",
    "blessing",
    "body",
    "burdensome",
    "come",
    "completely",
    "deeply",
    "discomfort",
    "emotional",
    "family",
    "give",
    "goodness",
    "grateful",
    "happening",
    "heart",
    "honor",
    "hopeless",
    "in",
    "late",
    "lie",
    "life",
    "looking",
    "me",
    "morning",
    "music",
    "my",
    "myself",
    "not",
    "of",
    "on",
    "praise",
    "right",
    "saying",
    "sinner",
    "song",
    "testimony",
    "to",
    "trials",
    "us",
    "very",
    "waiting",
    "word",
    "your",
}


@dataclass(frozen=True)
class RecordingCandidate:
    id: str
    path: str
    title: str
    recording_date: str
    kind: str
    size_bytes: int
    modified_at: str
    relative_path: str
    target_type: str = "file"
    file_count: int = 1


class ClosingSQLiteConnection(sqlite3.Connection):
    """Commit or roll back like sqlite3.Connection, then close the handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        suppress = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return suppress


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    app.config.update(
        SECRET_KEY=os.getenv("NTC_RECORDINGS_SECRET_KEY") or os.getenv("NTC_SECRET_KEY") or "change-me",
        NTC_RECORDINGS_DB_PATH=os.getenv("NTC_RECORDINGS_DB_PATH", "data/recording-requests.db"),
        NTC_RECORDINGS_LIBRARY_DIRS=os.getenv("NTC_RECORDINGS_LIBRARY_DIRS", DEFAULT_RECORDING_DIRS),
        NTC_RECORDINGS_TESTIMONY_SOURCE_DIR=(
            os.getenv("NTC_RECORDINGS_TESTIMONY_SOURCE_DIR")
            or os.getenv("NTC_RECORDINGS_DN300R_DIR")
            or str(Path(DEFAULT_MESSAGE_RECORDING_DIR) / "DN300R")
        ),
        NTC_RECORDINGS_TESTIMONY_LIBRARY_DIR=os.getenv("NTC_RECORDINGS_TESTIMONY_LIBRARY_DIR", DEFAULT_TESTIMONY_RECORDING_DIR),
        NTC_RECORDINGS_TESTIMONY_REJECTED_DIR=os.getenv("NTC_RECORDINGS_TESTIMONY_REJECTED_DIR", DEFAULT_TESTIMONY_REJECTED_DIR),
        NTC_RECORDINGS_MAX_SCAN_FILES=int(os.getenv("NTC_RECORDINGS_MAX_SCAN_FILES", "4000")),
        NTC_RECORDINGS_TESTIMONY_PROBE_LIMIT=int(os.getenv("NTC_RECORDINGS_TESTIMONY_PROBE_LIMIT", "80")),
        NTC_RECORDINGS_TESTIMONY_MIN_SECONDS=int(os.getenv("NTC_RECORDINGS_TESTIMONY_MIN_SECONDS", "45")),
        NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_URL=(
            os.getenv("NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_URL")
            or os.getenv("NTC_TRANSCRIPTION_LOCAL_URL", "")
        ),
        NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_SECONDS=int(os.getenv("NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_SECONDS", "90")),
        NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_TIMEOUT=float(os.getenv("NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_TIMEOUT", "120")),
        NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_PROMPT=os.getenv(
            "NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_PROMPT",
            "This is a church testimony. The speaker may introduce themselves by saying my name is, I am, or I'm.",
        ),
        NTC_RECORDINGS_TESTIMONY_TRANSCRIPT_SECONDS=int(os.getenv("NTC_RECORDINGS_TESTIMONY_TRANSCRIPT_SECONDS", "240")),
        NTC_RECORDINGS_TESTIMONY_TRANSCRIPT_MAX_TOKENS=int(os.getenv("NTC_RECORDINGS_TESTIMONY_TRANSCRIPT_MAX_TOKENS", "384")),
        NTC_RECORDINGS_TESTIMONY_TRANSCRIPT_LIMIT=int(os.getenv("NTC_RECORDINGS_TESTIMONY_TRANSCRIPT_LIMIT", "30")),
        NTC_RECORDINGS_INDEX_REFRESH_SECONDS=float(
            os.getenv(
                "NTC_RECORDINGS_INDEX_REFRESH_SECONDS",
                os.getenv("NTC_RECORDINGS_SCAN_CACHE_SECONDS", "300"),
            )
        ),
        NTC_RECORDINGS_PANEL_TITLE=os.getenv("NTC_RECORDINGS_PANEL_TITLE", "NTC NAS Recordings"),
        NTC_RECORDINGS_PUBLIC_BASE_URL=os.getenv("NTC_RECORDINGS_PUBLIC_BASE_URL", ""),
        NTC_RECORDINGS_PUBLIC_PREFIX=os.getenv("NTC_RECORDINGS_PUBLIC_PREFIX", ""),
        NTC_RECORDINGS_SHARE_PROVIDER=os.getenv("NTC_RECORDINGS_SHARE_PROVIDER", "internal"),
        NTC_RECORDINGS_ADMIN_PASSWORD=os.getenv("NTC_RECORDINGS_ADMIN_PASSWORD", ""),
        NTC_ADMIN_PASSWORD=os.getenv("NTC_ADMIN_PASSWORD", ""),
        NTC_ADMIN_SESSION_HOURS=float(os.getenv("NTC_ADMIN_SESSION_HOURS", "8")),
        NTC_RECORDINGS_LOGO_URL=os.getenv(
            "NTC_RECORDINGS_LOGO_URL",
            "https://drive.google.com/uc?id=1QiyDf3SW6jHcctra1qr5DKXlLn2_GCE0",
        ),
        NTC_NEXTCLOUD_BASE_URL=os.getenv("NTC_NEXTCLOUD_BASE_URL", ""),
        NTC_NEXTCLOUD_USERNAME=os.getenv("NTC_NEXTCLOUD_USERNAME", ""),
        NTC_NEXTCLOUD_APP_PASSWORD=os.getenv("NTC_NEXTCLOUD_APP_PASSWORD", ""),
        NTC_NEXTCLOUD_LOCAL_PATH_PREFIX=os.getenv("NTC_NEXTCLOUD_LOCAL_PATH_PREFIX", DEFAULT_RECORDING_DIR),
        NTC_NEXTCLOUD_PATH_PREFIX=os.getenv("NTC_NEXTCLOUD_PATH_PREFIX", ""),
        NTC_NEXTCLOUD_PATH_MAPPINGS=os.getenv("NTC_NEXTCLOUD_PATH_MAPPINGS", ""),
        NTC_RECORDINGS_EMAIL_ENABLED=os.getenv("NTC_RECORDINGS_EMAIL_ENABLED", "0"),
        NTC_RECORDINGS_EMAIL_FROM=os.getenv("NTC_RECORDINGS_EMAIL_FROM", ""),
        NTC_RECORDINGS_SMTP_HOST=os.getenv("NTC_RECORDINGS_SMTP_HOST", ""),
        NTC_RECORDINGS_SMTP_PORT=int(os.getenv("NTC_RECORDINGS_SMTP_PORT", "587")),
        NTC_RECORDINGS_SMTP_USERNAME=os.getenv("NTC_RECORDINGS_SMTP_USERNAME", ""),
        NTC_RECORDINGS_SMTP_PASSWORD=os.getenv("NTC_RECORDINGS_SMTP_PASSWORD", ""),
        NTC_RECORDINGS_SMTP_STARTTLS=os.getenv("NTC_RECORDINGS_SMTP_STARTTLS", "1"),
        NTC_RECORDINGS_AUTO_ARCHIVE_DAYS=int(os.getenv("NTC_RECORDINGS_AUTO_ARCHIVE_DAYS", "30")),
    )
    if test_config:
        app.config.update(test_config)
    app.permanent_session_lifetime = timedelta(hours=max(1, float(app.config.get("NTC_ADMIN_SESSION_HOURS") or 8)))

    install_branding(app)
    _init_db(app.config["NTC_RECORDINGS_DB_PATH"])
    app.recordings_index_lock = threading.Lock()
    app.testimony_suggestion_job_lock = threading.Lock()
    app.testimony_suggestion_job = _initial_testimony_suggestion_job_state()
    app.testimony_transcript_job_lock = threading.Lock()
    app.testimony_transcript_job = _initial_testimony_transcript_job_state()

    @app.context_processor
    def _recordings_url_context():
        return {"recordings_url_for": lambda endpoint, **values: _recordings_url_for(app, endpoint, **values)}

    def _admin_password() -> str:
        return (
            app.config.get("NTC_RECORDINGS_ADMIN_PASSWORD")
            or app.config.get("NTC_ADMIN_PASSWORD")
            or ""
        ).strip()

    def _is_admin() -> bool:
        if not session.get("recordings_admin"):
            return False
        authenticated_at = session.get("recordings_admin_authenticated_at")
        try:
            authenticated = datetime.fromisoformat(str(authenticated_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            session.pop("recordings_admin", None)
            session.pop("recordings_admin_authenticated_at", None)
            session.modified = True
            return False
        timeout = timedelta(hours=max(1, float(app.config.get("NTC_ADMIN_SESSION_HOURS") or 8)))
        if datetime.now(timezone.utc) - authenticated > timeout:
            session.pop("recordings_admin", None)
            session.pop("recordings_admin_authenticated_at", None)
            session.modified = True
            return False
        return True

    def _require_admin():
        if _is_admin():
            return None
        if _wants_json_response():
            return jsonify({"ok": False, "error": "Admin session expired. Sign in again, then retry the testimony update."}), 401
        return _redirect_to(app, "admin_login", error="Admin password required.")

    @app.get("/healthz")
    def healthz():
        try:
            recordings = _get_recordings(app)
            counts_by_kind = _recording_counts_by_kind(recordings)
            _connect(app.config["NTC_RECORDINGS_DB_PATH"]).close()
            return jsonify(
                {
                    "ok": True,
                    "recording_count": len(recordings),
                    "recording_counts_by_kind": counts_by_kind,
                    "timestamp": _utc_now(),
                }
            )
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            app.logger.exception("recording request panel health check failed")
            return jsonify({"ok": False, "error": str(exc)}), 500

    @app.get("/")
    def public_form():
        recordings = _get_recordings(app)
        return render_template_string(
            RECORDING_PUBLIC_TEMPLATE,
            title=app.config["NTC_RECORDINGS_PANEL_TITLE"],
            recording_dates=_public_recording_date_options(recordings),
            format_date=_format_date,
            message=request.args.get("message"),
            error=request.args.get("error"),
        )

    @app.post("/request")
    def create_request():
        requester_name = (request.form.get("requester_name") or "").strip()
        email = (request.form.get("email") or "").strip()
        secondary_email = (request.form.get("secondary_email") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        recording_kind = _normalize_recording_kind(request.form.get("recording_kind") or "message")
        requested_date = _normalize_date((request.form.get("requested_date") or "").strip())
        notes = (request.form.get("notes") or "").strip()
        if not requester_name or not email or not requested_date:
            return _redirect_to(app, "public_form", error="Name, email, and recording date are required.")
        recordings = _get_recordings(app)
        candidate = _default_candidate_for_date(app, recordings, requested_date, recording_kind)
        if not candidate:
            return _redirect_to(app, "public_form", error="Please choose one of the available recording dates.")
        request_id = _insert_request(
            app,
            requester_name=requester_name,
            email=email,
            secondary_email=secondary_email,
            phone=phone,
            recording_kind=recording_kind,
            requested_date=requested_date,
            candidate=candidate,
            notes=notes,
        )
        app.logger.info("recording request %s created for %s (%s)", request_id, requested_date, candidate.id)
        return _redirect_to(app, "public_form", message="Request submitted. We will email the recording link when it is approved.")

    @app.route("/admin", methods=["GET", "POST"])
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            expected = _admin_password()
            if not expected:
                return render_template_string(
                    RECORDING_ADMIN_LOGIN_TEMPLATE,
                    title=app.config["NTC_RECORDINGS_PANEL_TITLE"],
                    error="Admin access is not configured.",
                )
            password = request.form.get("password", "")
            if hmac.compare_digest(password, expected):
                session.permanent = True
                session["recordings_admin"] = True
                session["recordings_admin_authenticated_at"] = datetime.now(timezone.utc).isoformat()
                session.modified = True
                return _redirect_to(app, "admin_panel")
            return render_template_string(
                RECORDING_ADMIN_LOGIN_TEMPLATE,
                title=app.config["NTC_RECORDINGS_PANEL_TITLE"],
                error="Password was not accepted.",
            )
        if _is_admin():
            return _redirect_to(app, "admin_panel")
        return render_template_string(
            RECORDING_ADMIN_LOGIN_TEMPLATE,
            title=app.config["NTC_RECORDINGS_PANEL_TITLE"],
            error=request.args.get("error"),
        )

    @app.post("/admin/logout")
    def admin_logout():
        session.pop("recordings_admin", None)
        session.pop("recordings_admin_authenticated_at", None)
        session.modified = True
        return _redirect_to(app, "public_form")

    @app.get("/admin/panel")
    def admin_panel():
        guard = _require_admin()
        if guard:
            return guard
        _auto_archive_closed_requests(app)
        recordings = _get_recordings(app)
        requests = _list_requests(app)
        active_tab = (request.args.get("tab") or "pending").strip().lower()
        if active_tab in {"active", "closed", "archived"}:
            active_tab = "completed"
        if active_tab not in {"pending", "completed"}:
            active_tab = "pending"
        pending_requests = [item for item in requests if not item["archived_at"] and item["status"] == "pending"]
        active_requests = [item for item in requests if not item["archived_at"] and item["status"] in {"ready", "sent"}]
        closed_requests = [item for item in requests if not item["archived_at"] and item["status"] == "revoked"]
        archived_requests = [item for item in requests if item["archived_at"]]
        completed_requests = active_requests + closed_requests + archived_requests
        visible_requests = pending_requests if active_tab == "pending" else completed_requests
        candidates_by_request = {}
        for item in visible_requests:
            candidates_by_request[item["id"]] = _candidate_options_for_request(app, recordings, item)
        candidate_groups_by_request = {
            item["id"]: _candidate_groups(candidates_by_request[item["id"]])
            for item in visible_requests
        }
        tab_copy = {
            "pending": (
                "Pending Requests",
                "New requests. Open a row, confirm the selection, then send the link.",
                "No pending requests.",
            ),
            "completed": (
                "Completed Requests",
                "Prepared, sent, revoked, and archived requests live here as compact history rows.",
                "No completed requests.",
            ),
        }
        tab_title, tab_description, empty_message = tab_copy[active_tab]
        return render_template_string(
            RECORDING_ADMIN_TEMPLATE,
            title=app.config["NTC_RECORDINGS_PANEL_TITLE"],
            requests=visible_requests,
            request_groups=_request_groups(visible_requests),
            pending_count=len(pending_requests),
            active_count=len(active_requests),
            closed_count=len(closed_requests),
            archived_count=len(archived_requests),
            completed_count=len(completed_requests),
            recording_count=len(recordings),
            recording_counts_by_kind=_recording_counts_by_kind(recordings),
            active_tab=active_tab,
            tab_title=tab_title,
            tab_description=tab_description,
            empty_message=empty_message,
            auto_archive_days=int(app.config.get("NTC_RECORDINGS_AUTO_ARCHIVE_DAYS") or 0),
            candidates_by_request=candidates_by_request,
            candidate_groups_by_request=candidate_groups_by_request,
            email_enabled=_email_enabled(app),
            share_provider=(app.config.get("NTC_RECORDINGS_SHARE_PROVIDER") or "internal"),
            message=request.args.get("message"),
            error=request.args.get("error"),
            default_email_message=_default_recording_email_message,
            candidate_option_label=_candidate_option_label,
            status_label=_status_label,
            format_date=_format_date,
            format_datetime=_format_datetime,
        )

    @app.get("/admin/testimonies")
    def legacy_testimony_review_redirect():
        values = {}
        for key in ("status", "sort", "limit", "message", "error"):
            value = request.args.get(key)
            if value is not None:
                values[key] = value
        return _redirect_to(app, "testimony_review", **values)

    @app.get("/admin/recorder-review")
    def testimony_review():
        guard = _require_admin()
        if guard:
            return guard
        status_filter = (request.args.get("status") or "needs_review").strip().lower()
        if status_filter == "already_named":
            status_filter = "identified"
        if status_filter not in TESTIMONY_REVIEW_FILTERS:
            status_filter = "needs_review"
        sort = (request.args.get("sort") or "shortest").strip().lower()
        if sort not in {"shortest", "newest", "name"}:
            sort = "shortest"
        try:
            limit = int(request.args.get("limit") or "100")
        except ValueError:
            limit = 100
        limit = min(max(limit, 1), 500)
        items = _testimony_review_items(app)
        counts = _testimony_status_counts(items)
        if status_filter == "all":
            visible_items = items
        elif status_filter == "identified":
            visible_items = [item for item in items if item["status"] in {"identified", "already_named"}]
        else:
            visible_items = [item for item in items if item["status"] == status_filter]
        _sort_testimony_items(visible_items, sort)
        visible_items = visible_items[:limit]
        root = _testimony_source_root(app)
        return render_template_string(
            TESTIMONY_REVIEW_TEMPLATE,
            title=app.config["NTC_RECORDINGS_PANEL_TITLE"],
            items=visible_items,
            counts=counts,
            status_filter=status_filter,
            sort=sort,
            limit=limit,
            testimony_source_root=str(root),
            testimony_source_exists=root.exists() and root.is_dir(),
            probe_limit=int(app.config.get("NTC_RECORDINGS_TESTIMONY_PROBE_LIMIT") or 80),
            message=request.args.get("message"),
            error=request.args.get("error"),
            status_label=_testimony_status_label,
            format_date=_format_date,
            speaker_names=_testimony_known_speakers(app),
            suggestion_job=_testimony_suggestion_job_status(app),
            transcript_job=_testimony_transcript_job_status(app),
        )

    @app.post("/admin/testimonies/probe")
    def probe_testimony_durations():
        guard = _require_admin()
        if guard:
            return guard
        try:
            limit = int(request.form.get("limit") or app.config.get("NTC_RECORDINGS_TESTIMONY_PROBE_LIMIT") or 80)
        except ValueError:
            limit = int(app.config.get("NTC_RECORDINGS_TESTIMONY_PROBE_LIMIT") or 80)
        limit = min(max(limit, 1), 120)
        probed, skipped = _probe_missing_testimony_durations(app, limit)
        current_filter = (request.form.get("status") or "needs_review").strip().lower()
        if current_filter == "already_named":
            current_filter = "identified"
        if current_filter not in TESTIMONY_REVIEW_FILTERS:
            current_filter = "needs_review"
        return _redirect_to(
            app,
            "testimony_review",
            status=current_filter,
            sort=request.form.get("sort") or "shortest",
            message=f"Checked {probed + skipped} recorder source file{'s' if probed + skipped != 1 else ''}; saved {probed} duration{'s' if probed != 1 else ''}.",
        )

    @app.post("/admin/testimonies/suggest-all")
    def suggest_all_testimony_speakers():
        guard = _require_admin()
        if guard:
            return guard
        current_filter = (request.form.get("status") or "needs_review").strip().lower()
        if current_filter == "already_named":
            current_filter = "identified"
        if current_filter not in TESTIMONY_REVIEW_FILTERS:
            current_filter = "needs_review"
        started = _start_testimony_suggestion_job(app)
        if started:
            message = "Started recorder suggestion processing."
        else:
            message = "Recorder suggestion processing is already running."
        return _redirect_to(
            app,
            "testimony_review",
            status=current_filter,
            sort=request.form.get("sort") or "shortest",
            message=message,
        )

    @app.get("/admin/testimonies/suggest-status")
    def testimony_suggestion_status():
        guard = _require_admin()
        if guard:
            return guard
        return jsonify(_testimony_suggestion_job_status(app))

    @app.post("/admin/testimonies/transcribe-identified")
    def transcribe_identified_testimonies():
        guard = _require_admin()
        if guard:
            return guard
        current_filter = (request.form.get("status") or "identified").strip().lower()
        if current_filter == "already_named":
            current_filter = "identified"
        if current_filter not in TESTIMONY_REVIEW_FILTERS:
            current_filter = "identified"
        try:
            limit = int(request.form.get("limit") or app.config.get("NTC_RECORDINGS_TESTIMONY_TRANSCRIPT_LIMIT") or 30)
        except ValueError:
            limit = int(app.config.get("NTC_RECORDINGS_TESTIMONY_TRANSCRIPT_LIMIT") or 30)
        target_statuses = _testimony_transcript_statuses_for_filter(current_filter)
        started = _start_testimony_transcript_job(app, min(max(limit, 1), 100), statuses=target_statuses)
        if started:
            message = "Started recorder transcript processing."
        else:
            message = "Recorder transcript processing is already running."
        return _redirect_to(
            app,
            "testimony_review",
            status=current_filter,
            sort=request.form.get("sort") or "shortest",
            message=message,
        )

    @app.get("/admin/testimonies/transcript-status")
    def testimony_transcript_status():
        guard = _require_admin()
        if guard:
            return guard
        return jsonify(_testimony_transcript_job_status(app))

    @app.post("/admin/testimonies/<recording_id>/transcript")
    def transcribe_testimony_recording(recording_id: str):
        guard = _require_admin()
        if guard:
            return guard
        current_filter = (request.form.get("status_filter") or "needs_review").strip().lower()
        if current_filter == "already_named":
            current_filter = "identified"
        if current_filter not in TESTIMONY_REVIEW_FILTERS:
            current_filter = "needs_review"
        candidate = _testimony_source_recording_from_form(app, recording_id)
        if not candidate:
            if _wants_json_response():
                return jsonify({"ok": False, "error": "Recorder source file was not found."}), 404
            return _redirect_to(app, "testimony_review", status=current_filter, error="Recorder source file was not found.")

        existing = _testimony_review_row(app, recording_id)
        duration_seconds = _row_duration(existing) if existing else _probe_audio_duration(Path(candidate.path))
        service_date = (
            _normalize_date((request.form.get("service_date") or "").strip())
            or (str(existing["service_date"] or "") if existing else "")
            or candidate.recording_date
            or ""
        )
        speaker_name = (request.form.get("speaker_name") or "").strip() or (str(existing["speaker_name"] or "") if existing else "")
        group_title = (request.form.get("group_title") or "").strip() or (str(existing["testimony_title"] or "") if existing else "")
        status = str(existing["status"] or "") if existing else _testimony_status_for_candidate(app, candidate, None, duration_seconds)
        if status not in TESTIMONY_REVIEW_STATUSES:
            status = "needs_review"
        testimony_title = group_title if status == "grouped" else (_testimony_title_for_speaker(speaker_name) if speaker_name else (str(existing["testimony_title"] or "") if existing else ""))
        notes = str(existing["notes"] or "") if existing else ""
        proposed_path = str(existing["proposed_path"] or "") if existing else ""
        suggested_speaker = _valid_person_name_suggestion(str(existing["suggested_speaker"] or "") if existing else "", _testimony_known_speakers(app))
        suggestion_source = str(existing["suggestion_source"] or "") if existing else ""
        suggestion_text = str(existing["suggestion_text"] or "") if existing else ""

        _save_testimony_review(
            app,
            recording_id=recording_id,
            source_path=candidate.path,
            status="identified" if status == "already_named" else status,
            service_date=service_date,
            speaker_name=speaker_name,
            testimony_title=testimony_title,
            notes=notes,
            proposed_path=proposed_path,
            duration_seconds=duration_seconds,
        )

        transcript_text = ""
        transcript_error = ""
        try:
            transcript_text, transcript_error = _transcribe_testimony_review_excerpt(app, Path(candidate.path))
            if not transcript_text:
                transcript_error = transcript_error or "Transcript was empty."
        except Exception as exc:
            transcript_error = f"Transcript failed: {exc}"
            app.logger.exception("testimony row transcript failed for %s", candidate.path)
        transcript_source = "transcript_excerpt" if transcript_text else ""
        _save_testimony_transcript(
            app,
            recording_id,
            transcript_text=transcript_text,
            transcript_source=transcript_source,
            transcript_error=transcript_error,
        )

        if transcript_text:
            known_speakers = _testimony_known_speakers(app)
            transcript_speaker = _valid_person_name_suggestion(_extract_intro_speaker(transcript_text, known_speakers), known_speakers)
            if transcript_speaker:
                suggested_speaker = transcript_speaker
            if transcript_speaker or not suggestion_source:
                suggestion_source = "transcript_excerpt"
                suggestion_text = _compact_transcript_excerpt(transcript_text)
                _save_testimony_review(
                    app,
                    recording_id=recording_id,
                    source_path=candidate.path,
                    status="identified" if status == "already_named" else status,
                    service_date=service_date,
                    speaker_name=speaker_name,
                    testimony_title=testimony_title,
                    notes=notes,
                    proposed_path=proposed_path,
                    duration_seconds=duration_seconds,
                    suggested_speaker=suggested_speaker,
                    suggestion_source=suggestion_source,
                    suggestion_text=suggestion_text,
                )

        message = "Transcript processed." if transcript_text else transcript_error or "Transcript was not saved."
        if _wants_json_response():
            response = jsonify(
                {
                    "ok": bool(transcript_text),
                    "message": message,
                    "error": "" if transcript_text else message,
                    "recording_id": recording_id,
                    "status": "identified" if status == "already_named" else status,
                    "status_label": _testimony_status_label(status),
                    "service_date": service_date,
                    "service_date_label": _format_date(service_date),
                    "speaker_name": speaker_name,
                    "group_title": testimony_title if status in {"grouped", "message_review"} else "",
                    "title": candidate.title,
                    "source_label": Path(candidate.path).name,
                    "source_path": candidate.path,
                    "suggested_speaker": suggested_speaker,
                    "suggestion_source": suggestion_source,
                    "suggestion_source_label": _testimony_suggestion_source_label(suggestion_source),
                    "suggestion_text": suggestion_text,
                    "transcript_text": transcript_text,
                    "transcript_excerpt": _compact_transcript_excerpt(transcript_text, 900),
                    "transcript_preview": _testimony_display_transcript_preview(transcript_text, suggestion_source, suggestion_text, 900),
                    "transcript_error": transcript_error,
                    "transcript_updated_label": _format_datetime(_utc_now()),
                    "audio_url": _recordings_url_for(app, "testimony_audio", recording_id=recording_id),
                    "review_url": _recordings_url_for(app, "update_testimony_review", recording_id=recording_id),
                }
            )
            return (response, 200 if transcript_text else 500)
        return _redirect_to(
            app,
            "testimony_review",
            status=current_filter,
            sort=request.form.get("sort") or "shortest",
            message=message if transcript_text else None,
            error=None if transcript_text else message,
        )

    @app.post("/admin/testimonies/quarantine")
    def quarantine_testimony_reviews():
        guard = _require_admin()
        if guard:
            return guard
        current_filter = (request.form.get("status") or "not_testimony").strip().lower()
        if current_filter == "already_named":
            current_filter = "identified"
        if current_filter not in TESTIMONY_REVIEW_FILTERS:
            current_filter = "not_testimony"
        if current_filter in {"not_testimony", "duplicate"}:
            statuses = {current_filter}
        else:
            statuses = {"not_testimony", "duplicate"}
        moved, skipped, errors = _quarantine_testimony_reviews(app, statuses)
        if len(statuses) == 1 and "duplicate" in statuses:
            label = "duplicate file"
        elif len(statuses) == 1 and "not_testimony" in statuses:
            label = "not-testimony file"
        else:
            label = "rejected file"
        message = f"Moved {moved} {label}{'s' if moved != 1 else ''} to quarantine; skipped {skipped}; errors {errors}."
        return _redirect_to(
            app,
            "testimony_review",
            status=current_filter,
            sort=request.form.get("sort") or "shortest",
            message=message,
        )

    @app.post("/admin/testimonies/<recording_id>/suggest")
    def suggest_testimony_speaker(recording_id: str):
        guard = _require_admin()
        if guard:
            return guard
        current_filter = (request.form.get("status_filter") or "needs_review").strip().lower()
        if current_filter == "already_named":
            current_filter = "identified"
        if current_filter not in TESTIMONY_REVIEW_FILTERS:
            current_filter = "needs_review"
        candidate = _testimony_source_recording_from_form(app, recording_id)
        if not candidate:
            if _wants_json_response():
                return jsonify({"ok": False, "error": "Recorder source file was not found."}), 404
            return _redirect_to(app, "testimony_review", status=current_filter, error="Recorder source file was not found.")

        existing = _testimony_review_row(app, recording_id)
        duration_seconds = _row_duration(existing) if existing else _probe_audio_duration(Path(candidate.path))
        service_date = (
            _normalize_date((request.form.get("service_date") or "").strip())
            or (str(existing["service_date"] or "") if existing else "")
            or candidate.recording_date
            or ""
        )
        speaker_name = (request.form.get("speaker_name") or "").strip() or (str(existing["speaker_name"] or "") if existing else "")
        testimony_title = _testimony_title_for_speaker(speaker_name) if speaker_name else (str(existing["testimony_title"] or "") if existing else "")
        notes = str(existing["notes"] or "") if existing else ""
        proposed_path = str(existing["proposed_path"] or "") if existing else ""
        status = str(existing["status"] or "") if existing else _testimony_status_for_candidate(app, candidate, None, duration_seconds)
        if status not in TESTIMONY_REVIEW_STATUSES:
            status = "needs_review"

        suggested_speaker, suggestion_source, suggestion_text, suggestion_error = _generate_testimony_speaker_suggestion(app, candidate)
        _save_testimony_review(
            app,
            recording_id=recording_id,
            source_path=candidate.path,
            status="identified" if status == "already_named" else status,
            service_date=service_date,
            speaker_name=speaker_name,
            testimony_title=testimony_title,
            notes=notes,
            proposed_path=proposed_path,
            duration_seconds=duration_seconds,
            suggested_speaker=suggested_speaker,
            suggestion_source=suggestion_source,
            suggestion_text=suggestion_text,
        )
        if suggested_speaker:
            message = f"Suggested speaker: {suggested_speaker}."
        else:
            message = suggestion_error or "No speaker suggestion found."
        if _wants_json_response():
            return jsonify(
                {
                    "ok": True,
                    "message": message,
                    "recording_id": recording_id,
                    "suggested_speaker": suggested_speaker,
                    "suggestion_source": suggestion_source,
                    "suggestion_source_label": _testimony_suggestion_source_label(suggestion_source),
                    "suggestion_text": suggestion_text,
                    "transcript_text": (transcript_text := _row_optional_text(existing, "transcript_text")),
                    "transcript_excerpt": _compact_transcript_excerpt(transcript_text, 900),
                    "transcript_preview": _testimony_display_transcript_preview(transcript_text, suggestion_source, suggestion_text, 900),
                    "transcript_error": _row_optional_text(existing, "transcript_error"),
                    "transcript_updated_label": _format_datetime(_row_optional_text(existing, "transcript_updated_at")),
                }
            )
        return _redirect_to(
            app,
            "testimony_review",
            status=current_filter,
            sort=request.form.get("sort") or "shortest",
            message=message,
        )

    @app.post("/admin/testimonies/<recording_id>/review")
    def update_testimony_review(recording_id: str):
        guard = _require_admin()
        if guard:
            return guard
        current_filter = (request.form.get("status_filter") or "needs_review").strip().lower()
        if current_filter == "already_named":
            current_filter = "identified"
        if current_filter not in TESTIMONY_REVIEW_FILTERS:
            current_filter = "needs_review"
        candidate = _testimony_source_recording_from_form(app, recording_id)
        if not candidate:
            if _wants_json_response():
                return jsonify({"ok": False, "error": "Recorder source file was not found."}), 404
            return _redirect_to(app, "testimony_review", status=current_filter, error="Recorder source file was not found.")
        original_recording_id = recording_id
        status = (request.form.get("status") or "identified").strip().lower()
        if status not in TESTIMONY_REVIEW_EDITABLE_STATUSES:
            status = "identified"
        existing = _testimony_review_row(app, recording_id)
        service_date = (
            _normalize_date((request.form.get("service_date") or "").strip())
            or (str(existing["service_date"] or "") if existing else "")
            or candidate.recording_date
            or ""
        )
        speaker_name = (request.form.get("speaker_name") or "").strip()
        group_title = (request.form.get("group_title") or "").strip()
        known_speakers = _testimony_known_speakers(app)
        if status == "identified":
            speaker_name = _valid_person_name_suggestion(speaker_name, known_speakers)
            if not speaker_name:
                review_error = "Enter a speaker name before saving a speaker."
                if _wants_json_response():
                    return jsonify({"ok": False, "error": review_error}), 400
                return _redirect_to(
                    app,
                    "testimony_review",
                    status=current_filter,
                    sort=request.form.get("sort") or "shortest",
                    error=review_error,
                )
        if status == "grouped":
            speaker_name = ""
            testimony_title = group_title or "Testimonies"
        elif status == "message_review":
            speaker_name = ""
            testimony_title = group_title or "Message / Event Needs Review"
        elif status == "needs_review":
            testimony_title = ""
        else:
            testimony_title = _testimony_title_for_speaker(speaker_name)
        notes = str(existing["notes"] or "") if existing else ""
        duration_seconds = _row_duration(existing) if existing else None
        suggested_speaker = _valid_person_name_suggestion(str(existing["suggested_speaker"] or "") if existing else "", _testimony_known_speakers(app))
        suggestion_source = str(existing["suggestion_source"] or "") if existing else ""
        suggestion_text = str(existing["suggestion_text"] or "") if existing else ""
        transcript_text = _row_optional_text(existing, "transcript_text")
        transcript_source = _row_optional_text(existing, "transcript_source")
        transcript_error = _row_optional_text(existing, "transcript_error")
        proposed_path = ""
        save_message = "Recorder review saved."
        review_error = ""
        if status in {"identified", "grouped"}:
            proposed_path = _proposed_testimony_path(app, Path(candidate.path), service_date, speaker_name, testimony_title)
            renamed_candidate, proposed_path, rename_error = _rename_testimony_recording(
                app,
                candidate,
                service_date=service_date,
                speaker_name=speaker_name,
                testimony_title=testimony_title,
            )
            if renamed_candidate.id != recording_id:
                _delete_testimony_review(app, recording_id)
            candidate = renamed_candidate
            if not rename_error:
                _replace_indexed_recording(app, recording_id, candidate)
            recording_id = candidate.id
            if rename_error:
                status = "needs_review"
                review_error = rename_error
                save_message = f"Recorder review was not completed. {rename_error}"
            else:
                save_message = f"Recorder review saved and renamed to {Path(proposed_path).name}."
        _save_testimony_review(
            app,
            recording_id=recording_id,
            source_path=candidate.path,
            status=status,
            service_date=service_date,
            speaker_name=speaker_name,
            testimony_title=testimony_title,
            notes=notes,
            proposed_path=proposed_path,
            duration_seconds=duration_seconds,
            suggested_speaker=suggested_speaker,
            suggestion_source=suggestion_source,
            suggestion_text=suggestion_text,
        )
        if transcript_text or transcript_source or transcript_error:
            _save_testimony_transcript(
                app,
                recording_id,
                transcript_text=transcript_text,
                transcript_source=transcript_source,
                transcript_error=transcript_error,
            )
        if _wants_json_response():
            response = jsonify(
                {
                    "ok": not bool(review_error),
                    "message": save_message,
                    "error": review_error,
                    "recording_id": recording_id,
                    "previous_recording_id": original_recording_id,
                    "status": status,
                    "status_label": _testimony_status_label(status),
                    "service_date": service_date,
                    "service_date_label": _format_date(service_date),
                    "speaker_name": speaker_name,
                    "group_title": testimony_title if status in {"grouped", "message_review"} else "",
                    "title": candidate.title,
                    "source_label": Path(candidate.path).name,
                    "source_path": candidate.path,
                    "suggested_speaker": suggested_speaker,
                    "suggestion_source": suggestion_source,
                    "suggestion_source_label": _testimony_suggestion_source_label(suggestion_source),
                    "suggestion_text": suggestion_text,
                    "transcript_text": transcript_text,
                    "transcript_excerpt": _compact_transcript_excerpt(transcript_text, 900),
                    "transcript_preview": _testimony_display_transcript_preview(transcript_text, suggestion_source, suggestion_text, 900),
                    "transcript_error": transcript_error,
                    "transcript_updated_label": _format_datetime(_row_optional_text(existing, "transcript_updated_at")),
                    "audio_url": _recordings_url_for(app, "testimony_audio", recording_id=recording_id),
                    "review_url": _recordings_url_for(app, "update_testimony_review", recording_id=recording_id),
                }
            )
            return (response, 500) if review_error else response
        if review_error:
            return _redirect_to(
                app,
                "testimony_review",
                status=current_filter,
                sort=request.form.get("sort") or "shortest",
                error=save_message,
            )
        return _redirect_to(
            app,
            "testimony_review",
            status=current_filter,
            sort=request.form.get("sort") or "shortest",
            message=save_message,
        )

    @app.get("/admin/testimonies/audio/<recording_id>")
    def testimony_audio(recording_id: str):
        guard = _require_admin()
        if guard:
            return guard
        candidate = _testimony_source_recording_by_id(app, recording_id)
        if not candidate:
            row = _testimony_review_row(app, recording_id)
            if row:
                candidate = _testimony_candidate_from_review_row(app, row)
        if not candidate:
            return jsonify({"error": "recording was not found"}), 404
        path = Path(candidate.path)
        if not _path_allowed(app, path) or not path.exists() or not path.is_file():
            return jsonify({"error": "recording is unavailable"}), 404
        return send_file(path, as_attachment=False, conditional=True)

    @app.post("/admin/requests/<int:request_id>/send")
    def send_request_link(request_id: int):
        guard = _require_admin()
        if guard:
            return guard
        target_tab = _request_return_tab()
        row = _get_request(app, request_id)
        if not row:
            return _redirect_to(app, "admin_panel", tab=target_tab, error="Request was not found.")
        recording_id = (request.form.get("recording_id") or "").strip()
        candidate = _recording_target_by_id(app, recording_id)
        if not candidate:
            return _redirect_to(app, "admin_panel", tab=target_tab, error="Selected recording was not found.")
        email_message = _normalize_recording_email_message((request.form.get("email_message") or "").strip())
        if not email_message:
            email_message = _default_recording_email_message(row, candidate)
        token = row["share_token"] or secrets.token_urlsafe(22)
        share_url, share_provider, share_external_id, share_error = _create_share_link(app, candidate, token, existing_row=row)
        email_sent, email_error = _send_recording_email(app, row, candidate, share_url, email_message)
        status = "sent" if email_sent else "ready"
        combined_error = "; ".join(item for item in (share_error, email_error) if item)
        _mark_request_shared(
            app,
            request_id,
            candidate,
            token,
            share_url=share_url,
            share_provider=share_provider,
            share_external_id=share_external_id,
            status=status,
            email_error=combined_error,
            email_message=email_message,
        )
        if email_sent:
            return _redirect_to(app, "admin_panel", tab=target_tab, message=f"Recording link emailed to {row['email']}.")
        return _redirect_to(app, "admin_panel", tab=target_tab, message="Share link is ready.")

    @app.post("/admin/requests/<int:request_id>/revoke")
    def revoke_request_link(request_id: int):
        guard = _require_admin()
        if guard:
            return guard
        row = _get_request(app, request_id)
        if not row:
            return _redirect_to(app, "admin_panel", tab="pending", error="Request was not found.")
        target_tab = _request_return_tab(default="completed")
        revoke_error = _revoke_share_link(app, row)
        _mark_request_revoked(app, request_id, revoke_error=revoke_error)
        if revoke_error:
            return _redirect_to(app, "admin_panel", tab=target_tab, error=f"Request closed locally. Revoke warning: {revoke_error}")
        return _redirect_to(app, "admin_panel", tab=target_tab, message="Recording access revoked.")

    @app.post("/admin/requests/<int:request_id>/archive")
    def archive_request(request_id: int):
        guard = _require_admin()
        if guard:
            return guard
        row = _get_request(app, request_id)
        target_tab = _request_return_tab(default="completed")
        if not row:
            return _redirect_to(app, "admin_panel", tab=target_tab, error="Request was not found.")
        if row["status"] != "revoked":
            return _redirect_to(app, "admin_panel", tab=target_tab, error="Revoke access before archiving a request.")
        _archive_request(app, request_id)
        return _redirect_to(app, "admin_panel", tab=target_tab, message="Request archived.")

    @app.get("/share/<token>")
    def share_recording(token: str):
        row = _get_request_by_token(app, token)
        if not row or not row["recording_path"]:
            return render_template_string(RECORDING_SHARE_MISSING_TEMPLATE), 404
        shared_path = Path(row["recording_path"])
        return render_template_string(
            RECORDING_SHARE_TEMPLATE,
            title=row["recording_title"] or "Requested Recording",
            request_row=row,
            stream_url=_recordings_url_for(app, "stream_recording", token=token),
            is_folder=shared_path.exists() and shared_path.is_dir(),
            folder_items=_folder_audio_items(app, shared_path),
            format_date=_format_date,
        )

    @app.get("/share/<token>/stream")
    def stream_recording(token: str):
        row = _get_request_by_token(app, token)
        if not row or not row["recording_path"]:
            return jsonify({"error": "share link was not found"}), 404
        path = Path(row["recording_path"])
        if not _path_allowed(app, path) or not path.exists() or not path.is_file():
            return jsonify({"error": "recording file is unavailable"}), 404
        return send_file(path, as_attachment=False, conditional=True)

    @app.get("/share/<token>/download")
    def download_recording(token: str):
        return jsonify({"error": "recording downloads are disabled for shared links"}), 403

    return app


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, factory=ClosingSQLiteConnection)
    connection.row_factory = sqlite3.Row
    return connection


def _init_db(db_path: str) -> None:
    with _connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS recording_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                requester_name TEXT NOT NULL,
                email TEXT NOT NULL,
                secondary_email TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                recording_kind TEXT NOT NULL DEFAULT 'message',
                requested_date TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                recording_id TEXT NOT NULL DEFAULT '',
                recording_path TEXT NOT NULL DEFAULT '',
                recording_title TEXT NOT NULL DEFAULT '',
                share_url TEXT NOT NULL DEFAULT '',
                share_provider TEXT NOT NULL DEFAULT 'internal',
                share_external_id TEXT NOT NULL DEFAULT '',
                share_token TEXT UNIQUE,
                sent_at TEXT,
                revoked_at TEXT,
                archived_at TEXT,
                email_error TEXT NOT NULL DEFAULT '',
                email_message TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_recording_requests_status ON recording_requests(status)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_recording_requests_date ON recording_requests(requested_date)")
        _ensure_column(connection, "recording_requests", "secondary_email", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "recording_requests", "recording_kind", "TEXT NOT NULL DEFAULT 'message'")
        _ensure_column(connection, "recording_requests", "share_url", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "recording_requests", "share_provider", "TEXT NOT NULL DEFAULT 'internal'")
        _ensure_column(connection, "recording_requests", "share_external_id", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "recording_requests", "revoked_at", "TEXT")
        _ensure_column(connection, "recording_requests", "archived_at", "TEXT")
        _ensure_column(connection, "recording_requests", "email_message", "TEXT NOT NULL DEFAULT ''")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS recording_library (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                recording_date TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'unsure',
                size_bytes INTEGER NOT NULL DEFAULT 0,
                modified_at TEXT NOT NULL DEFAULT '',
                relative_path TEXT NOT NULL DEFAULT '',
                root_path TEXT NOT NULL DEFAULT '',
                indexed_at TEXT NOT NULL
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_recording_library_date ON recording_library(recording_date)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_recording_library_kind ON recording_library(kind)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_recording_library_path ON recording_library(path)")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS recording_library_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS testimony_reviews (
                recording_id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'needs_review',
                service_date TEXT NOT NULL DEFAULT '',
                speaker_name TEXT NOT NULL DEFAULT '',
                testimony_title TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                proposed_path TEXT NOT NULL DEFAULT '',
                duration_seconds REAL,
                suggested_speaker TEXT NOT NULL DEFAULT '',
                suggestion_source TEXT NOT NULL DEFAULT '',
                suggestion_text TEXT NOT NULL DEFAULT '',
                suggestion_updated_at TEXT NOT NULL DEFAULT '',
                transcript_text TEXT NOT NULL DEFAULT '',
                transcript_source TEXT NOT NULL DEFAULT '',
                transcript_error TEXT NOT NULL DEFAULT '',
                transcript_updated_at TEXT NOT NULL DEFAULT '',
                quarantined_from_path TEXT NOT NULL DEFAULT '',
                quarantined_path TEXT NOT NULL DEFAULT '',
                quarantined_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        _ensure_column(connection, "testimony_reviews", "suggested_speaker", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "testimony_reviews", "suggestion_source", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "testimony_reviews", "suggestion_text", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "testimony_reviews", "suggestion_updated_at", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "testimony_reviews", "transcript_text", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "testimony_reviews", "transcript_source", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "testimony_reviews", "transcript_error", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "testimony_reviews", "transcript_updated_at", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "testimony_reviews", "quarantined_from_path", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "testimony_reviews", "quarantined_path", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "testimony_reviews", "quarantined_at", "TEXT NOT NULL DEFAULT ''")
        connection.execute(
            """
            UPDATE testimony_reviews
            SET status = 'message_review',
                testimony_title = CASE
                    WHEN COALESCE(testimony_title, '') = '' THEN 'Message / Event Needs Review'
                    ELSE testimony_title
                END,
                updated_at = ?
            WHERE status = 'not_testimony'
              AND LOWER(COALESCE(suggestion_text, '')) LIKE '%likely message recording%'
            """,
            (_utc_now(),),
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_testimony_reviews_status ON testimony_reviews(status)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_testimony_reviews_source_path ON testimony_reviews(source_path)")


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _insert_request(
    app: Flask,
    *,
    requester_name: str,
    email: str,
    secondary_email: str,
    phone: str,
    recording_kind: str,
    requested_date: str,
    notes: str,
    candidate: RecordingCandidate | None = None,
) -> int:
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        cursor = connection.execute(
            """
            INSERT INTO recording_requests (
                created_at,
                requester_name,
                email,
                secondary_email,
                phone,
                recording_kind,
                requested_date,
                notes,
                recording_id,
                recording_path,
                recording_title
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_now(),
                requester_name,
                email,
                secondary_email,
                phone,
                recording_kind,
                requested_date,
                notes,
                candidate.id if candidate else "",
                candidate.path if candidate else "",
                candidate.title if candidate else "",
            ),
        )
        return int(cursor.lastrowid)


def _list_requests(app: Flask) -> list[sqlite3.Row]:
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        return list(
            connection.execute(
                """
                SELECT *
                FROM recording_requests
                ORDER BY
                    CASE WHEN archived_at IS NULL THEN 0 ELSE 1 END,
                    CASE status WHEN 'pending' THEN 0 WHEN 'ready' THEN 1 WHEN 'sent' THEN 2 WHEN 'revoked' THEN 3 ELSE 4 END,
                    created_at DESC
                LIMIT 200
                """
            )
        )


def _status_label(status: str) -> str:
    labels = {
        "pending": "Pending",
        "ready": "Link Ready",
        "sent": "Sent",
        "revoked": "Revoked",
    }
    return labels.get(status, status.replace("_", " ").title())


def _wants_json_response() -> bool:
    accept = request.headers.get("Accept", "")
    return request.headers.get("X-Requested-With") == "fetch" or "application/json" in accept


def _recordings_url_for(app: Flask, endpoint: str, **values) -> str:
    generated = url_for(endpoint, **values)
    if endpoint == "testimony_review" and generated.startswith("/admin/testimonies"):
        generated = f"/admin/recorder-review{generated[len('/admin/testimonies'):]}"
    if values.get("_external") or not generated.startswith("/") or re.match(r"^[a-z][a-z0-9+.-]*://", generated, flags=re.IGNORECASE):
        return generated
    prefix = _public_mount_prefix(app)
    if not prefix:
        return generated
    if generated == prefix or generated.startswith(f"{prefix}/"):
        return generated
    if generated == "/":
        return f"{prefix}/"
    return f"{prefix}{generated}"


def _public_mount_prefix(app: Flask) -> str:
    explicit = _normalize_mount_prefix(app.config.get("NTC_RECORDINGS_PUBLIC_PREFIX"))
    if explicit:
        return explicit
    if has_request_context():
        forwarded = _normalize_mount_prefix(request.headers.get("X-Forwarded-Prefix", ""))
        if forwarded:
            return forwarded
        script_root = _normalize_mount_prefix(request.script_root or request.environ.get("SCRIPT_NAME", ""))
        if script_root:
            return script_root
    base_url = str(app.config.get("NTC_RECORDINGS_PUBLIC_BASE_URL") or "").strip()
    parsed = urlparse(base_url)
    base_prefix = _normalize_mount_prefix(parsed.path)
    if not base_prefix:
        return ""
    if not has_request_context() or not parsed.hostname:
        return base_prefix
    request_hostname = (request.host or "").split(":", 1)[0].strip("[]").lower()
    forwarded_hostname = (request.headers.get("X-Forwarded-Host") or "").split(",", 1)[0].split(":", 1)[0].strip("[]").lower()
    public_hostname = parsed.hostname.lower()
    if request_hostname == public_hostname or forwarded_hostname == public_hostname:
        return base_prefix
    return ""


def _normalize_mount_prefix(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        raw = urlparse(raw).path
    raw = "/" + raw.strip("/")
    return "" if raw == "/" else raw


def _redirect_to(app: Flask, endpoint: str, **values):
    return redirect(_recordings_url_for(app, endpoint, **values))


def _get_request(app: Flask, request_id: int) -> sqlite3.Row | None:
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        return connection.execute("SELECT * FROM recording_requests WHERE id = ?", (request_id,)).fetchone()


def _get_request_by_token(app: Flask, token: str) -> sqlite3.Row | None:
    if not token or len(token) > 128:
        return None
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        return connection.execute(
            "SELECT * FROM recording_requests WHERE share_token = ? AND status != 'revoked'",
            (token,),
        ).fetchone()


def _mark_request_shared(
    app: Flask,
    request_id: int,
    candidate: RecordingCandidate,
    token: str,
    *,
    share_url: str,
    share_provider: str,
    share_external_id: str,
    status: str,
    email_error: str,
    email_message: str,
) -> None:
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        connection.execute(
            """
            UPDATE recording_requests
            SET status = ?,
                recording_id = ?,
                recording_path = ?,
                recording_title = ?,
                share_url = ?,
                share_provider = ?,
                share_external_id = ?,
                share_token = ?,
                sent_at = ?,
                revoked_at = NULL,
                archived_at = NULL,
                email_error = ?,
                email_message = ?
            WHERE id = ?
            """,
            (
                status,
                candidate.id,
                candidate.path,
                candidate.title,
                share_url,
                share_provider,
                share_external_id,
                token,
                _utc_now() if status == "sent" else None,
                email_error,
                email_message,
                request_id,
            ),
        )


def _mark_request_revoked(app: Flask, request_id: int, *, revoke_error: str = "") -> None:
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        connection.execute(
            """
            UPDATE recording_requests
            SET status = 'revoked',
                share_url = '',
                share_external_id = '',
                share_token = NULL,
                email_error = ?,
                revoked_at = ?,
                archived_at = NULL,
                sent_at = sent_at
            WHERE id = ?
            """,
            (revoke_error, _utc_now(), request_id),
        )


def _archive_request(app: Flask, request_id: int) -> None:
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        connection.execute(
            """
            UPDATE recording_requests
            SET archived_at = ?
            WHERE id = ? AND status = 'revoked'
            """,
            (_utc_now(), request_id),
        )


def _auto_archive_closed_requests(app: Flask) -> int:
    days = int(app.config.get("NTC_RECORDINGS_AUTO_ARCHIVE_DAYS") or 0)
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff.isoformat(timespec="seconds")
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        cursor = connection.execute(
            """
            UPDATE recording_requests
            SET archived_at = ?
            WHERE archived_at IS NULL
              AND status = 'revoked'
              AND COALESCE(revoked_at, created_at) <= ?
            """,
            (_utc_now(), cutoff_iso),
        )
        return int(cursor.rowcount or 0)


def _library_dirs(app: Flask) -> list[Path]:
    return [root for _, root in _library_roots(app)]


def _library_roots(app: Flask) -> list[tuple[str, Path]]:
    raw = str(app.config.get("NTC_RECORDINGS_LIBRARY_DIRS") or DEFAULT_RECORDING_DIR)
    roots: list[tuple[str, Path]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        kind = ""
        path_text = item
        if ":" in item and not item.startswith("/"):
            maybe_kind, maybe_path = item.split(":", 1)
            if maybe_path.strip():
                kind = _normalize_recording_kind(maybe_kind)
                path_text = maybe_path.strip()
        path = Path(path_text)
        roots.append((kind or _recording_kind_for_path(path), path))
    return roots


def _path_allowed(app: Flask, path: Path) -> bool:
    try:
        resolved = path.resolve()
    except FileNotFoundError:
        resolved = path.absolute()
    allowed_roots = _library_dirs(app) + [_testimony_rejected_root(app)]
    for root in allowed_roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except (FileNotFoundError, ValueError):
            continue
    return False


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (FileNotFoundError, ValueError):
        return False


def _folder_audio_items(app: Flask, path: Path) -> list[str]:
    if not _path_allowed(app, path) or not path.exists() or not path.is_dir():
        return []
    items = []
    for item in path.rglob("*"):
        if not item.is_file() or item.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        if item.name.startswith("._") or any(part.startswith(".") for part in item.parts):
            continue
        if any(part in LIBRARY_EXCLUDED_DIR_NAMES for part in item.parts):
            continue
        try:
            items.append(str(item.relative_to(path)))
        except ValueError:
            items.append(item.name)
    return sorted(items)


def _scan_recordings(app: Flask) -> list[RecordingCandidate]:
    recordings = []
    max_files = int(app.config.get("NTC_RECORDINGS_MAX_SCAN_FILES") or 4000)
    for root_kind, root in _library_roots(app):
        if not root.exists() or not root.is_dir():
            continue
        for path in root.rglob("*"):
            if len(recordings) >= max_files:
                break
            if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            if path.name.startswith("._") or any(part.startswith(".") for part in path.parts):
                continue
            if any(part in LIBRARY_EXCLUDED_DIR_NAMES for part in path.parts):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            recording_date = _extract_recording_date(" ".join(path.parts)) or ""
            try:
                relative_path = str(path.relative_to(root))
            except ValueError:
                relative_path = path.name
            path_kind = _recording_kind_for_path(path)
            recordings.append(
                RecordingCandidate(
                    id=_recording_id(path),
                    path=str(path),
                    title=_display_title(path),
                    recording_date=recording_date,
                    kind=path_kind if path_kind != "unsure" else root_kind,
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                    relative_path=relative_path,
                )
            )
    recordings.sort(key=lambda item: (item.recording_date or "0000-00-00", item.modified_at, item.title), reverse=True)
    return recordings


def _get_recordings(app: Flask) -> list[RecordingCandidate]:
    if _recording_index_needs_refresh(app):
        with app.recordings_index_lock:
            if _recording_index_needs_refresh(app):
                try:
                    _refresh_recording_index(app)
                except Exception:  # pragma: no cover - keep serving the last good index at runtime
                    app.logger.exception("failed to refresh recording library index")
    return _load_indexed_recordings(app)


def _recording_by_id(app: Flask, recording_id: str) -> RecordingCandidate | None:
    if not recording_id:
        return None
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        row = connection.execute(
            """
            SELECT id, path, title, recording_date, kind, size_bytes, modified_at, relative_path
            FROM recording_library
            WHERE id = ?
            """,
            (recording_id,),
        ).fetchone()
    if row:
        return _recording_candidate_from_row(row)
    _get_recordings(app)
    return next((item for item in _load_indexed_recordings(app) if item.id == recording_id), None)


def _recording_index_needs_refresh(app: Flask) -> bool:
    refresh_seconds = float(app.config.get("NTC_RECORDINGS_INDEX_REFRESH_SECONDS") or 0)
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        row_count = connection.execute("SELECT COUNT(*) AS count FROM recording_library").fetchone()["count"]
        if row_count <= 0:
            return True
        if refresh_seconds <= 0:
            return False
        refreshed_at = _recording_library_meta(connection, "last_refresh_finished")
    if not refreshed_at:
        return True
    try:
        refreshed = datetime.fromisoformat(refreshed_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if refreshed.tzinfo is None:
        refreshed = refreshed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - refreshed >= timedelta(seconds=refresh_seconds)


def _refresh_recording_index(app: Flask) -> int:
    recordings = _scan_recordings(app)
    indexed_at = _utc_now()
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        connection.execute(
            """
            INSERT INTO recording_library_meta (key, value)
            VALUES ('last_refresh_started', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (indexed_at,),
        )
        connection.execute("DELETE FROM recording_library")
        connection.executemany(
            """
            INSERT INTO recording_library (
                id,
                path,
                title,
                recording_date,
                kind,
                size_bytes,
                modified_at,
                relative_path,
                root_path,
                indexed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    recording.id,
                    recording.path,
                    recording.title,
                    recording.recording_date,
                    recording.kind,
                    recording.size_bytes,
                    recording.modified_at,
                    recording.relative_path,
                    _matched_library_root(app, Path(recording.path)),
                    indexed_at,
                )
                for recording in recordings
            ],
        )
        connection.execute(
            """
            INSERT INTO recording_library_meta (key, value)
            VALUES ('last_refresh_finished', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (_utc_now(),),
        )
        connection.execute(
            """
            INSERT INTO recording_library_meta (key, value)
            VALUES ('recording_count', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(len(recordings)),),
        )
    app.logger.info("recording library index refreshed with %s files", len(recordings))
    return len(recordings)


def _load_indexed_recordings(app: Flask) -> list[RecordingCandidate]:
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        rows = connection.execute(
            """
            SELECT id, path, title, recording_date, kind, size_bytes, modified_at, relative_path
            FROM recording_library
            ORDER BY COALESCE(NULLIF(recording_date, ''), '0000-00-00') DESC,
                     modified_at DESC,
                     title ASC
            """
        ).fetchall()
    return [_recording_candidate_from_row(row) for row in rows]


def _replace_indexed_recording(app: Flask, old_recording_id: str, recording: RecordingCandidate) -> None:
    indexed_at = _utc_now()
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        if old_recording_id and old_recording_id != recording.id:
            connection.execute("DELETE FROM recording_library WHERE id = ?", (old_recording_id,))
        connection.execute(
            """
            INSERT INTO recording_library (
                id,
                path,
                title,
                recording_date,
                kind,
                size_bytes,
                modified_at,
                relative_path,
                root_path,
                indexed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                path = excluded.path,
                title = excluded.title,
                recording_date = excluded.recording_date,
                kind = excluded.kind,
                size_bytes = excluded.size_bytes,
                modified_at = excluded.modified_at,
                relative_path = excluded.relative_path,
                root_path = excluded.root_path,
                indexed_at = excluded.indexed_at
            """,
            (
                recording.id,
                recording.path,
                recording.title,
                recording.recording_date,
                recording.kind,
                recording.size_bytes,
                recording.modified_at,
                recording.relative_path,
                _matched_library_root(app, Path(recording.path)),
                indexed_at,
            ),
        )


def _recording_candidate_from_row(row: sqlite3.Row) -> RecordingCandidate:
    return RecordingCandidate(
        id=row["id"],
        path=row["path"],
        title=row["title"],
        recording_date=row["recording_date"],
        kind=row["kind"],
        size_bytes=int(row["size_bytes"] or 0),
        modified_at=row["modified_at"],
        relative_path=row["relative_path"],
    )


def _recording_library_meta(connection: sqlite3.Connection, key: str) -> str:
    row = connection.execute("SELECT value FROM recording_library_meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else ""


def _matched_library_root(app: Flask, path: Path) -> str:
    try:
        resolved = path.resolve()
    except FileNotFoundError:
        resolved = path.absolute()
    matches = []
    for _, root in _library_roots(app):
        try:
            root_resolved = root.resolve()
            resolved.relative_to(root_resolved)
        except (FileNotFoundError, ValueError):
            continue
        matches.append(root_resolved)
    if not matches:
        return ""
    matches.sort(key=lambda item: len(str(item)), reverse=True)
    return str(matches[0])


def _public_recording_date_options(recordings: Iterable[RecordingCandidate]) -> list[dict]:
    counts: dict[str, int] = {}
    counts_by_kind: dict[str, dict[str, int]] = {}
    kinds: dict[str, set[str]] = {}
    for recording in recordings:
        if recording.recording_date:
            counts[recording.recording_date] = counts.get(recording.recording_date, 0) + 1
            counts_by_kind.setdefault(recording.recording_date, {})
            counts_by_kind[recording.recording_date][recording.kind] = (
                counts_by_kind[recording.recording_date].get(recording.kind, 0) + 1
            )
            kinds.setdefault(recording.recording_date, set()).add(recording.kind)
    return [
        {
            "date": recording_date,
            "label": _format_date(recording_date),
            "count": counts[recording_date],
            "counts_by_kind": counts_by_kind.get(recording_date, {}),
            "kinds": sorted(kinds.get(recording_date, set())),
        }
        for recording_date in sorted(counts, reverse=True)
    ]


def _recording_counts_by_kind(recordings: Iterable[RecordingCandidate]) -> dict[str, int]:
    counts = {"message": 0, "worship": 0, "testimony": 0, "unsure": 0}
    for recording in recordings:
        kind = recording.kind if recording.kind in counts else "unsure"
        counts[kind] += 1
    return counts


def _default_candidate_for_date(
    app: Flask,
    recordings: Iterable[RecordingCandidate],
    requested_date: str,
    recording_kind: str,
) -> RecordingCandidate | None:
    options = _target_options_for_date(app, recordings, requested_date, recording_kind)
    return options[0] if options else None


def _candidate_options_for_request(app: Flask, recordings: Iterable[RecordingCandidate], row: sqlite3.Row) -> list[RecordingCandidate]:
    recordings_list = list(recordings)
    selected = _target_from_recordings(app, recordings_list, row["recording_id"])
    requested_kind = _normalize_recording_kind(row["recording_kind"] if "recording_kind" in row.keys() else "")
    same_date = [
        item
        for item in _target_options_for_date(app, recordings_list, row["requested_date"], requested_kind)
        if item.id != row["recording_id"]
    ]
    ordered = []
    if selected:
        ordered.append(selected)
    ordered.extend(same_date)
    return ordered[:12]


def _target_options_for_date(
    app: Flask,
    recordings: Iterable[RecordingCandidate],
    requested_date: str,
    recording_kind: str,
) -> list[RecordingCandidate]:
    recordings_list = [item for item in recordings if item.recording_date == requested_date]
    requested_kind = _normalize_recording_kind(recording_kind)
    if requested_kind == "worship":
        return _worship_collection_options(app, recordings_list)
    if requested_kind in {"message", "testimony"}:
        return [item for item in recordings_list if item.kind == requested_kind]

    message_options = [item for item in recordings_list if item.kind == "message"]
    worship_options = _worship_collection_options(app, recordings_list)
    testimony_options = [item for item in recordings_list if item.kind == "testimony"]
    unsure_options = [item for item in recordings_list if item.kind not in {"message", "worship", "testimony"}]
    return [*message_options, *worship_options, *testimony_options, *unsure_options]


def _worship_collection_options(app: Flask, recordings: Iterable[RecordingCandidate]) -> list[RecordingCandidate]:
    grouped: dict[Path, list[RecordingCandidate]] = {}
    for recording in recordings:
        if recording.kind != "worship":
            continue
        grouped.setdefault(_worship_collection_path(app, recording), []).append(recording)

    collections = []
    for collection_path, files in grouped.items():
        files.sort(key=lambda item: (item.modified_at, item.title), reverse=True)
        root_path = _matched_library_root(app, collection_path)
        try:
            relative_path = str(collection_path.relative_to(Path(root_path))) if root_path else collection_path.name
        except ValueError:
            relative_path = collection_path.name
        collections.append(
            RecordingCandidate(
                id=_collection_id(collection_path),
                path=str(collection_path),
                title=collection_path.name,
                recording_date=files[0].recording_date,
                kind="worship",
                size_bytes=sum(item.size_bytes for item in files),
                modified_at=max(item.modified_at for item in files),
                relative_path=relative_path,
                target_type="folder",
                file_count=len(files),
            )
        )
    collections.sort(key=lambda item: (item.recording_date, item.modified_at, item.title), reverse=True)
    return collections


def _worship_collection_path(app: Flask, recording: RecordingCandidate) -> Path:
    path = Path(recording.path)
    root_path = _matched_library_root(app, path)
    root = Path(root_path) if root_path else None
    for parent in path.parents:
        if root and parent == root:
            break
        if _extract_recording_date(parent.name) == recording.recording_date:
            return parent
    return path.parent


def _target_from_recordings(app: Flask, recordings: Iterable[RecordingCandidate], recording_id: str) -> RecordingCandidate | None:
    if not recording_id:
        return None
    recordings_list = list(recordings)
    selected = next((item for item in recordings_list if item.id == recording_id), None)
    if selected:
        return selected
    return next((item for item in _all_recording_targets(app, recordings_list) if item.id == recording_id), None)


def _recording_target_by_id(app: Flask, recording_id: str) -> RecordingCandidate | None:
    selected = _recording_by_id(app, recording_id)
    if selected:
        return selected
    recordings = _get_recordings(app)
    return _target_from_recordings(app, recordings, recording_id)


def _all_recording_targets(app: Flask, recordings: Iterable[RecordingCandidate]) -> list[RecordingCandidate]:
    recordings_list = list(recordings)
    file_targets = [item for item in recordings_list if item.kind != "worship"]
    return [*file_targets, *_worship_collection_options(app, recordings_list)]


def _candidate_groups(candidates: Iterable[RecordingCandidate]) -> list[dict]:
    order = {"message": 0, "worship": 1, "testimony": 2, "unsure": 3}
    grouped: dict[str, list[RecordingCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.kind, []).append(candidate)
    return [
        {"kind": kind, "label": _recording_kind_label(kind), "options": grouped[kind]}
        for kind in sorted(grouped, key=lambda item: order.get(item, 99))
    ]


def _request_groups(requests: Iterable[sqlite3.Row]) -> list[dict]:
    order = {"message": 0, "worship": 1, "testimony": 2, "unsure": 3}
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in requests:
        kind = _normalize_recording_kind(row["recording_kind"] if "recording_kind" in row.keys() else "")
        grouped.setdefault(kind, []).append(row)
    return [
        {
            "kind": kind,
            "label": _request_group_label(kind),
            "requests": grouped[kind],
        }
        for kind in sorted(grouped, key=lambda item: order.get(item, 99))
    ]


def _request_group_label(kind: str) -> str:
    labels = {
        "message": "Message Requests",
        "worship": "Worship Requests",
        "testimony": "Testimony Requests",
        "unsure": "Not Sure Requests",
    }
    return labels.get(kind, "Recording Requests")


def _request_return_tab(default: str = "pending") -> str:
    target_tab = (request.form.get("tab") or default).strip().lower()
    if target_tab in {"active", "closed", "archived"}:
        return "completed"
    if target_tab not in {"pending", "completed"}:
        return default if default in {"pending", "completed"} else "pending"
    return target_tab


def _testimony_source_root(app: Flask) -> Path:
    default_source = str(Path(DEFAULT_MESSAGE_RECORDING_DIR) / "DN300R")
    configured_value = app.config.get("NTC_RECORDINGS_TESTIMONY_SOURCE_DIR") or ""
    legacy_value = app.config.get("NTC_RECORDINGS_DN300R_DIR") or ""
    if legacy_value and (not configured_value or configured_value == default_source):
        configured_value = legacy_value
    configured = Path(str(configured_value)).expanduser()
    if configured.exists():
        return configured
    message_root = _message_recording_root(app)
    for folder_name in ("DN300R", "DM300R"):
        candidate = message_root / folder_name
        if candidate.exists():
            return candidate
    return configured


def _message_recording_root(app: Flask) -> Path:
    roots = _library_roots(app)
    for kind, root in roots:
        if kind == "message":
            return root
    for _, root in roots:
        if "messagerecordings" in re.sub(r"[^a-z]+", "", str(root).lower()):
            return root
    return Path(DEFAULT_MESSAGE_RECORDING_DIR)


def _testimony_recording_root(app: Flask) -> Path:
    configured = Path(str(app.config.get("NTC_RECORDINGS_TESTIMONY_LIBRARY_DIR") or DEFAULT_TESTIMONY_RECORDING_DIR))
    roots = _library_roots(app)
    for kind, root in roots:
        if kind == "testimony":
            return root
    for _, root in roots:
        if "testimonyrecordings" in re.sub(r"[^a-z]+", "", str(root).lower()):
            return root
    return configured


def _testimony_rejected_root(app: Flask) -> Path:
    return Path(str(app.config.get("NTC_RECORDINGS_TESTIMONY_REJECTED_DIR") or DEFAULT_TESTIMONY_REJECTED_DIR)).expanduser()


def _relative_to_first_root(path: Path, roots: Iterable[Path]) -> str:
    for root in roots:
        try:
            return str(path.relative_to(root))
        except ValueError:
            continue
    raise ValueError(f"{path} is outside known roots")


def _testimony_source_candidates(app: Flask) -> list[RecordingCandidate]:
    root = _testimony_source_root(app)
    if not root.exists() or not root.is_dir():
        return []
    known_roots = [_testimony_recording_root(app), _message_recording_root(app), root, _testimony_rejected_root(app)]
    candidates = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        if path.name.startswith("._") or any(part.startswith(".") for part in path.parts):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        recording_date = _extract_recording_date(" ".join(path.parts)) or _date_from_file_metadata(stat) or ""
        try:
            relative_path = _relative_to_first_root(path, known_roots)
        except ValueError:
            relative_path = path.name
        kind = "testimony" if _raw_testimony_name(path) else "message"
        candidates.append(
            RecordingCandidate(
                id=_recording_id(path),
                path=str(path),
                title=_display_title(path),
                recording_date=recording_date,
                kind=kind,
                size_bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                relative_path=relative_path,
            )
        )
    candidates.sort(key=lambda item: (item.recording_date or "0000-00-00", item.modified_at, item.title), reverse=True)
    return candidates


def _testimony_source_candidate_from_path(app: Flask, path: Path) -> RecordingCandidate | None:
    root = _testimony_source_root(app)
    message_root = _message_recording_root(app)
    testimony_root = _testimony_recording_root(app)
    rejected_root = _testimony_rejected_root(app)
    if not (
        _path_within(path, root)
        or _path_within(path, message_root)
        or _path_within(path, testimony_root)
        or _path_within(path, rejected_root)
    ):
        return None
    if not path.exists() or not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
        return None
    if path.name.startswith("._") or any(part.startswith(".") for part in path.parts):
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    recording_date = _extract_recording_date(" ".join(path.parts)) or _date_from_file_metadata(stat) or ""
    try:
        relative_path = _relative_to_first_root(path, [testimony_root, message_root, root, rejected_root])
    except ValueError:
        relative_path = path.name
    kind = "testimony" if _raw_testimony_name(path) else "message"
    return RecordingCandidate(
        id=_recording_id(path),
        path=str(path),
        title=_display_title(path),
        recording_date=recording_date,
        kind=kind,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
        relative_path=relative_path,
    )


def _testimony_source_recording_by_id(app: Flask, recording_id: str) -> RecordingCandidate | None:
    if not recording_id:
        return None
    return next((item for item in _testimony_source_candidates(app) if item.id == recording_id), None)


def _testimony_source_recording_from_form(app: Flask, recording_id: str) -> RecordingCandidate | None:
    form_path = (request.form.get("source_path") or "").strip()
    row = _testimony_review_row(app, recording_id)
    if form_path:
        candidate = _testimony_source_candidate_from_path(app, Path(form_path))
        if candidate and candidate.id == recording_id:
            return candidate
        if candidate and row:
            row_paths = {str(row["source_path"] or ""), str(row["proposed_path"] or "")}
            if candidate.path in row_paths:
                return candidate
    if row:
        candidate = _testimony_candidate_from_review_row(app, row)
        if candidate:
            return candidate
    return _testimony_source_recording_by_id(app, recording_id)


def _raw_testimony_name(path: Path) -> bool:
    normalized = re.sub(r"[^a-z]+", "", str(path).lower())
    return "testimony" in normalized or "testimonies" in normalized


def _raw_recorder_name(path: Path) -> bool:
    return bool(re.fullmatch(r"(?i)rec\d+", _strip_audio_extensions(path.name).strip()))


def _named_non_testimony_recording(candidate: RecordingCandidate) -> bool:
    path = Path(candidate.path)
    if _raw_testimony_name(path) or _raw_recorder_name(path):
        return False
    return candidate.kind != "testimony"


def _testimony_review_row(app: Flask, recording_id: str) -> sqlite3.Row | None:
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        return connection.execute("SELECT * FROM testimony_reviews WHERE recording_id = ?", (recording_id,)).fetchone()


def _testimony_review_rows(app: Flask) -> dict[str, sqlite3.Row]:
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        rows = connection.execute("SELECT * FROM testimony_reviews").fetchall()
    return {row["recording_id"]: row for row in rows}


def _testimony_candidate_from_review_row(app: Flask, row: sqlite3.Row) -> RecordingCandidate | None:
    for key in ("source_path", "proposed_path"):
        value = str(row[key] or "").strip()
        if not value:
            continue
        candidate = _testimony_source_candidate_from_path(app, Path(value))
        if candidate:
            return candidate
    return None


def _testimony_review_item(app: Flask, candidate: RecordingCandidate, row: sqlite3.Row | None, known_speakers: Iterable[str]) -> dict:
    duration_seconds = _row_duration(row) if row else None
    status = _testimony_status_for_candidate(app, candidate, row, duration_seconds)
    service_date = str(row["service_date"] or "") if row else ""
    speaker_name = str(row["speaker_name"] or "") if row else ""
    testimony_title = str(row["testimony_title"] or "") if row else ""
    notes = str(row["notes"] or "") if row else ""
    proposed_path = str(row["proposed_path"] or "") if row else ""
    suggested_speaker = _valid_person_name_suggestion(str(row["suggested_speaker"] or "") if row else "", known_speakers)
    suggestion_source = str(row["suggestion_source"] or "") if row else ""
    suggestion_text = str(row["suggestion_text"] or "") if row else ""
    transcript_text = _row_optional_text(row, "transcript_text")
    transcript_source = _row_optional_text(row, "transcript_source")
    transcript_error = _row_optional_text(row, "transcript_error")
    transcript_updated_at = _row_optional_text(row, "transcript_updated_at")
    quarantined_from_path = _row_optional_text(row, "quarantined_from_path")
    quarantined_path = _row_optional_text(row, "quarantined_path")
    quarantined_at = _row_optional_text(row, "quarantined_at")
    if not suggested_speaker and not speaker_name:
        suggested_speaker = _testimony_filename_speaker_suggestion(Path(candidate.path))
        if suggested_speaker:
            suggestion_source = "filename"
            suggestion_text = Path(candidate.path).name
    if not service_date:
        service_date = candidate.recording_date
    event_folder = _testimony_event_folder(service_date)
    if not proposed_path and status in {"identified", "grouped"}:
        proposed_path = _proposed_testimony_path(
            app,
            Path(candidate.path),
            service_date,
            speaker_name,
            testimony_title,
        )
    display_id = str(row["recording_id"] or "") if row else candidate.id
    if not display_id:
        display_id = candidate.id
    return {
        "id": display_id,
        "title": candidate.title,
        "source_path": candidate.path,
        "source_label": Path(candidate.path).name,
        "relative_path": candidate.relative_path,
        "recording_date": candidate.recording_date,
        "service_date": service_date,
        "speaker_name": speaker_name,
        "testimony_title": testimony_title,
        "group_title": testimony_title if status in {"grouped", "message_review"} else "",
        "event_group": event_folder[1] if event_folder else "",
        "notes": notes,
        "proposed_path": proposed_path,
        "suggested_speaker": suggested_speaker,
        "suggestion_source": suggestion_source,
        "suggestion_source_label": _testimony_suggestion_source_label(suggestion_source),
        "suggestion_text": suggestion_text,
        "transcript_text": transcript_text,
        "transcript_excerpt": _compact_transcript_excerpt(transcript_text, 900),
        "transcript_preview": _testimony_display_transcript_preview(transcript_text, suggestion_source, suggestion_text, 900),
        "transcript_source": transcript_source,
        "transcript_source_label": _testimony_transcript_source_label(transcript_source),
        "transcript_error": transcript_error,
        "transcript_updated_at": transcript_updated_at,
        "transcript_updated_label": _format_datetime(transcript_updated_at),
        "quarantined": bool(quarantined_path or quarantined_at),
        "quarantined_from_path": quarantined_from_path,
        "quarantined_path": quarantined_path,
        "quarantined_at": quarantined_at,
        "quarantined_label": _format_datetime(quarantined_at),
        "duration_seconds": duration_seconds,
        "duration_label": _format_duration(duration_seconds),
        "size_label": _human_size(candidate.size_bytes),
        "modified_at": candidate.modified_at,
        "modified_label": _format_datetime(candidate.modified_at),
        "status": status,
        "status_label": _testimony_status_label(status),
        "audio_url": _recordings_url_for(app, "testimony_audio", recording_id=display_id),
        "extension": Path(candidate.path).suffix.lower(),
    }


def _testimony_review_row_is_quarantined(row: sqlite3.Row) -> bool:
    status = str(row["status"] or "")
    return status in {"duplicate", "not_testimony"} and bool(_row_optional_text(row, "quarantined_path"))


def _testimony_review_items(app: Flask) -> list[dict]:
    rows = _testimony_review_rows(app)
    known_speakers = _testimony_known_speakers(app)
    items = []
    seen_row_ids = set()
    seen_paths = set()
    for candidate in _testimony_source_candidates(app):
        row = rows.get(candidate.id)
        if row and _testimony_review_row_is_quarantined(row):
            seen_row_ids.add(str(row["recording_id"]))
            seen_paths.add(candidate.path)
            continue
        if row:
            seen_row_ids.add(str(row["recording_id"]))
        seen_paths.add(candidate.path)
        items.append(_testimony_review_item(app, candidate, row, known_speakers))

    for row_id, row in rows.items():
        if row_id in seen_row_ids:
            continue
        if _testimony_review_row_is_quarantined(row):
            continue
        candidate = _testimony_candidate_from_review_row(app, row)
        if not candidate or candidate.path in seen_paths:
            continue
        seen_row_ids.add(row_id)
        seen_paths.add(candidate.path)
        items.append(_testimony_review_item(app, candidate, row, known_speakers))

    return items


def _row_duration(row: sqlite3.Row | None) -> float | None:
    if not row or row["duration_seconds"] is None:
        return None
    try:
        return float(row["duration_seconds"])
    except (TypeError, ValueError):
        return None


def _row_optional_text(row: sqlite3.Row | None, key: str) -> str:
    if not row or key not in row.keys():
        return ""
    return str(row[key] or "")


def _testimony_status_counts(items: Iterable[dict]) -> dict[str, int]:
    counts = {"needs_review": 0, "message_review": 0, "identified": 0, "grouped": 0, "not_testimony": 0, "duplicate": 0, "already_named": 0, "all": 0}
    for item in items:
        status = item.get("status") if item.get("status") in counts else "needs_review"
        counts[status] += 1
        if status == "already_named":
            counts["identified"] += 1
        counts["all"] += 1
    return counts


def _sort_testimony_items(items: list[dict], sort: str) -> None:
    if sort == "newest":
        items.sort(key=lambda item: (item.get("modified_at") or "", item.get("title") or ""), reverse=True)
        return
    if sort == "name":
        items.sort(key=lambda item: (item.get("title") or "").lower())
        return
    items.sort(
        key=lambda item: (
            item.get("duration_seconds") is None,
            item.get("duration_seconds") if item.get("duration_seconds") is not None else float("inf"),
            item.get("recording_date") or "9999-99-99",
            (item.get("title") or "").lower(),
        )
    )


def _testimony_status_label(status: str) -> str:
    labels = {
        "needs_review": "Needs Review",
        "message_review": "Message/Event Review",
        "identified": "Identified",
        "grouped": "Grouped",
        "not_testimony": "Not Needed",
        "duplicate": "Duplicate",
        "already_named": "Already Named",
        "all": "All",
    }
    return labels.get(status, status.replace("_", " ").title())


def _initial_testimony_suggestion_job_state() -> dict:
    return {
        "state": "idle",
        "started_at": "",
        "finished_at": "",
        "total": 0,
        "processed": 0,
        "found": 0,
        "checked": 0,
        "skipped": 0,
        "errors": 0,
        "current": "",
        "message": "",
    }


def _testimony_suggestion_job_status(app: Flask) -> dict:
    lock = getattr(app, "testimony_suggestion_job_lock", None)
    if lock:
        with lock:
            return dict(getattr(app, "testimony_suggestion_job", _initial_testimony_suggestion_job_state()))
    return dict(getattr(app, "testimony_suggestion_job", _initial_testimony_suggestion_job_state()))


def _update_testimony_suggestion_job(app: Flask, **updates) -> None:
    with app.testimony_suggestion_job_lock:
        state = dict(app.testimony_suggestion_job)
        state.update(updates)
        app.testimony_suggestion_job = state


def _start_testimony_suggestion_job(app: Flask) -> bool:
    with app.testimony_suggestion_job_lock:
        if app.testimony_suggestion_job.get("state") == "running":
            return False
        app.testimony_suggestion_job = {
            **_initial_testimony_suggestion_job_state(),
            "state": "running",
            "started_at": _utc_now(),
            "message": "Scanning recorder review queue.",
        }
    thread = threading.Thread(target=_run_testimony_suggestion_job, args=(app,), name="testimony-suggestion-job", daemon=True)
    thread.start()
    return True


def _run_testimony_suggestion_job(app: Flask) -> None:
    try:
        with app.app_context():
            targets = _testimony_suggestion_targets(app)
            _update_testimony_suggestion_job(app, total=len(targets), message=f"Processing {len(targets)} unidentified recordings.")
            processed = found = checked = errors = 0
            skipped = int(_testimony_suggestion_job_status(app).get("skipped") or 0)
            for target in targets:
                candidate = target["candidate"]
                _update_testimony_suggestion_job(app, current=Path(candidate.path).name)
                try:
                    duration_seconds = target["duration_seconds"]
                    status = target["status"]
                    service_date = target["service_date"]
                    speaker_name = target["speaker_name"]
                    testimony_title = target["testimony_title"]
                    notes = target["notes"]
                    proposed_path = target["proposed_path"]
                    suggested_speaker, suggestion_source, suggestion_text, suggestion_error = _generate_testimony_speaker_suggestion(app, candidate)
                    if (
                        status == "needs_review"
                        and not suggested_speaker
                        and _testimony_looks_like_message_recording(
                            app,
                            duration_seconds,
                            suggestion_text,
                            Path(candidate.path),
                        )
                    ):
                        status = "message_review"
                        testimony_title = testimony_title or "Message / Event Needs Review"
                        suggestion_source = suggestion_source or "transcript_intro"
                        suggestion_text = suggestion_text or "Likely message recording based on duration and intro."
                    if suggested_speaker:
                        found += 1
                    elif suggestion_source or suggestion_text:
                        checked += 1
                    else:
                        errors += 1
                        suggestion_source = ""
                        suggestion_text = suggestion_error or "No speaker suggestion found."
                    _save_testimony_review(
                        app,
                        recording_id=candidate.id,
                        source_path=candidate.path,
                        status=status,
                        service_date=service_date,
                        speaker_name=speaker_name,
                        testimony_title=testimony_title,
                        notes=notes,
                        proposed_path=proposed_path,
                        duration_seconds=duration_seconds,
                        suggested_speaker=suggested_speaker,
                        suggestion_source=suggestion_source,
                        suggestion_text=suggestion_text,
                    )
                except Exception as exc:
                    errors += 1
                    app.logger.exception("testimony suggestion failed for %s", candidate.path)
                    _save_testimony_review(
                        app,
                        recording_id=candidate.id,
                        source_path=candidate.path,
                        status=target["status"],
                        service_date=target["service_date"],
                        speaker_name=target["speaker_name"],
                        testimony_title=target["testimony_title"],
                        notes=target["notes"],
                        proposed_path=target["proposed_path"],
                        duration_seconds=target["duration_seconds"],
                        suggested_speaker="",
                        suggestion_source="",
                        suggestion_text=f"Suggestion failed: {exc}",
                    )
                processed += 1
                _update_testimony_suggestion_job(
                    app,
                    processed=processed,
                    found=found,
                    checked=checked,
                    skipped=skipped,
                    errors=errors,
                    message=f"Processed {processed} of {len(targets)} recordings.",
                )
            _update_testimony_suggestion_job(
                app,
                state="finished",
                finished_at=_utc_now(),
                current="",
                message=f"Finished. Suggested {found}; checked {checked}; errors {errors}.",
            )
    except Exception as exc:
        app.logger.exception("testimony suggestion job failed")
        _update_testimony_suggestion_job(
            app,
            state="failed",
            finished_at=_utc_now(),
            current="",
            errors=_testimony_suggestion_job_status(app).get("errors", 0) + 1,
            message=f"Suggestion job failed: {exc}",
        )


def _testimony_suggestion_targets(app: Flask) -> list[dict]:
    rows = _testimony_review_rows(app)
    known_speakers = _testimony_known_speakers(app)
    targets = []
    skipped = 0
    for candidate in _testimony_source_candidates(app):
        row = rows.get(candidate.id)
        duration_seconds = _row_duration(row) if row else None
        if duration_seconds is None:
            duration_seconds = _probe_audio_duration(Path(candidate.path))
        status = _testimony_status_for_candidate(app, candidate, row, duration_seconds)
        service_date = str(row["service_date"] or "") if row else ""
        speaker_name = str(row["speaker_name"] or "") if row else ""
        testimony_title = str(row["testimony_title"] or "") if row else ""
        notes = str(row["notes"] or "") if row else ""
        proposed_path = str(row["proposed_path"] or "") if row else ""
        suggested_speaker = _valid_person_name_suggestion(str(row["suggested_speaker"] or "") if row else "", known_speakers)
        suggestion_source = str(row["suggestion_source"] or "") if row else ""
        if not service_date:
            service_date = candidate.recording_date
        review_note_text = str(row["suggestion_text"] or "") if row else ""
        if status == "needs_review" and _duration_is_too_short_for_testimony(app, duration_seconds):
            status = "not_testimony"
            review_note_text = review_note_text or "Too short to be useful recorder material."
        if status == "needs_review" and _testimony_looks_like_message_recording(
            app,
            duration_seconds,
            str(row["suggestion_text"] or "") if row else "",
            Path(candidate.path),
        ):
            status = "message_review"
            testimony_title = testimony_title or "Message / Event Needs Review"
            review_note_text = review_note_text or "Likely message recording based on duration and intro."
        if status in {"not_testimony", "message_review"} and (not row or str(row["status"] or "") != status or _row_duration(row) is None):
            _save_testimony_review(
                app,
                recording_id=candidate.id,
                source_path=candidate.path,
                status=status,
                service_date=service_date,
                speaker_name=speaker_name,
                testimony_title=testimony_title,
                notes=notes,
                proposed_path=proposed_path,
                duration_seconds=duration_seconds,
                suggested_speaker=suggested_speaker,
                suggestion_source=suggestion_source,
                suggestion_text=review_note_text,
            )
        if status != "needs_review" or speaker_name or suggested_speaker or suggestion_source:
            skipped += 1
            continue
        targets.append(
            {
                "candidate": candidate,
                "row": row,
                "duration_seconds": duration_seconds,
                "status": status,
                "service_date": service_date,
                "speaker_name": speaker_name,
                "testimony_title": testimony_title,
                "notes": notes,
                "proposed_path": proposed_path,
            }
        )
    _update_testimony_suggestion_job(app, skipped=skipped)
    return targets


def _initial_testimony_transcript_job_state() -> dict:
    return {
        "state": "idle",
        "started_at": "",
        "finished_at": "",
        "total": 0,
        "processed": 0,
        "saved": 0,
        "skipped": 0,
        "errors": 0,
        "current": "",
        "message": "",
    }


def _testimony_transcript_job_status(app: Flask) -> dict:
    lock = getattr(app, "testimony_transcript_job_lock", None)
    if lock:
        with lock:
            return dict(getattr(app, "testimony_transcript_job", _initial_testimony_transcript_job_state()))
    return dict(getattr(app, "testimony_transcript_job", _initial_testimony_transcript_job_state()))


def _update_testimony_transcript_job(app: Flask, **updates) -> None:
    with app.testimony_transcript_job_lock:
        state = dict(app.testimony_transcript_job)
        state.update(updates)
        app.testimony_transcript_job = state


def _start_testimony_transcript_job(app: Flask, limit: int | None = None, statuses: set[str] | None = None) -> bool:
    with app.testimony_transcript_job_lock:
        if app.testimony_transcript_job.get("state") == "running":
            return False
        app.testimony_transcript_job = {
            **_initial_testimony_transcript_job_state(),
            "state": "running",
            "started_at": _utc_now(),
            "message": "Scanning identified testimonies.",
        }
    thread = threading.Thread(target=_run_testimony_transcript_job, args=(app, limit, statuses), name="testimony-transcript-job", daemon=True)
    thread.start()
    return True


def _run_testimony_transcript_job(app: Flask, limit: int | None = None, statuses: set[str] | None = None) -> None:
    try:
        with app.app_context():
            targets = _testimony_transcript_targets(app, limit, statuses=statuses)
            _update_testimony_transcript_job(app, total=len(targets), message=f"Processing {len(targets)} testimony transcripts.")
            processed = saved = errors = 0
            for target in targets:
                row = target["row"]
                candidate = target["candidate"]
                _update_testimony_transcript_job(app, current=Path(candidate.path).name)
                transcript_text = ""
                transcript_error = ""
                try:
                    transcript_text, transcript_error = _transcribe_testimony_review_excerpt(app, Path(candidate.path))
                    if transcript_text:
                        saved += 1
                    else:
                        errors += 1
                        transcript_error = transcript_error or "Transcript was empty."
                except Exception as exc:
                    errors += 1
                    transcript_error = f"Transcript failed: {exc}"
                    app.logger.exception("testimony transcript failed for %s", candidate.path)
                recording_id = str(row["recording_id"])
                _save_testimony_transcript(
                    app,
                    recording_id=recording_id,
                    transcript_text=transcript_text,
                    transcript_source="transcript_excerpt" if transcript_text else "",
                    transcript_error=transcript_error,
                )
                if transcript_text and not str(row["speaker_name"] or ""):
                    known_speakers = _testimony_known_speakers(app)
                    transcript_speaker = _valid_person_name_suggestion(_extract_intro_speaker(transcript_text, known_speakers), known_speakers)
                    existing_suggestion = _valid_person_name_suggestion(str(row["suggested_speaker"] or ""), known_speakers)
                    if transcript_speaker or not str(row["suggestion_source"] or ""):
                        _save_testimony_review(
                            app,
                            recording_id=recording_id,
                            source_path=str(row["source_path"] or candidate.path),
                            status=str(row["status"] or "needs_review"),
                            service_date=str(row["service_date"] or candidate.recording_date or ""),
                            speaker_name=str(row["speaker_name"] or ""),
                            testimony_title=str(row["testimony_title"] or ""),
                            notes=str(row["notes"] or ""),
                            proposed_path=str(row["proposed_path"] or ""),
                            duration_seconds=_row_duration(row),
                            suggested_speaker=transcript_speaker or existing_suggestion,
                            suggestion_source="transcript_excerpt",
                            suggestion_text=_compact_transcript_excerpt(transcript_text),
                        )
                processed += 1
                _update_testimony_transcript_job(
                    app,
                    processed=processed,
                    saved=saved,
                    errors=errors,
                    message=f"Processed {processed} of {len(targets)} testimony transcripts.",
                )
            _update_testimony_transcript_job(
                app,
                state="finished",
                finished_at=_utc_now(),
                current="",
                message=f"Finished. Saved {saved}; errors {errors}.",
            )
    except Exception as exc:
        app.logger.exception("testimony transcript job failed")
        _update_testimony_transcript_job(
            app,
            state="failed",
            finished_at=_utc_now(),
            current="",
            errors=_testimony_transcript_job_status(app).get("errors", 0) + 1,
            message=f"Transcript job failed: {exc}",
        )


def _testimony_transcript_statuses_for_filter(status_filter: str) -> set[str]:
    status_filter = (status_filter or "").strip().lower()
    if status_filter == "needs_review":
        return {"needs_review"}
    if status_filter == "grouped":
        return {"grouped"}
    if status_filter == "message_review":
        return {"message_review"}
    if status_filter in {"all", ""}:
        return {"needs_review", "message_review", "identified", "grouped", "already_named"}
    return {"identified", "already_named"}


def _testimony_transcript_targets(app: Flask, limit: int | None = None, statuses: set[str] | None = None) -> list[dict]:
    rows = _testimony_review_rows(app)
    targets = []
    skipped = 0
    target_statuses = statuses or {"message_review", "identified", "grouped", "already_named"}
    for row in rows.values():
        if str(row["status"] or "") not in target_statuses:
            continue
        if _row_optional_text(row, "transcript_text"):
            skipped += 1
            continue
        candidate = _testimony_candidate_from_review_row(app, row)
        if not candidate:
            skipped += 1
            continue
        targets.append({"row": row, "candidate": candidate})
    targets.sort(key=lambda item: (str(item["row"]["service_date"] or ""), str(item["row"]["speaker_name"] or ""), Path(item["candidate"].path).name))
    if limit:
        targets = targets[: max(0, limit)]
    _update_testimony_transcript_job(app, skipped=skipped)
    return targets


def _testimony_suggestion_source_label(source: str) -> str:
    labels = {
        "filename": "from filename",
        "transcript_intro": "from transcript",
        "transcript_excerpt": "from transcript",
        "history": "from confirmed history",
    }
    return labels.get(source, source.replace("_", " ") if source else "")


def _testimony_display_transcript_preview(transcript_text: str, suggestion_source: str, suggestion_text: str, limit: int = 900) -> str:
    transcript_excerpt = _compact_transcript_excerpt(transcript_text, limit)
    if transcript_excerpt:
        return transcript_excerpt
    if suggestion_source in {"transcript_intro", "transcript_excerpt"}:
        return _compact_transcript_excerpt(suggestion_text, limit)
    return ""


def _testimony_transcript_source_label(source: str) -> str:
    labels = {
        "transcript_excerpt": "from stored transcript excerpt",
    }
    return labels.get(source, source.replace("_", " ") if source else "")


def _save_testimony_review(
    app: Flask,
    *,
    recording_id: str,
    source_path: str,
    status: str,
    service_date: str,
    speaker_name: str,
    testimony_title: str,
    notes: str,
    proposed_path: str,
    duration_seconds: float | None,
    suggested_speaker: str | None = None,
    suggestion_source: str | None = None,
    suggestion_text: str | None = None,
) -> None:
    update_suggestion = suggested_speaker is not None or suggestion_source is not None or suggestion_text is not None
    suggested_speaker_value = suggested_speaker or ""
    suggestion_source_value = suggestion_source or ""
    suggestion_text_value = suggestion_text or ""
    suggestion_updated_at = _utc_now() if update_suggestion and (suggested_speaker_value or suggestion_source_value or suggestion_text_value) else ""
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(recording_id) DO UPDATE SET
                source_path = excluded.source_path,
                status = excluded.status,
                service_date = excluded.service_date,
                speaker_name = excluded.speaker_name,
                testimony_title = excluded.testimony_title,
                notes = excluded.notes,
                proposed_path = excluded.proposed_path,
                duration_seconds = excluded.duration_seconds,
                suggested_speaker = CASE WHEN ? THEN excluded.suggested_speaker ELSE suggested_speaker END,
                suggestion_source = CASE WHEN ? THEN excluded.suggestion_source ELSE suggestion_source END,
                suggestion_text = CASE WHEN ? THEN excluded.suggestion_text ELSE suggestion_text END,
                suggestion_updated_at = CASE WHEN ? THEN excluded.suggestion_updated_at ELSE suggestion_updated_at END,
                updated_at = excluded.updated_at
            """,
            (
                recording_id,
                source_path,
                status,
                service_date,
                speaker_name,
                testimony_title,
                notes,
                proposed_path,
                duration_seconds,
                suggested_speaker_value,
                suggestion_source_value,
                suggestion_text_value,
                suggestion_updated_at,
                _utc_now(),
                1 if update_suggestion else 0,
                1 if update_suggestion else 0,
                1 if update_suggestion else 0,
                1 if update_suggestion else 0,
            ),
        )


def _save_testimony_transcript(app: Flask, recording_id: str, transcript_text: str, transcript_source: str, transcript_error: str) -> None:
    updated_at = _utc_now()
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        connection.execute(
            """
            UPDATE testimony_reviews
            SET transcript_text = ?,
                transcript_source = ?,
                transcript_error = ?,
                transcript_updated_at = ?,
                updated_at = ?
            WHERE recording_id = ?
            """,
            (
                transcript_text,
                transcript_source,
                transcript_error,
                updated_at,
                updated_at,
                recording_id,
            ),
        )


def _testimony_quarantine_status_folder(status: str) -> str:
    if status == "duplicate":
        return "Duplicate"
    return "Not Testimony"


def _quarantine_testimony_destination(app: Flask, candidate: RecordingCandidate, row: sqlite3.Row) -> Path:
    service_date = _normalize_date(str(row["service_date"] or "")) or candidate.recording_date or ""
    year = service_date[:4] if service_date else "Unsorted"
    filename = _sanitize_filename_part(Path(candidate.path).stem) + Path(candidate.path).suffix.lower()
    return _testimony_rejected_root(app) / _testimony_quarantine_status_folder(str(row["status"] or "")) / year / filename


def _save_testimony_quarantine(
    app: Flask,
    *,
    recording_id: str,
    source_path: str,
    quarantined_from_path: str,
    quarantined_path: str,
) -> None:
    updated_at = _utc_now()
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        connection.execute(
            """
            UPDATE testimony_reviews
            SET source_path = ?,
                quarantined_from_path = ?,
                quarantined_path = ?,
                quarantined_at = ?,
                updated_at = ?
            WHERE recording_id = ?
            """,
            (
                source_path,
                quarantined_from_path,
                quarantined_path,
                updated_at,
                updated_at,
                recording_id,
            ),
        )


def _quarantine_testimony_reviews(app: Flask, statuses: set[str]) -> tuple[int, int, int]:
    allowed_statuses = {"not_testimony", "duplicate"}
    target_statuses = statuses & allowed_statuses
    if not target_statuses:
        return 0, 0, 0
    rows = _testimony_review_rows(app)
    moved = skipped = errors = 0
    rejected_root = _testimony_rejected_root(app)
    for row in rows.values():
        row_status = str(row["status"] or "")
        if row_status not in target_statuses:
            continue
        candidate = _testimony_candidate_from_review_row(app, row)
        if not candidate:
            skipped += 1
            continue
        source_path = Path(candidate.path)
        if _path_within(source_path, rejected_root):
            skipped += 1
            if not _row_optional_text(row, "quarantined_path"):
                _save_testimony_quarantine(
                    app,
                    recording_id=str(row["recording_id"]),
                    source_path=str(source_path),
                    quarantined_from_path=_row_optional_text(row, "quarantined_from_path") or str(source_path),
                    quarantined_path=str(source_path),
                )
            continue
        if not source_path.exists() or not source_path.is_file():
            skipped += 1
            continue
        destination = _unique_destination_path(_quarantine_testimony_destination(app, candidate, row))
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_path), str(destination))
        except OSError:
            app.logger.exception("failed to quarantine testimony review file %s", source_path)
            errors += 1
            continue
        _save_testimony_quarantine(
            app,
            recording_id=str(row["recording_id"]),
            source_path=str(destination),
            quarantined_from_path=str(source_path),
            quarantined_path=str(destination),
        )
        moved += 1
    return moved, skipped, errors


def _delete_testimony_review(app: Flask, recording_id: str) -> None:
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        connection.execute("DELETE FROM testimony_reviews WHERE recording_id = ?", (recording_id,))


def _unique_destination_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem} - {index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"No available filename near {path}")


def _testimony_final_suffix(source_path: Path) -> str:
    suffix = source_path.suffix.lower()
    if suffix == ".wav":
        return TESTIMONY_FINAL_AUDIO_EXTENSION
    return suffix


def _write_final_testimony_audio(source_path: Path, target_path: Path) -> None:
    if source_path.suffix.lower() != ".wav" or target_path.suffix.lower() != TESTIMONY_FINAL_AUDIO_EXTENSION:
        shutil.move(str(source_path), str(target_path))
        return
    temporary_target = target_path.with_name(f".{target_path.stem}.tmp-{secrets.token_hex(6)}{target_path.suffix}")
    try:
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(source_path),
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(temporary_target),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if completed.returncode != 0 or not temporary_target.exists() or temporary_target.stat().st_size <= 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise OSError(f"WAV to MP3 conversion failed{': ' + detail[:180] if detail else ''}")
        os.replace(temporary_target, target_path)
        source_path.unlink()
    finally:
        if temporary_target.exists():
            temporary_target.unlink()


def _rename_testimony_recording(
    app: Flask,
    candidate: RecordingCandidate,
    *,
    service_date: str,
    speaker_name: str,
    testimony_title: str,
) -> tuple[RecordingCandidate, str, str]:
    source_path = Path(candidate.path)
    proposed_path = Path(_proposed_testimony_path(app, source_path, service_date, speaker_name, testimony_title))
    message_root = _message_recording_root(app)
    testimony_root = _testimony_recording_root(app)
    source_root = _testimony_source_root(app)
    if not _path_within(source_path, source_root) and not _path_within(source_path, message_root) and not _path_within(source_path, testimony_root):
        return candidate, str(proposed_path), "Source file is outside the testimony recording folder."
    if not _path_within(proposed_path, testimony_root):
        return candidate, str(proposed_path), "Proposed testimony filename is outside TestimonyRecordings."
    try:
        resolved_source = source_path.resolve()
        resolved_target = proposed_path.resolve()
    except FileNotFoundError:
        return candidate, str(proposed_path), "Source file was not found."
    if resolved_source == resolved_target:
        return candidate, str(proposed_path), ""
    try:
        proposed_path.parent.mkdir(parents=True, exist_ok=True)
        target_path = _unique_destination_path(proposed_path)
        _write_final_testimony_audio(source_path, target_path)
    except OSError as exc:
        return candidate, str(proposed_path), f"File move failed: {exc}"
    renamed = _testimony_source_candidate_from_path(app, target_path)
    if not renamed:
        return candidate, str(target_path), "File was renamed but could not be reloaded."
    return renamed, str(target_path), ""


def _probe_missing_testimony_durations(app: Flask, limit: int) -> tuple[int, int]:
    rows = _testimony_review_rows(app)
    probed = 0
    skipped = 0
    for candidate in _testimony_source_candidates(app):
        if probed + skipped >= limit:
            break
        row = rows.get(candidate.id)
        if _row_duration(row) is not None:
            skipped += 1
            continue
        duration = _probe_audio_duration(Path(candidate.path))
        if duration is None:
            skipped += 1
            continue
        status = _testimony_status_for_candidate(app, candidate, row, duration)
        service_date = str(row["service_date"] or "") if row else candidate.recording_date
        speaker_name = str(row["speaker_name"] or "") if row else ""
        testimony_title = str(row["testimony_title"] or "") if row else ""
        notes = str(row["notes"] or "") if row else ""
        proposed_path = str(row["proposed_path"] or "") if row else ""
        _save_testimony_review(
            app,
            recording_id=candidate.id,
            source_path=candidate.path,
            status=status,
            service_date=service_date,
            speaker_name=speaker_name,
            testimony_title=testimony_title,
            notes=notes,
            proposed_path=proposed_path,
            duration_seconds=duration,
        )
        probed += 1
    return probed, skipped


def _probe_audio_duration(path: Path) -> float | None:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        duration = float((completed.stdout or "").strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


def _testimony_status_for_candidate(
    app: Flask,
    candidate: RecordingCandidate,
    row: sqlite3.Row | None,
    duration_seconds: float | None,
) -> str:
    status = str(row["status"] or "") if row else ""
    if status in {"identified", "grouped", "message_review", "not_testimony", "duplicate", "already_named"}:
        return status
    if status not in TESTIMONY_REVIEW_STATUSES:
        status = "already_named" if _raw_testimony_name(Path(candidate.path)) else "needs_review"
    if status == "needs_review" and _named_non_testimony_recording(candidate):
        return "message_review"
    if status == "needs_review" and _duration_is_too_short_for_testimony(app, duration_seconds):
        return "not_testimony"
    return status


def _duration_is_too_short_for_testimony(app: Flask, duration_seconds: float | None) -> bool:
    if duration_seconds is None:
        return False
    try:
        minimum = int(app.config.get("NTC_RECORDINGS_TESTIMONY_MIN_SECONDS") or 45)
    except (TypeError, ValueError):
        minimum = 45
    return 0 < duration_seconds < max(1, minimum)


def _testimony_looks_like_message_recording(
    app: Flask,
    duration_seconds: float | None,
    intro_text: str,
    source_path: Path | None = None,
) -> bool:
    if duration_seconds is None:
        return False
    try:
        hard_max = int(app.config.get("NTC_RECORDINGS_TESTIMONY_HARD_MAX_SECONDS") or 4500)
    except (TypeError, ValueError):
        hard_max = 4500
    if duration_seconds >= max(1, hard_max):
        return True
    try:
        message_minimum = int(app.config.get("NTC_RECORDINGS_TESTIMONY_MESSAGE_MIN_SECONDS") or 1800)
    except (TypeError, ValueError):
        message_minimum = 1800
    if duration_seconds < max(1, message_minimum):
        return False
    text = " ".join((intro_text or "").split()).lower()
    if not text:
        return False
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in TESTIMONY_EXPLICIT_INTRO_PATTERNS):
        return False
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in TESTIMONY_MESSAGE_INTRO_PATTERNS):
        return True
    try:
        long_service_minimum = int(app.config.get("NTC_RECORDINGS_TESTIMONY_LONG_SERVICE_SECONDS") or 2700)
    except (TypeError, ValueError):
        long_service_minimum = 2700
    source_is_labeled_testimony = bool(source_path and _raw_testimony_name(Path(source_path)))
    return duration_seconds >= max(message_minimum, long_service_minimum) and not source_is_labeled_testimony


def _testimony_title_for_speaker(speaker_name: str) -> str:
    speaker = speaker_name.strip()
    if speaker:
        return f"{speaker}'s Testimony"
    return ""


def _testimony_known_speakers(app: Flask) -> list[str]:
    names: dict[str, str] = {}
    with _connect(app.config["NTC_RECORDINGS_DB_PATH"]) as connection:
        rows = connection.execute(
            """
            SELECT speaker_name, source_path, proposed_path
            FROM testimony_reviews
            WHERE status IN ('identified', 'already_named')
            ORDER BY updated_at DESC
            """
        ).fetchall()
    for row in rows:
        for value in (row["speaker_name"], _testimony_filename_speaker_suggestion(Path(row["source_path"] or "")), _testimony_filename_speaker_suggestion(Path(row["proposed_path"] or ""))):
            value = _clean_speaker_name(value)
            if value:
                names.setdefault(_speaker_key(value), value)
    return sorted(names.values(), key=lambda name: _speaker_key(name))


def _testimony_filename_speaker_suggestion(path: Path) -> str:
    name = _strip_audio_extensions(path.name)
    if not name:
        return ""
    name = re.sub(r"^\d{8}\s*[-–—]\s*", "", name)
    name = re.sub(r"^\d{4}-\d{2}-\d{2}\s*[-–—]\s*", "", name)
    name = re.sub(r"^[A-Za-z]+\s+\d{1,2},\s+\d{4}\s*[-–—]\s*", "", name)
    if not re.search(r"(?i)\btestimon(?:y|ies)\b", name):
        return ""
    name = re.sub(r"(?i)'s\s+testimony$", "", name).strip()
    name = re.sub(r"(?i)\s+testimony$", "", name).strip()
    candidate = _clean_speaker_name(name)
    if not _person_name_candidate(candidate, []):
        return ""
    return candidate


def _strip_audio_extensions(filename: str) -> str:
    stem = filename.strip()
    while stem:
        suffix = Path(stem).suffix
        if suffix.lower() not in AUDIO_EXTENSIONS:
            return stem
        stem = stem[: -len(suffix)]
    return ""


def _generate_testimony_speaker_suggestion(app: Flask, candidate: RecordingCandidate) -> tuple[str, str, str, str]:
    filename_speaker = _testimony_filename_speaker_suggestion(Path(candidate.path))
    if filename_speaker:
        return filename_speaker, "filename", Path(candidate.path).name, ""

    transcript, error = _transcribe_testimony_intro(app, Path(candidate.path))
    if error:
        return "", "", "", error
    transcript_excerpt = _compact_transcript_excerpt(transcript)
    suggested_speaker = _extract_intro_speaker(transcript, _testimony_known_speakers(app))
    if suggested_speaker:
        return suggested_speaker, "transcript_intro", transcript_excerpt, ""
    if transcript_excerpt:
        return "", "transcript_intro", transcript_excerpt, "No speaker name found in the intro transcript."
    return "", "transcript_intro", "", "The intro transcript was empty."


def _transcribe_testimony_intro(app: Flask, source_path: Path) -> tuple[str, str]:
    transcribe_url = str(app.config.get("NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_URL") or "").strip()
    if not transcribe_url:
        return "", "Speaker suggestion needs a testimony transcription URL configured."
    try:
        seconds = int(app.config.get("NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_SECONDS") or 90)
    except (TypeError, ValueError):
        seconds = 90
    seconds = min(max(seconds, 15), 300)
    timeout = float(app.config.get("NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_TIMEOUT") or 120)
    prompt = str(app.config.get("NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_PROMPT") or "")
    with tempfile.TemporaryDirectory(prefix="ntc-testimony-intro-") as temp_dir:
        wav_path = Path(temp_dir) / "intro.wav"
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(source_path),
                "-t",
                str(seconds),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "wav",
                str(wav_path),
            ],
            capture_output=True,
            text=True,
            timeout=min(timeout, 60),
            check=False,
        )
        if completed.returncode != 0 or not wav_path.exists():
            detail = (completed.stderr or completed.stdout or "").strip()
            return "", f"Could not prepare intro audio for transcription{': ' + detail[:180] if detail else ''}."
        try:
            response = requests.post(
                transcribe_url,
                params={"language": "en", "prompt": prompt, "max_new_tokens": "128"},
                data=wav_path.read_bytes(),
                headers={"Content-Type": "audio/wav", "Accept": "application/json"},
                timeout=timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            return "", f"Transcription request failed: {exc}"
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip(), ""
    return str(payload.get("text") or "").strip(), ""


def _transcribe_testimony_review_excerpt(app: Flask, source_path: Path) -> tuple[str, str]:
    transcribe_url = str(app.config.get("NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_URL") or "").strip()
    if not transcribe_url:
        return "", "Testimony transcripts need a transcription URL configured."
    try:
        seconds = int(app.config.get("NTC_RECORDINGS_TESTIMONY_TRANSCRIPT_SECONDS") or 240)
    except (TypeError, ValueError):
        seconds = 240
    seconds = min(max(seconds, 30), 900)
    try:
        max_tokens = int(app.config.get("NTC_RECORDINGS_TESTIMONY_TRANSCRIPT_MAX_TOKENS") or 384)
    except (TypeError, ValueError):
        max_tokens = 384
    max_tokens = min(max(max_tokens, 128), 384)
    timeout = float(app.config.get("NTC_RECORDINGS_TESTIMONY_TRANSCRIBE_TIMEOUT") or 120)
    prompt = str(
        app.config.get("NTC_RECORDINGS_TESTIMONY_TRANSCRIPT_PROMPT")
        or "Transcribe this church testimony clearly. Keep names exactly as spoken."
    )
    with tempfile.TemporaryDirectory(prefix="ntc-testimony-transcript-") as temp_dir:
        wav_path = Path(temp_dir) / "testimony.wav"
        completed = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(source_path),
                "-t",
                str(seconds),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "wav",
                str(wav_path),
            ],
            capture_output=True,
            text=True,
            timeout=min(max(timeout, 30), 120),
            check=False,
        )
        if completed.returncode != 0 or not wav_path.exists():
            detail = (completed.stderr or completed.stdout or "").strip()
            return "", f"Could not prepare testimony audio for transcription{': ' + detail[:180] if detail else ''}."
        try:
            response = requests.post(
                transcribe_url,
                params={"language": "en", "prompt": prompt, "max_new_tokens": str(max_tokens)},
                data=wav_path.read_bytes(),
                headers={"Content-Type": "audio/wav", "Accept": "application/json"},
                timeout=timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            return "", f"Transcription request failed: {exc}"
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip(), ""
    return str(payload.get("text") or "").strip(), ""


def _extract_intro_speaker(transcript: str, known_speakers: Iterable[str]) -> str:
    text = " ".join((transcript or "").split())
    if not text:
        return ""
    patterns = [
        r"\bmy\s+name\s+is\s+([a-z][a-z' -]{1,60})",
        r"\bmy\s+name'?s\s+([a-z][a-z' -]{1,60})",
        r"\bi\s+am\s+([a-z][a-z' -]{1,60})",
        r"\bi'?m\s+([a-z][a-z' -]{1,60})",
        r"\bthis\s+is\s+((?:brother|bro|sister|sis)\s+[a-z][a-z' -]{1,50})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        name = _clean_transcript_name(match.group(1))
        if not _person_name_candidate(name, known_speakers):
            continue
        return _canonical_speaker_name(name, known_speakers)
    return ""


def _clean_transcript_name(value: str) -> str:
    value = re.split(r"[\.,;:!?()\[\]\n\r]", value, maxsplit=1)[0]
    boundary = "|".join(sorted(re.escape(word) for word in TRANSCRIPT_NAME_BOUNDARY_WORDS))
    value = re.split(rf"\b(?:{boundary})\b", value, maxsplit=1, flags=re.IGNORECASE)[0]
    value = re.sub(r"(?i)'s\s+(?:testimony|story)\b.*$", "", value)
    value = re.sub(r"(?i)\b(?:testimony|story)\b.*$", "", value)
    words = re.findall(r"[A-Za-z][A-Za-z']*", value)
    if not words:
        return ""
    return _clean_speaker_name(" ".join(words[:4]))


def _valid_person_name_suggestion(value: str, known_speakers: Iterable[str]) -> str:
    candidate = _clean_speaker_name(value)
    if not _person_name_candidate(candidate, known_speakers):
        return ""
    return _canonical_speaker_name(candidate, known_speakers)


def _person_name_candidate(value: str, known_speakers: Iterable[str]) -> bool:
    candidate = _clean_speaker_name(value)
    if not candidate:
        return False
    known = list(known_speakers)
    candidate_key = _speaker_key(candidate)
    candidate_titleless_key = _speaker_key(_remove_speaker_title(candidate))
    for known_speaker in known:
        if candidate_key == _speaker_key(known_speaker) or candidate_titleless_key == _speaker_key(_remove_speaker_title(known_speaker)):
            return True
    words = candidate.split()
    lowered = [word.strip("'").lower() for word in words]
    starts_with_title = lowered[0] in {"brother", "sister"}
    if starts_with_title:
        if len(words) < 2 or len(words) > 4:
            return False
        checked_words = lowered[1:]
    else:
        if len(words) > 3:
            return False
        checked_words = lowered
    if not checked_words:
        return False
    if checked_words[0] in TRANSCRIPT_NAME_REJECT_FIRST_WORDS:
        return False
    if any(word in TRANSCRIPT_NAME_REJECT_WORDS for word in checked_words):
        return False
    for index, word in enumerate(checked_words):
        if len(word) <= 1 and not (index > 0 and len(word) == 1):
            return False
    return True


def _clean_speaker_name(value: str) -> str:
    value = re.sub(r"\s+", " ", (value or "").strip(" -–—'\""))
    if not value:
        return ""
    parts = []
    for word in value.split():
        if word.lower() in {"bro", "brother"}:
            parts.append("Brother")
        elif word.lower() in {"sis", "sister"}:
            parts.append("Sister")
        else:
            parts.append(_title_name_word(word))
    return " ".join(parts).strip()


def _title_name_word(word: str) -> str:
    pieces = [piece for piece in word.split("'") if piece]
    titled = []
    for index, piece in enumerate(pieces):
        lowered = piece.lower()
        if index > 0 and lowered in {"s", "t", "re", "ve", "ll", "d", "m"}:
            titled.append(lowered)
        else:
            titled.append(piece[:1].upper() + piece[1:].lower())
    return "'".join(titled)


def _canonical_speaker_name(candidate: str, known_speakers: Iterable[str]) -> str:
    candidate = _clean_speaker_name(candidate)
    if not candidate:
        return ""
    candidate_key = _speaker_key(candidate)
    candidate_titleless_key = _speaker_key(_remove_speaker_title(candidate))
    for known in known_speakers:
        known_key = _speaker_key(known)
        if candidate_key == known_key or candidate_titleless_key == _speaker_key(_remove_speaker_title(known)):
            return known
    return candidate


def _remove_speaker_title(value: str) -> str:
    return re.sub(r"^(brother|sister|bro|sis)\s+", "", value.strip(), flags=re.IGNORECASE)


def _speaker_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _compact_transcript_excerpt(text: str, limit: int = 360) -> str:
    excerpt = " ".join((text or "").split())
    if len(excerpt) <= limit:
        return excerpt
    return excerpt[: limit - 1].rstrip() + "..."


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "Unknown"
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _proposed_testimony_path(
    app: Flask,
    source_path: Path,
    service_date: str,
    speaker_name: str,
    testimony_title: str,
) -> str:
    normalized_date = _normalize_date(service_date or "") or ""
    if normalized_date:
        year = normalized_date[:4]
        date_prefix = _format_date(normalized_date)
    else:
        year = "Unsorted"
        date_prefix = _sanitize_filename_part(source_path.stem) or "Undated"
    title = testimony_title.strip()
    speaker = speaker_name.strip()
    if not title and speaker:
        title = f"{speaker}'s Testimony"
    if not title:
        title = "Testimony"
    filename = _sanitize_filename_part(f"{date_prefix} - {title}") + _testimony_final_suffix(source_path)
    event_folder = _testimony_event_folder(service_date)
    if event_folder:
        category, folder_name = event_folder
        return str(_testimony_recording_root(app) / year / category / folder_name / filename)
    return str(_testimony_recording_root(app) / year / "Sunday Testimonies" / filename)


def _testimony_event_folder(service_date: str) -> tuple[str, str] | None:
    normalized_date = _normalize_date(service_date or "")
    if not normalized_date:
        return None
    return TESTIMONY_EVENT_FOLDERS.get(normalized_date)


def _date_from_file_metadata(stat_result: os.stat_result) -> str | None:
    try:
        return datetime.fromtimestamp(stat_result.st_mtime, _recording_local_timezone()).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _recording_local_timezone():
    timezone_name = os.getenv("NTC_RECORDINGS_LOCAL_TIMEZONE", "America/New_York")
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _sanitize_filename_part(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "-", str(value or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    return cleaned or "Untitled"


def _candidate_option_label(candidate: RecordingCandidate) -> str:
    if candidate.target_type == "folder":
        return f"{candidate.title} · {candidate.file_count} files · {candidate.relative_path}"
    return f"{candidate.title} · {candidate.relative_path}"


def _normalize_recording_kind(value: str) -> str:
    normalized = re.sub(r"[^a-z]+", "", str(value or "").lower())
    if normalized in {"testimony", "testimonies", "testimonyrecording", "testimoniesrecording"}:
        return "testimony"
    if normalized in {"worship", "worshiprecording", "music", "song"}:
        return "worship"
    if normalized in {"message", "messagerecording", "sermon", "teaching"}:
        return "message"
    return "unsure"


def _recording_kind_for_path(path: Path) -> str:
    normalized = re.sub(r"[^a-z]+", "", str(path).lower())
    if "testimony" in normalized or "testimonies" in normalized:
        return "testimony"
    if "worshiprecordings" in normalized or "worship" in normalized:
        return "worship"
    if "messagerecordings" in normalized or "message" in normalized:
        return "message"
    return "unsure"


def _recording_kind_label(kind: str) -> str:
    labels = {
        "message": "Message",
        "worship": "Worship",
        "testimony": "Testimony",
        "unsure": "Recording",
    }
    return labels.get(kind, "Recording")


def _recording_id(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:24]


def _collection_id(path: Path) -> str:
    return "folder-" + hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:24]


def _display_title(path: Path) -> str:
    stem = path.stem.strip()
    stem = re.sub(r"^\d{4}[-_ .]?\d{1,2}[-_ .]?\d{1,2}\s*[-_ ]*", "", stem)
    stem = re.sub(r"^\d{8}\s*[-_ ]*", "", stem)
    return stem or path.name


def _extract_recording_date(value: str) -> str | None:
    candidates = [
        r"\b(20\d{2})[-_ .]?([01]\d)[-_ .]?([0-3]\d)\b",
        r"\b([01]?\d)[-_/]([0-3]?\d)[-_/](20\d{2})\b",
    ]
    for pattern in candidates:
        match = re.search(pattern, value)
        if not match:
            continue
        groups = match.groups()
        if groups[0].startswith("20"):
            parsed = _date_from_parts(groups[0], groups[1], groups[2])
        else:
            parsed = _date_from_parts(groups[2], groups[0], groups[1])
        if parsed:
            return parsed.isoformat()

    seven_digit = re.search(r"\b(20\d{2})([1-9])([0-3]\d)\b", value)
    if seven_digit:
        parsed = _date_from_parts(seven_digit.group(1), seven_digit.group(2), seven_digit.group(3))
        if parsed:
            return parsed.isoformat()

    month_match = re.search(
        r"\b("
        + "|".join(MONTHS)
        + r")\.?\s+([0-3]?\d)(?:st|nd|rd|th)?,?\s+(20\d{2})\b",
        value,
        re.IGNORECASE,
    )
    if month_match:
        month = MONTHS[month_match.group(1).lower().rstrip(".")]
        parsed = _date_from_parts(month_match.group(3), str(month), month_match.group(2))
        if parsed:
            return parsed.isoformat()
    return None


def _normalize_date(value: str) -> str | None:
    return _extract_recording_date(value) or _date_from_iso(value)


def _date_from_iso(value: str) -> str | None:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _date_from_parts(year: str, month: str, day: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _share_url(app: Flask, token: str) -> str:
    base_url = str(app.config.get("NTC_RECORDINGS_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if base_url:
        return f"{base_url}{url_for('share_recording', token=token)}"
    return url_for("share_recording", token=token, _external=True)


def _create_share_link(
    app: Flask,
    candidate: RecordingCandidate,
    token: str,
    *,
    existing_row: sqlite3.Row | None = None,
) -> tuple[str, str, str, str]:
    provider = str(app.config.get("NTC_RECORDINGS_SHARE_PROVIDER") or "internal").strip().lower()
    internal_url = _share_url(app, token)
    if provider != "nextcloud":
        return internal_url, "internal", "", ""

    if existing_row and str(existing_row["share_provider"] or "").strip().lower() == "nextcloud":
        existing_url = str(existing_row["share_url"] or "").strip()
        existing_id = str(existing_row["share_external_id"] or "").strip()
        if existing_url and existing_id and existing_row["recording_id"] == candidate.id:
            secure_error = _secure_nextcloud_share(app, existing_id)
            if not secure_error:
                return existing_url, "nextcloud", existing_id, ""
            return internal_url, "internal", "", f"Nextcloud share fallback: {secure_error}"

    nextcloud_url, nextcloud_share_id, error = _create_nextcloud_share_link(app, candidate)
    if nextcloud_url:
        return nextcloud_url, "nextcloud", nextcloud_share_id, ""
    return internal_url, "internal", "", f"Nextcloud share fallback: {error or 'not configured'}"


def _create_nextcloud_share_link(app: Flask, candidate: RecordingCandidate) -> tuple[str, str, str]:
    nextcloud_path = _nextcloud_path_for_candidate(app, candidate)
    if not nextcloud_path:
        return "", "", "recording path could not be mapped into Nextcloud"

    existing_shares, lookup_error = _list_nextcloud_shares(app, nextcloud_path)
    if existing_shares:
        share = existing_shares[0]
        secure_error = _secure_nextcloud_share(app, share["id"])
        if secure_error:
            return "", "", secure_error
        return share["url"], share["id"], ""
    if lookup_error:
        return "", "", lookup_error

    config = _nextcloud_config(app)
    if not config:
        return "", "", "Nextcloud credentials are not configured"
    base_url, username, password = config
    endpoint = f"{base_url}/ocs/v2.php/apps/files_sharing/api/v1/shares"
    try:
        response = requests.post(
            endpoint,
            params={"format": "json"},
            headers={"OCS-APIRequest": "true"},
            auth=(username, password),
            data=_nextcloud_public_share_payload(nextcloud_path),
            timeout=15,
        )
    except requests.RequestException as exc:
        return "", "", str(exc)
    if response.status_code >= 400:
        return "", "", f"Nextcloud returned HTTP {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return "", "", "Nextcloud returned non-JSON response"
    data = ((payload.get("ocs") or {}).get("data") or {})
    share_url = (data.get("url") or "").strip()
    share_id = str(data.get("id") or "").strip()
    if not share_url:
        message = (((payload.get("ocs") or {}).get("meta") or {}).get("message") or "missing share URL").strip()
        return "", "", message
    secure_error = _secure_nextcloud_share(app, share_id)
    if secure_error:
        _delete_nextcloud_share(app, share_id)
        return "", "", secure_error
    return share_url, share_id, ""


def _nextcloud_public_share_payload(nextcloud_path: str = "", *, include_share_type: bool = True) -> dict[str, str | int]:
    payload: dict[str, str | int] = {
        "permissions": 1,
        "attributes": _nextcloud_no_download_attributes(),
    }
    if include_share_type:
        payload["shareType"] = 3
    if nextcloud_path:
        payload["path"] = nextcloud_path
    return payload


def _nextcloud_no_download_attributes() -> str:
    return json.dumps(
        [
            {
                "scope": "permissions",
                "key": "download",
                "value": False,
            }
        ],
        separators=(",", ":"),
    )


def _secure_nextcloud_share(app: Flask, share_id: str) -> str:
    if not share_id:
        return "Nextcloud share id is missing"
    config = _nextcloud_config(app)
    if not config:
        return "Nextcloud credentials are not configured"
    base_url, username, password = config
    endpoint = f"{base_url}/ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}"
    try:
        response = requests.put(
            endpoint,
            params={"format": "json"},
            headers={"OCS-APIRequest": "true"},
            auth=(username, password),
            data=_nextcloud_public_share_payload(include_share_type=False),
            timeout=15,
        )
    except requests.RequestException as exc:
        return str(exc)
    if response.status_code >= 400:
        return f"Nextcloud returned HTTP {response.status_code} while locking share"
    return ""


def _delete_nextcloud_share(app: Flask, share_id: str) -> str:
    config = _nextcloud_config(app)
    if not config:
        return "Nextcloud credentials are not configured"
    base_url, username, password = config
    endpoint = f"{base_url}/ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}"
    try:
        response = requests.delete(
            endpoint,
            params={"format": "json"},
            headers={"OCS-APIRequest": "true"},
            auth=(username, password),
            timeout=15,
        )
    except requests.RequestException as exc:
        return str(exc)
    if response.status_code >= 400:
        return f"Nextcloud returned HTTP {response.status_code}"
    return ""


def _revoke_share_link(app: Flask, row: sqlite3.Row) -> str:
    provider = str(row["share_provider"] or "").strip().lower()
    external_id = str(row["share_external_id"] or "").strip()
    if provider != "nextcloud":
        return ""

    if not _nextcloud_config(app):
        return "Nextcloud credentials are not configured"

    share_ids = [external_id] if external_id else []
    if not share_ids:
        nextcloud_path = _nextcloud_path_for_path(app, Path(row["recording_path"] or ""))
        if not nextcloud_path:
            return "Nextcloud share id is missing and the recording path could not be mapped"
        shares, lookup_error = _list_nextcloud_shares(app, nextcloud_path)
        if lookup_error:
            return lookup_error
        share_url = str(row["share_url"] or "").strip()
        if share_url:
            share_ids = [share["id"] for share in shares if share.get("url") == share_url and share.get("id")]
        elif len(shares) == 1:
            share_ids = [shares[0]["id"]]
        if not share_ids:
            return "Nextcloud share id is missing and no matching share was found"

    errors = []
    for share_id in share_ids:
        error = _delete_nextcloud_share(app, share_id)
        if error:
            errors.append(error)
    if errors:
        return "; ".join(errors)
    return ""


def _nextcloud_path_for_candidate(app: Flask, candidate: RecordingCandidate) -> str:
    return _nextcloud_path_for_path(app, Path(candidate.path))


def _nextcloud_path_for_path(app: Flask, path: Path) -> str:
    for local_prefix, nextcloud_prefix in _nextcloud_path_mappings(app):
        try:
            relative = path.resolve().relative_to(local_prefix.resolve())
        except (FileNotFoundError, ValueError):
            continue
        parts = [part for part in relative.parts if part and part != "."]
        if nextcloud_prefix:
            return "/" + "/".join([nextcloud_prefix, *parts])
        return "/" + "/".join(parts)
    local_prefix = Path(str(app.config.get("NTC_NEXTCLOUD_LOCAL_PATH_PREFIX") or DEFAULT_RECORDING_DIR))
    nextcloud_prefix = str(app.config.get("NTC_NEXTCLOUD_PATH_PREFIX") or "").strip().strip("/")
    try:
        relative = path.resolve().relative_to(local_prefix.resolve())
    except (FileNotFoundError, ValueError):
        return ""
    parts = [part for part in relative.parts if part and part != "."]
    if nextcloud_prefix:
        return "/" + "/".join([nextcloud_prefix, *parts])
    return "/" + "/".join(parts)


def _nextcloud_path_mappings(app: Flask) -> list[tuple[Path, str]]:
    raw = str(app.config.get("NTC_NEXTCLOUD_PATH_MAPPINGS") or "").strip()
    mappings: list[tuple[Path, str]] = []
    for item in re.split(r"[;\n]+", raw):
        item = item.strip()
        if not item or "=" not in item:
            continue
        local, remote = item.split("=", 1)
        local = local.strip()
        if not local:
            continue
        mappings.append((Path(local), remote.strip().strip("/")))
    mappings.sort(key=lambda pair: len(str(pair[0])), reverse=True)
    return mappings


def _nextcloud_config(app: Flask) -> tuple[str, str, str] | None:
    base_url = str(app.config.get("NTC_NEXTCLOUD_BASE_URL") or "").strip().rstrip("/")
    username = str(app.config.get("NTC_NEXTCLOUD_USERNAME") or "").strip()
    password = str(app.config.get("NTC_NEXTCLOUD_APP_PASSWORD") or "").strip()
    if not base_url or not username or not password:
        return None
    return base_url, username, password


def _list_nextcloud_shares(app: Flask, nextcloud_path: str) -> tuple[list[dict[str, str]], str]:
    config = _nextcloud_config(app)
    if not config:
        return [], "Nextcloud credentials are not configured"
    base_url, username, password = config
    endpoint = f"{base_url}/ocs/v2.php/apps/files_sharing/api/v1/shares"
    try:
        response = requests.get(
            endpoint,
            params={"format": "json", "path": nextcloud_path, "reshares": "true"},
            headers={"OCS-APIRequest": "true"},
            auth=(username, password),
            timeout=15,
        )
    except requests.RequestException as exc:
        return [], str(exc)
    if response.status_code >= 400:
        return [], f"Nextcloud returned HTTP {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return [], "Nextcloud returned non-JSON response"
    data = ((payload.get("ocs") or {}).get("data") or [])
    if isinstance(data, dict):
        data = [data]
    shares: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        share_url = str(item.get("url") or "").strip()
        share_id = str(item.get("id") or "").strip()
        if share_url and share_id:
            shares.append({"url": share_url, "id": share_id})
    return shares, ""


def _email_enabled(app: Flask) -> bool:
    enabled = str(app.config.get("NTC_RECORDINGS_EMAIL_ENABLED", "0")).strip().lower() in {"1", "true", "yes", "on"}
    return enabled and bool(app.config.get("NTC_RECORDINGS_SMTP_HOST")) and bool(app.config.get("NTC_RECORDINGS_EMAIL_FROM"))


def _send_recording_email(
    app: Flask,
    row: sqlite3.Row,
    candidate: RecordingCandidate,
    share_url: str,
    email_message: str,
) -> tuple[bool, str]:
    if not _email_enabled(app):
        return False, "email disabled"
    subject = "NTC Newark Recording Request"
    body = _recording_email_html(app, row, candidate, share_url, email_message)
    message = MIMEText(body, "html")
    message["Subject"] = subject
    message["From"] = app.config["NTC_RECORDINGS_EMAIL_FROM"]
    recipients = [email for email in (row["email"], row["secondary_email"]) if email]
    message["To"] = ", ".join(recipients)
    try:
        with smtplib.SMTP(app.config["NTC_RECORDINGS_SMTP_HOST"], app.config["NTC_RECORDINGS_SMTP_PORT"], timeout=12) as smtp:
            if str(app.config.get("NTC_RECORDINGS_SMTP_STARTTLS", "1")).strip().lower() not in {"0", "false", "no", "off"}:
                smtp.starttls()
            username = app.config.get("NTC_RECORDINGS_SMTP_USERNAME")
            password = app.config.get("NTC_RECORDINGS_SMTP_PASSWORD")
            if username and password:
                smtp.login(username, password)
            smtp.send_message(message, to_addrs=recipients)
        return True, ""
    except Exception as exc:  # pragma: no cover - depends on external SMTP
        app.logger.exception("failed to send recording request email")
        return False, str(exc)


def _default_recording_email_message(row: sqlite3.Row, candidate: RecordingCandidate) -> str:
    selection_label = "Worship folder" if candidate.target_type == "folder" else f"{_recording_kind_label(candidate.kind)} recording"
    return (
        "Praise the Lord,\n\n"
        f"Your requested recording from {_format_date(row['requested_date'])} is ready.\n\n"
        "Please use the link below to listen to the recording.\n\n"
        f"{selection_label}: {candidate.title}\n\n"
        "God bless,\n"
        "NTC Newark"
    )


def _normalize_recording_email_message(value: str) -> str:
    message = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    message = message.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    return message.strip()


def _recording_email_html(
    app: Flask,
    row: sqlite3.Row,
    candidate: RecordingCandidate,
    share_url: str,
    email_message: str,
) -> str:
    logo_url = app.config.get("NTC_RECORDINGS_LOGO_URL", "")
    safe_logo = html.escape(str(logo_url), quote=True)
    safe_share_url = html.escape(share_url, quote=True)
    safe_title = html.escape(candidate.title)
    safe_date = html.escape(_format_date(row["requested_date"]))
    normalized_message = _normalize_recording_email_message(email_message or _default_recording_email_message(row, candidate))
    safe_message = html.escape(normalized_message).replace("\n", "<br>")
    return f"""
    <!doctype html>
    <html style="margin:0;padding:0;width:100%;height:100%;background:#06101d;">
      <body bgcolor="#06101d" style="margin:0;padding:0;width:100%;height:100%;min-height:100%;background:#06101d;font-family:Arial,Helvetica,sans-serif;color:#edf7ff;">
        <table width="100%" height="100%" role="presentation" bgcolor="#06101d" style="width:100%;height:100%;min-height:100%;border-collapse:collapse;background:#06101d;background-image:linear-gradient(145deg,#050913 0%,#0b1a2c 55%,#102a3c 100%);color:#edf7ff;">
          <tr>
            <td align="center" valign="top" bgcolor="#06101d" style="padding:48px 16px 96px;background:#06101d;background-image:radial-gradient(circle at 12% 0%,rgba(143,211,255,.22),transparent 360px),radial-gradient(circle at 95% 18%,rgba(116,221,180,.13),transparent 340px);">
              <div style="max-width:760px;border:1px solid rgba(143,211,255,.24);border-radius:28px;background:#091422;box-shadow:0 28px 90px rgba(0,0,0,.36);overflow:hidden;">
                <div style="padding:34px 34px 24px;text-align:center;background:#0c1b2c;background-image:radial-gradient(circle at top left,rgba(143,211,255,.22),transparent 320px),radial-gradient(circle at bottom right,rgba(116,221,180,.16),transparent 300px);">
                  <img src="{safe_logo}" style="width:126px;height:auto;margin-bottom:18px;" alt="NTC Newark">
                  <div style="font-size:12px;letter-spacing:.18em;text-transform:uppercase;color:#8fd3ff;font-weight:800;">NTC Newark</div>
                  <h1 style="font-size:32px;line-height:1.05;margin:12px 0 0;color:#edf7ff;">Your Recording Is Ready</h1>
                </div>
                <div style="padding:30px 34px 36px;text-align:left;">
                  <p style="font-size:18px;line-height:1.58;margin:0 0 22px;color:#dbeaff;">{safe_message}</p>
                  <div style="border:1px solid rgba(143,211,255,.18);border-radius:18px;background:#0f2033;padding:16px;margin-bottom:24px;">
                    <div style="font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:#9fb2c6;font-weight:800;">Requested Date</div>
                    <div style="font-size:17px;margin-top:5px;color:#edf7ff;font-weight:800;">{safe_date}</div>
                    <div style="font-size:14px;margin-top:8px;color:#9fb2c6;">{safe_title}</div>
                  </div>
                  <div style="text-align:center;">
                    <a href="{safe_share_url}" style="display:inline-block;padding:14px 22px;background:linear-gradient(135deg,#8fd3ff,#8ff5c8);color:#06101d;text-decoration:none;border-radius:14px;font-weight:900;">Open Recording</a>
                  </div>
                  <p style="font-size:13px;line-height:1.5;color:#9fb2c6;margin:24px 0 0;text-align:center;">If the button does not open, copy and paste this link into your browser:<br><span style="word-break:break-all;color:#8fd3ff;">{safe_share_url}</span></p>
                </div>
              </div>
            </td>
          </tr>
          <tr><td bgcolor="#06101d" style="height:100%;background:#06101d;">&nbsp;</td></tr>
        </table>
      </body>
    </html>
    """


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _format_date(value: str | None) -> str:
    if not value:
        return "Not set"
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def _format_datetime(value: str | None) -> str:
    if not value:
        return "Not yet"
    raw = str(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo:
        parsed = parsed.astimezone()
    date_part = f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
    time_part = parsed.strftime("%I:%M %p").lstrip("0")
    return f"{date_part} at {time_part}"


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


RECORDING_PUBLIC_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ title }}</title>
    <style>
      :root {
        color-scheme: dark;
        --bg:#080d18;
        --surface:rgba(12,22,38,.9);
        --surface-2:rgba(19,35,58,.9);
        --surface-3:rgba(19,35,58,.9);
        --line:rgba(144,202,255,.2);
        --line-soft:rgba(144,202,255,.2);
        --line-strong:rgba(143,211,255,.42);
        --text:#eef7ff;
        --muted:#a4b4c8;
        --accent:#8fd3ff;
        --good:#7be4bb;
        --good-soft:rgba(123,228,187,.12);
        --bad:#ffaaaa;
        --bad-soft:rgba(255,154,154,.1);
        --mono:"IBM Plex Mono","SFMono-Regular",Consolas,monospace;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        color: var(--text);
        background:radial-gradient(circle at 10% 0%, rgba(143,211,255,.2), transparent 28rem), linear-gradient(145deg,#050913,var(--bg));
        font-family:ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      body::before {
        content: "";
        position: fixed;
        inset: 0;
        z-index: -1;
        pointer-events: none;
        background: url("{{ recordings_url_for('ntc_brand_background') }}") center / min(1120px, 118vw) auto no-repeat;
        opacity: 0.31;
        filter: saturate(1.08) contrast(1.04);
      }
      main { width: min(1060px, calc(100vw - 32px)); margin: 0 auto; padding: 34px 0 48px; }
      h1, h2, p { margin: 0; }
      h1 { font-size: clamp(34px, 5.4vw, 66px); letter-spacing: -0.055em; line-height: 0.94; }
      h2 { font-size: 1.25rem; letter-spacing: 0; }
      .eyebrow { color: var(--accent); font: 800 0.78rem var(--mono); letter-spacing: 0.2em; text-transform: uppercase; }
      .hero { display: grid; gap: 0.75rem; margin-bottom: 1.2rem; }
      .hero p { max-width: 44rem; color: var(--muted); font-size: 1.05rem; line-height: 1.5; }
      .grid { display: grid; grid-template-columns: minmax(0, 1.14fr) minmax(18rem, 0.72fr); gap: 1rem; align-items: start; }
      .card {
        border: 1px solid var(--line);
        border-radius: 28px;
        background: var(--surface);
        box-shadow: 0 22px 70px rgba(0, 0, 0, 0.36);
        padding: clamp(18px, 2.4vw, 30px);
      }
      form { display: grid; gap: 0.9rem; }
      .form-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.9rem; }
      .wide { grid-column:1 / -1; }
      label { display: grid; gap: 0.34rem; color: var(--muted); font-weight: 800; }
      .optional { color: #7890a8; font-weight: 750; }
      input, textarea, select, button {
        width: 100%;
        border: 1px solid var(--line);
        border-radius: 16px;
        background: var(--surface-3);
        color: var(--text);
        padding: 0.86rem 0.95rem;
        font: inherit;
      }
      textarea { min-height: 7rem; resize: vertical; }
      select { cursor: pointer; padding-right: 2.35rem; }
      select option { background:#13233a; color:var(--text); }
      button {
        cursor: pointer;
        border-color: rgba(143, 211, 255, 0.42);
        background: linear-gradient(135deg, rgba(143, 211, 255, 0.25), rgba(123, 228, 187, 0.14));
        color: var(--text);
        font-weight: 900;
      }
      button:disabled, select:disabled { opacity: 0.55; cursor: not-allowed; }
      .banner { margin-bottom: 1rem; border: 1px solid rgba(123, 228, 187, 0.35); background: var(--good-soft); color: var(--good); border-radius: 16px; padding: 0.9rem; font-weight: 800; }
      .banner.error { border-color: rgba(255, 154, 154, 0.4); background: var(--bad-soft); color: var(--bad); }
      .steps { display: grid; gap: 0.8rem; margin-top: 1rem; }
      .step { border: 1px solid var(--line); border-radius: 18px; background: rgba(255,255,255,0.035); padding: 0.9rem; }
      .step strong { display: block; margin-bottom: 0.2rem; color: var(--text); }
      .step span { color: var(--muted); line-height: 1.45; }
      .date-summary {
        margin-top: .9rem;
        border: 1px solid rgba(143, 211, 255, 0.16);
        border-radius: 18px;
        background: rgba(143, 211, 255, 0.055);
        padding: .9rem;
        color: var(--muted);
        line-height: 1.45;
      }
      .meta { color: var(--muted); font: 800 0.72rem var(--mono); letter-spacing: 0.08em; text-transform: uppercase; }
      .muted { color: var(--muted); margin-top: 0.8rem; }
      .hint { color:var(--muted); font-size:.9rem; line-height:1.45; margin-top:-.35rem; }
      .decision-row {
        grid-column: 1 / -1;
        display: grid;
        grid-template-columns: minmax(0, 1fr);
        gap: .9rem;
        padding-bottom: .25rem;
      }
      .calendar-field { display:grid; gap:.42rem; }
      .calendar-picker {
        border:1px solid var(--line);
        border-radius:20px;
        background:rgba(5,13,24,.52);
        padding:.78rem;
        box-shadow:inset 0 1px 0 rgba(255,255,255,.04);
      }
      .calendar-top {
        display:grid;
        grid-template-columns:2.45rem minmax(0,1fr) 2.45rem;
        align-items:center;
        gap:.55rem;
        margin-bottom:.68rem;
      }
      .calendar-nav {
        display:grid;
        place-items:center;
        width:2.45rem;
        height:2.45rem;
        padding:0;
        border-radius:999px;
        font-size:1.2rem;
        line-height:1;
      }
      .calendar-heading { min-width:0; text-align:center; }
      .calendar-month {
        display:inline-flex;
        justify-content:center;
        max-width:100%;
        min-height:auto;
        border:0;
        border-radius:999px;
        background:transparent;
        padding:.08rem .5rem;
        color:var(--text);
        font-size:1.22rem;
        font-weight:900;
        letter-spacing:0;
        line-height:1.2;
      }
      .calendar-month:hover,
      .calendar-month:focus-visible {
        background:rgba(143,211,255,.12);
        outline:1px solid rgba(143,211,255,.34);
      }
      .calendar-selected { display:block; margin-top:.1rem; color:var(--muted); font-size:.86rem; line-height:1.3; }
      .calendar-jump {
        display:grid;
        grid-template-columns:repeat(2,minmax(0,1fr));
        gap:.45rem;
        margin-top:.58rem;
        text-align:left;
      }
      .calendar-jump[hidden] { display:none; }
      .calendar-jump label {
        display:grid;
        gap:.22rem;
        color:var(--muted);
        font:900 .68rem var(--mono);
        letter-spacing:.08em;
        text-transform:uppercase;
      }
      .calendar-jump select {
        min-height:2.75rem;
        border-radius:12px;
        padding:.52rem .7rem;
        font:900 1rem var(--sans);
        letter-spacing:0;
        text-transform:none;
      }
      .calendar-jump select option { font-size:1rem; }
      .calendar-weekdays,
      .calendar-grid {
        display:grid;
        grid-template-columns:repeat(7,minmax(0,1fr));
        gap:.34rem;
      }
      .calendar-weekdays {
        margin-bottom:.34rem;
        color:var(--muted);
        font:800 .58rem var(--mono);
        letter-spacing:.08em;
        text-align:center;
        text-transform:uppercase;
      }
      .calendar-day {
        min-height:3.05rem;
        aspect-ratio:1 / 1;
        display:grid;
        align-content:center;
        justify-items:center;
        gap:.08rem;
        padding:.28rem;
        border-radius:13px;
        background:rgba(255,255,255,.035);
        color:var(--muted);
        font-weight:900;
        line-height:1;
      }
      .calendar-day small {
        display:block;
        max-width:100%;
        overflow:hidden;
        text-overflow:ellipsis;
        white-space:nowrap;
        color:inherit;
        font-size:.54rem;
        font-weight:800;
      }
      .calendar-day.is-empty {
        visibility:hidden;
        pointer-events:none;
      }
      .calendar-day.is-unavailable,
      .calendar-day:disabled {
        opacity:.42;
        cursor:not-allowed;
        color:#65788f;
        border-color:rgba(144,202,255,.12);
        background:rgba(255,255,255,.018);
      }
      .calendar-day.is-available {
        color:var(--text);
        border-color:rgba(143,211,255,.36);
        background:linear-gradient(135deg,rgba(143,211,255,.18),rgba(123,228,187,.1));
      }
      .calendar-day.is-selected {
        color:#06101d;
        border-color:rgba(143,245,200,.9);
        background:linear-gradient(135deg,#8fd3ff,#8ff5c8);
        box-shadow:0 10px 24px rgba(0,0,0,.22);
      }
      .calendar-empty {
        grid-column:1 / -1;
        border:1px dashed rgba(143,211,255,.24);
        border-radius:14px;
        padding:1rem;
        color:var(--muted);
        text-align:center;
      }
      @media (max-width: 840px) { .grid, .form-grid { grid-template-columns: 1fr; } .wide { grid-column:auto; } }
      @media (max-width: 840px) {
        .decision-row { grid-template-columns: 1fr; }
        .calendar-picker { padding:.65rem; }
        .calendar-month { font-size:1.08rem; padding:.08rem .6rem; }
        .calendar-day { min-height:2.65rem; border-radius:11px; }
        .calendar-day small { display:none; }
        .calendar-jump label { font-size:.64rem; }
        .calendar-jump select { min-height:2.45rem; font-size:.94rem; padding:.45rem .58rem; }
      }
    </style>
  </head>
  <body>
    <main>
      <section class="hero">
        <div class="eyebrow">NTC Newark</div>
        <h1>Recording Requests</h1>
        <p>Choose an available service date and we will email an approved private recording link.</p>
      </section>
      {% if message %}<div class="banner">{{ message }}</div>{% endif %}
      {% if error %}<div class="banner error">{{ error }}</div>{% endif %}
      <div class="grid">
        <section class="card">
          <h2>Request a Recording</h2>
          <form method="post" action="{{ recordings_url_for('create_request') }}">
            <div class="form-grid">
              <div class="decision-row">
                <label>
                  Recording Type
                  <select name="recording_kind" required>
                    <option value="message">Message recording</option>
                    <option value="worship">Worship recordings</option>
                    <option value="testimony">Testimony recording</option>
                    <option value="unsure">Not sure</option>
                  </select>
                </label>
                <div class="calendar-field">
                  <label for="requested-date-value">Service Date</label>
                  <input id="requested-date-value" name="requested_date" type="hidden">
                  <div class="calendar-picker" data-calendar>
                    <div class="calendar-top">
                      <button class="calendar-nav" type="button" data-calendar-prev aria-label="Previous month">&lsaquo;</button>
                      <div class="calendar-heading">
                        <button class="calendar-month" type="button" data-calendar-jump-toggle aria-expanded="false" aria-controls="calendar-jump-controls" data-calendar-month>Choose service date</button>
                        <span class="calendar-selected" data-calendar-selected>Available days are highlighted.</span>
                        <div class="calendar-jump" id="calendar-jump-controls" data-calendar-jump hidden>
                          <label>Month <select data-calendar-month-select aria-label="Jump to month"></select></label>
                          <label>Year <select data-calendar-year-select aria-label="Jump to year"></select></label>
                        </div>
                      </div>
                      <button class="calendar-nav" type="button" data-calendar-next aria-label="Next month">&rsaquo;</button>
                    </div>
                    <div class="calendar-weekdays" aria-hidden="true">
                      <span>Sun</span><span>Mon</span><span>Tue</span><span>Wed</span><span>Thu</span><span>Fri</span><span>Sat</span>
                    </div>
                    <div class="calendar-grid" data-calendar-grid role="group" aria-label="Available service dates"></div>
                  </div>
                </div>
              </div>
              <p class="hint wide">Greyed-out days are not available for the selected recording type.</p>
              <label>First and Last Name <input name="requester_name" autocomplete="name" required></label>
              <label>Email <input name="email" type="email" autocomplete="email" required></label>
              <label>Send Copy To <span class="optional">Optional</span><input name="secondary_email" type="email" autocomplete="email" placeholder="Optional"></label>
              <label>Phone Number <span class="optional">Optional</span><input name="phone" autocomplete="tel" placeholder="Optional"></label>
              <label class="wide">Additional Instructions <textarea name="notes" placeholder="Optional"></textarea></label>
            </div>
            <button type="submit" {% if not recording_dates %}disabled{% endif %}>Send Request</button>
          </form>
          {% if not recording_dates %}
            <p class="muted">No recordings are available right now.</p>
          {% endif %}
        </section>
        <section class="card">
          <h2>How Requests Work</h2>
          <div class="steps">
            <div class="step">
              <strong>1. Choose the recording type</strong>
                  <span>Pick message, worship, testimony, or not sure before choosing a service date.</span>
            </div>
            <div class="step">
              <strong>2. Choose the service date</strong>
              <span>Only dates with available recordings for that type can be selected.</span>
            </div>
            <div class="step">
              <strong>3. Check your email</strong>
              <span>Once approved, the recording link is sent to the email address provided.</span>
            </div>
          </div>
          <div class="date-summary">
            <div class="meta">Available Dates</div>
            <p>{{ recording_dates|length }} recording date{{ "" if recording_dates|length == 1 else "s" }} currently available.</p>
          </div>
        </section>
      </div>
    </main>
    <script type="application/json" id="recording-date-data">{{ recording_dates|tojson }}</script>
    <script>
      (() => {
        const form = document.querySelector('form[action="{{ recordings_url_for('create_request') }}"]');
        const kindSelect = document.querySelector('select[name="recording_kind"]');
        const dateInput = document.querySelector('input[name="requested_date"]');
        const dataScript = document.getElementById("recording-date-data");
        const monthLabel = document.querySelector("[data-calendar-month]");
        const selectedLabel = document.querySelector("[data-calendar-selected]");
        const grid = document.querySelector("[data-calendar-grid]");
        const prevButton = document.querySelector("[data-calendar-prev]");
        const nextButton = document.querySelector("[data-calendar-next]");
        const jumpToggle = document.querySelector("[data-calendar-jump-toggle]");
        const jumpPanel = document.querySelector("[data-calendar-jump]");
        const monthSelect = document.querySelector("[data-calendar-month-select]");
        const yearSelect = document.querySelector("[data-calendar-year-select]");
        if (!form || !kindSelect || !dateInput || !dataScript || !monthLabel || !selectedLabel || !grid || !prevButton || !nextButton || !jumpToggle || !jumpPanel || !monthSelect || !yearSelect) return;

        let dateOptions = [];
        try {
          dateOptions = JSON.parse(dataScript.textContent || "[]");
        } catch {
          dateOptions = [];
        }
        const optionByDate = new Map(dateOptions.map((option) => [option.date, option]));
        const monthFormatter = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });
        const monthNameFormatter = new Intl.DateTimeFormat(undefined, { month: "long" });
        const monthNames = Array.from({ length: 12 }, (_, month) => monthNameFormatter.format(new Date(2024, month, 1)));
        const parseDate = (value) => {
          const [year, month, day] = String(value || "").split("-").map(Number);
          return new Date(year || 1970, (month || 1) - 1, day || 1);
        };
        const toDateKey = (date) => {
          const year = date.getFullYear();
          const month = String(date.getMonth() + 1).padStart(2, "0");
          const day = String(date.getDate()).padStart(2, "0");
          return `${year}-${month}-${day}`;
        };
        const relevantOptions = () => {
          const kind = kindSelect.value;
          return dateOptions.filter((option) => kind === "unsure" || (option.kinds || []).includes(kind));
        };
        const firstMonthForKind = () => {
          const first = relevantOptions()[0] || dateOptions[0];
          const date = first ? parseDate(first.date) : new Date();
          return new Date(date.getFullYear(), date.getMonth(), 1);
        };
        let selectedDate = "";
        let currentMonth = firstMonthForKind();

        const setSelectedDate = (dateKey) => {
          const option = optionByDate.get(dateKey);
          selectedDate = option ? dateKey : "";
          dateInput.value = selectedDate;
          selectedLabel.textContent = option ? option.label : "Available days are highlighted.";
        };

        const setJumpOpen = (isOpen) => {
          jumpPanel.hidden = !isOpen;
          jumpToggle.setAttribute("aria-expanded", String(isOpen));
        };

        const syncJumpControls = (available) => {
          const currentYear = currentMonth.getFullYear();
          const years = Array.from(new Set(available.map((option) => parseDate(option.date).getFullYear())))
            .sort((a, b) => b - a);
          if (!years.includes(currentYear)) {
            years.unshift(currentYear);
          }

          monthSelect.replaceChildren(...monthNames.map((name, month) => {
            const option = document.createElement("option");
            option.value = String(month);
            option.textContent = name;
            return option;
          }));
          yearSelect.replaceChildren(...years.map((year) => {
            const option = document.createElement("option");
            option.value = String(year);
            option.textContent = String(year);
            return option;
          }));
          monthSelect.value = String(currentMonth.getMonth());
          yearSelect.value = String(currentYear);
          const disabled = !available.length;
          monthSelect.disabled = disabled;
          yearSelect.disabled = disabled;
          jumpToggle.disabled = disabled;
        };

        const jumpToSelectedMonth = () => {
          const year = Number(yearSelect.value) || currentMonth.getFullYear();
          const month = Number(monthSelect.value);
          currentMonth = new Date(year, Number.isFinite(month) ? month : currentMonth.getMonth(), 1);
          renderCalendar();
        };

        const renderCalendar = () => {
          const available = relevantOptions();
          const availableDates = new Set(available.map((option) => option.date));
          if (selectedDate && !availableDates.has(selectedDate)) {
            setSelectedDate("");
          }
          if (!availableDates.size) {
            grid.replaceChildren();
            const empty = document.createElement("div");
            empty.className = "calendar-empty";
            empty.textContent = "No service dates are available for this recording type.";
            grid.appendChild(empty);
            monthLabel.textContent = "No available dates";
            selectedLabel.textContent = "Choose another recording type.";
            syncJumpControls(available);
            return;
          }

          monthLabel.textContent = monthFormatter.format(currentMonth);
          syncJumpControls(available);
          grid.replaceChildren();
          const year = currentMonth.getFullYear();
          const month = currentMonth.getMonth();
          const firstDay = new Date(year, month, 1).getDay();
          const daysInMonth = new Date(year, month + 1, 0).getDate();
          for (let index = 0; index < firstDay; index += 1) {
            const spacer = document.createElement("button");
            spacer.type = "button";
            spacer.className = "calendar-day is-empty";
            spacer.tabIndex = -1;
            grid.appendChild(spacer);
          }
          for (let day = 1; day <= daysInMonth; day += 1) {
            const dateKey = toDateKey(new Date(year, month, day));
            const option = optionByDate.get(dateKey);
            const availableForKind = availableDates.has(dateKey);
            const dayButton = document.createElement("button");
            dayButton.type = "button";
            dayButton.className = `calendar-day ${availableForKind ? "is-available" : "is-unavailable"} ${dateKey === selectedDate ? "is-selected" : ""}`;
            dayButton.disabled = !availableForKind;
            dayButton.setAttribute("aria-label", option ? option.label : `${month + 1}/${day}/${year} unavailable`);
            dayButton.innerHTML = `<span>${day}</span>`;
            if (availableForKind) {
              dayButton.addEventListener("click", () => {
                setSelectedDate(dateKey);
                renderCalendar();
              });
            }
            grid.appendChild(dayButton);
          }
        };

        prevButton.addEventListener("click", () => {
          currentMonth = new Date(currentMonth.getFullYear(), currentMonth.getMonth() - 1, 1);
          renderCalendar();
        });
        nextButton.addEventListener("click", () => {
          currentMonth = new Date(currentMonth.getFullYear(), currentMonth.getMonth() + 1, 1);
          renderCalendar();
        });
        jumpToggle.addEventListener("click", () => setJumpOpen(jumpPanel.hidden));
        monthSelect.addEventListener("change", jumpToSelectedMonth);
        yearSelect.addEventListener("change", jumpToSelectedMonth);
        kindSelect.addEventListener("change", () => {
          currentMonth = firstMonthForKind();
          renderCalendar();
        });
        form.addEventListener("submit", (event) => {
          if (!dateInput.value) {
            event.preventDefault();
            selectedLabel.textContent = "Choose a highlighted service date before sending.";
          }
        });
        renderCalendar();
      })();
    </script>
  </body>
</html>
"""


RECORDING_ADMIN_LOGIN_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ title }} Admin</title>
    <style>
      :root { color-scheme: dark; --bg:#08111d; --surface:#101d30; --surface-2:#14243a; --line:rgba(144,202,255,.22); --text:#eef7ff; --muted:#a8b6c8; --accent:#8fd3ff; --bad:#ffaaaa; --bad-soft:rgba(255,154,154,.1); }
      * { box-sizing: border-box; }
      html { min-height:100%; background:#050913; }
      body { margin:0; min-height:100vh; min-height:100svh; display:grid; place-items:center; padding:16px; background:radial-gradient(circle at top left, rgba(143,211,255,.2), transparent 26rem), linear-gradient(145deg,#050913,var(--bg)); color:var(--text); font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
      main { width:min(520px, calc(100vw - 32px)); }
      section { border:1px solid var(--line); border-radius:28px; background:var(--surface); padding:30px; box-shadow:0 24px 80px rgba(0,0,0,.38); }
      h1 { margin:0 0 .5rem; font-size:clamp(32px,5vw,52px); line-height:1; letter-spacing:-.05em; }
      p { margin:0 0 1rem; color:var(--muted); }
      form { display:grid; gap:.8rem; }
      input, button { border:1px solid var(--line); border-radius:16px; background:var(--surface-2); color:var(--text); padding:.9rem; font:inherit; }
      button { cursor:pointer; font-weight:900; background:rgba(143,211,255,.16); }
      .error { border:1px solid rgba(255,154,154,.35); border-radius:16px; background:var(--bad-soft); color:var(--bad); margin-bottom:1rem; padding:.75rem .85rem; font-weight:800; }
    </style>
  </head>
  <body>
    <main>
      <section>
        <h1>Recording Admin</h1>
        <p>Review recording requests and send approved links.</p>
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="post">
          <input type="password" name="password" placeholder="Admin password" autocomplete="current-password" autofocus required>
          <button type="submit">Open Panel</button>
        </form>
      </section>
    </main>
  </body>
</html>
"""


RECORDING_ADMIN_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ title }} Admin</title>
    <style>
      :root {
        color-scheme: dark;
        --bg:#07121e;
        --surface:rgba(10,21,36,.92);
        --surface-2:rgba(18,34,53,.9);
        --surface-3:rgba(6,13,24,.58);
        --line:rgba(143,211,255,.2);
        --line-soft:rgba(143,211,255,.14);
        --line-strong:rgba(143,211,255,.34);
        --text:#edf7ff;
        --muted:#9fb2c6;
        --accent:#8fd3ff;
        --good:#74ddb4;
        --good-soft:rgba(116,221,180,.1);
        --warn:#ffc875;
        --warn-soft:rgba(255,200,117,.1);
        --bad:#ffaaa8;
        --bad-soft:rgba(255,170,168,.1);
        --ink:#06101d;
        --shadow:0 22px 70px rgba(0,0,0,.34);
        --mono:ui-monospace,"SFMono-Regular",Consolas,monospace;
      }
      * { box-sizing:border-box; }
      body {
        margin:0;
        min-height:100vh;
        color:var(--text);
        background:
          radial-gradient(circle at 10% 0%, rgba(143,211,255,.2), transparent 28rem),
          linear-gradient(145deg,#050913,var(--bg)),
          var(--bg);
        font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      }
      main { width:min(1400px, calc(100vw - 32px)); margin:0 auto; padding:30px 0 44px; }
      header { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:1rem; margin-bottom:1rem; align-items:start; }
      h1, h2, p { margin:0; }
      h1 { font-size:clamp(34px,5.2vw,64px); line-height:.95; letter-spacing:-.055em; }
      h2 { font-size:1.2rem; letter-spacing:-.02em; }
      .eyebrow, .meta {
        color:var(--accent);
        font:800 .72rem var(--mono);
        letter-spacing:.12em;
        text-transform:uppercase;
      }
      .eyebrow + h1 { margin-top:.38rem; }
      .actions { display:flex; gap:.5rem; flex-wrap:wrap; justify-content:flex-end; }
      a, button, select {
        border:1px solid var(--line);
        border-radius:14px;
        background:var(--surface-2);
        color:var(--text);
        padding:.72rem .9rem;
        text-decoration:none;
        font:inherit;
        font-weight:800;
      }
      button { cursor:pointer; }
      button:hover, a:hover, select:hover { border-color:var(--line-strong); }
      .tabs {
        display:inline-grid;
        grid-template-columns:repeat(2,minmax(0,auto));
        gap:.28rem;
        margin:.9rem 0;
        padding:.28rem;
        border:1px solid var(--line);
        border-radius:999px;
        background:rgba(5,13,24,.58);
        box-shadow:inset 0 1px 0 rgba(255,255,255,.04);
      }
      .tab {
        display:flex;
        align-items:center;
        gap:.44rem;
        border:1px solid transparent;
        border-radius:999px;
        background:transparent;
        color:var(--muted);
        padding:.5rem .72rem;
        font-size:.84rem;
        line-height:1;
        transition:background .16s ease, border-color .16s ease, color .16s ease, transform .16s ease;
      }
      .tab:hover { color:var(--text); border-color:rgba(143,211,255,.22); background:rgba(143,211,255,.06); transform:translateY(-1px); }
      .tab.active {
        color:var(--text);
        background:linear-gradient(135deg,rgba(143,211,255,.18),rgba(143,245,200,.12));
        border-color:rgba(143,211,255,.42);
        box-shadow:0 10px 26px rgba(8,19,33,.26), inset 0 0 0 1px rgba(255,255,255,.04);
      }
      .tab strong {
        display:inline-grid;
        place-items:center;
        min-width:1.45rem;
        min-height:1.45rem;
        padding:0 .34rem;
        border-radius:999px;
        background:rgba(143,211,255,.1);
        color:inherit;
        font-size:.76rem;
      }
      .tab.active strong { background:linear-gradient(135deg,#8fd3ff,#8ff5c8); color:var(--ink); }
      .metrics {
        display:grid;
        grid-template-columns:repeat(4,minmax(0,1fr));
        gap:.65rem;
        margin:.65rem 0 1rem;
      }
      .metric {
        border:1px solid var(--line);
        border-radius:18px;
        background:rgba(5,13,24,.58);
        padding:.78rem .85rem;
        min-width:0;
      }
      .metric span {
        display:block;
        color:var(--muted);
        font:800 .64rem var(--mono);
        letter-spacing:.12em;
        text-transform:uppercase;
      }
      .metric strong { display:block; margin-top:.24rem; font-size:1.42rem; line-height:1; letter-spacing:-.04em; }
      .metric small { display:block; margin-top:.32rem; color:var(--muted); line-height:1.35; overflow:visible; text-overflow:clip; white-space:normal; }
      .grid { display:grid; gap:1rem; align-items:start; }
      .card {
        border:1px solid var(--line);
        border-radius:24px;
        background:var(--surface);
        padding:1rem;
        box-shadow:var(--shadow);
      }
      .section-head { display:flex; justify-content:space-between; align-items:end; gap:1rem; margin-bottom:.85rem; flex-wrap:wrap; }
      .section-head p { max-width:46rem; margin-top:.28rem; color:var(--muted); line-height:1.45; }
      .request-groups { display:grid; gap:1rem; }
      .request-group { display:grid; gap:.55rem; }
      .request-group-head {
        display:flex;
        align-items:center;
        justify-content:space-between;
        gap:1rem;
        padding:.35rem .2rem .1rem;
        border-bottom:1px solid rgba(143,211,255,.12);
      }
      .request-group-head h3 { margin:0; font-size:1.05rem; letter-spacing:-.015em; }
      .request-group-count { color:var(--muted); font:800 .68rem var(--mono); letter-spacing:.08em; text-transform:uppercase; }
      .request-list { display:grid; gap:.55rem; }
      .request-table-head {
        display:grid;
        grid-template-columns:minmax(12rem,1.05fr) minmax(8rem,.58fr) minmax(16rem,1.3fr) minmax(12rem,.86fr) minmax(5.3rem,.36fr);
        gap:.78rem;
        padding:0 .9rem .25rem;
        color:var(--muted);
        font:800 .62rem var(--mono);
        letter-spacing:.12em;
        text-transform:uppercase;
      }
      .request {
        border:1px solid rgba(143,211,255,.16);
        border-radius:16px;
        background:linear-gradient(135deg,rgba(255,255,255,.04),rgba(143,211,255,.018));
        overflow:hidden;
        transition:border-color .18s ease, background .18s ease, transform .18s ease;
      }
      .request:hover { border-color:var(--line-strong); transform:translateY(-1px); background:linear-gradient(135deg,rgba(255,255,255,.055),rgba(143,211,255,.035)); }
      .request[open] { border-color:var(--line-strong); }
      .request.sent, .request.ready { border-color:rgba(116,221,180,.26); }
      .request.revoked { opacity:.78; border-color:rgba(255,170,168,.24); }
      .request.archived { border-color:rgba(159,178,198,.24); }
      .request summary { list-style:none; cursor:pointer; padding:.78rem .86rem; }
      .request summary::-webkit-details-marker { display:none; }
      .request[open] summary { border-bottom:1px solid var(--line); background:rgba(143,211,255,.035); }
      .request.completed-row summary { padding:.58rem .72rem; }
      .request.completed-row .request-head {
        grid-template-columns:minmax(11rem,1.05fr) minmax(7rem,.52fr) minmax(11rem,.78fr) minmax(9.5rem,.66fr) minmax(5.1rem,.32fr);
        gap:.55rem;
      }
      .request.completed-row .request-title strong { font-size:1rem; }
      .request.completed-row .queue-label { margin-bottom:.12rem; font-size:.56rem; }
      .request.completed-row .queue-subvalue { font-size:.82rem; }
      .request.completed-row .open-hint { padding:.34rem .5rem; font-size:.62rem; }
      .request-head {
        display:grid;
        grid-template-columns:minmax(12rem,1.05fr) minmax(8rem,.58fr) minmax(16rem,1.3fr) minmax(12rem,.86fr) minmax(5.3rem,.36fr);
        align-items:center;
        gap:.78rem;
      }
      .request-title strong { display:block; font-size:1.1rem; }
      .request-subtitle { color:var(--muted); line-height:1.45; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .queue-cell { min-width:0; }
      .queue-label {
        display:block;
        margin-bottom:.18rem;
        color:var(--muted);
        font:800 .62rem var(--mono);
        letter-spacing:.12em;
        text-transform:uppercase;
      }
      .queue-value { display:block; color:var(--text); font-weight:850; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .queue-subvalue { display:block; margin-top:.12rem; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .submitted-cell .queue-value { overflow:visible; text-overflow:clip; white-space:normal; line-height:1.25; }
      .open-hint {
        display:inline-flex;
        align-items:center;
        justify-content:center;
        border:1px solid var(--line-strong);
        border-radius:999px;
        padding:.45rem .58rem;
        color:var(--accent);
        background:rgba(143,211,255,.075);
        font:900 .68rem var(--mono);
        letter-spacing:.12em;
        text-transform:uppercase;
      }
      .pill {
        border:1px solid var(--line);
        border-radius:999px;
        padding:.35rem .58rem;
        color:var(--muted);
        font:800 .68rem var(--mono);
        letter-spacing:.08em;
        text-transform:uppercase;
        justify-self:end;
      }
      .pill.pending { color:var(--warn); border-color:rgba(255,200,117,.35); background:var(--warn-soft); }
      .pill.sent, .pill.ready { color:var(--good); border-color:rgba(116,221,180,.34); background:var(--good-soft); }
      .pill.revoked { color:var(--bad); border-color:rgba(255,170,168,.35); background:var(--bad-soft); }
      .pill.archived { color:var(--muted); border-color:rgba(159,178,198,.28); }
      .request-body { display:grid; gap:1rem; padding:1.05rem 1.08rem 1.12rem; background:linear-gradient(180deg,rgba(143,211,255,.028),rgba(4,11,20,.12)); }
      .email-details summary {
        display:inline-flex;
        align-items:center;
        justify-content:center;
        gap:.45rem;
        width:max-content;
        max-width:100%;
        padding:.42rem .7rem;
        border:1px solid rgba(116,221,180,.3);
        border-radius:999px;
        background:rgba(116,221,180,.075);
        color:var(--good);
        font:900 .68rem var(--mono);
        letter-spacing:.12em;
        text-transform:uppercase;
        cursor:pointer;
      }
      .email-details summary::after {
        content:"";
        width:.42rem;
        height:.42rem;
        border-right:2px solid currentColor;
        border-bottom:2px solid currentColor;
        transform:rotate(45deg) translateY(-.08rem);
      }
      .email-details[open] summary { margin-bottom:.65rem; }
      .email-details[open] summary::after { transform:rotate(225deg) translateY(-.08rem); }
      .note-strip {
        border-top:1px solid rgba(143,211,255,.14);
        padding-top:.82rem;
      }
      .note-strip p { margin-top:.22rem; color:var(--muted); line-height:1.5; }
      .request-note {
        margin:0;
        border-top:1px solid var(--line);
        padding-top:.72rem;
        color:var(--muted);
        line-height:1.45;
      }
      .request-note strong { color:var(--text); }
      .action-panel {
        display:flex;
        justify-content:space-between;
        align-items:center;
        gap:1rem;
        flex-wrap:wrap;
        border:1px solid rgba(143,211,255,.2);
        border-radius:18px;
        background:rgba(143,211,255,.055);
        padding:.9rem;
      }
      .action-panel strong { display:block; margin-top:.18rem; }
      .action-panel p { margin-top:.18rem; color:var(--muted); line-height:1.45; }
      .approve-form {
        display:grid;
        gap:.82rem;
        border:1px solid rgba(116,221,180,.34);
        border-radius:20px;
        padding:.95rem;
        background:linear-gradient(135deg,rgba(116,221,180,.09),rgba(143,211,255,.055));
        box-shadow:inset 0 1px 0 rgba(255,255,255,.04);
      }
      .approval-head { display:flex; align-items:flex-start; justify-content:space-between; gap:1rem; }
      .approval-head strong { display:block; margin-top:.2rem; font-size:1.08rem; }
      .approval-head p { margin-top:.16rem; color:var(--muted); line-height:1.4; }
      .approve-grid { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:.8rem; align-items:end; }
      .approve-form label { display:grid; gap:.35rem; color:var(--muted); font-weight:850; }
      .approve-form select { width:100%; min-height:3.15rem; background:rgba(4,11,20,.56); }
      .approve-form option { background:#13233a; color:var(--text); }
      .approve-submit { display:flex; align-items:end; justify-content:flex-end; }
      .approve-submit button { width:auto; white-space:nowrap; color:#dcfff0; background:rgba(116,221,180,.15); border-color:rgba(116,221,180,.45); }
      .email-details { border-top:1px solid rgba(116,221,180,.18); padding-top:.65rem; }
      .request-actions { display:flex; gap:.55rem; flex-wrap:wrap; align-items:center; }
      .request-actions form { margin:0; }
      .action-panel .request-actions { margin-left:auto; }
      .danger { color:#ffd7d7; border-color:rgba(255,170,168,.35); background:var(--bad-soft); }
      .email-note { display:grid; gap:.35rem; color:var(--muted); font-weight:850; }
      .email-note textarea {
        min-height:7.25rem;
        resize:vertical;
        border:1px solid var(--line);
        border-radius:14px;
        background:rgba(4,11,20,.56);
        color:var(--text);
        padding:.82rem;
        font:inherit;
        line-height:1.45;
      }
      .muted { color:var(--muted); }
      .banner { margin-bottom:1rem; border:1px solid rgba(116,221,180,.35); background:var(--good-soft); color:var(--good); border-radius:16px; padding:.85rem; font-weight:850; }
      .banner.error { border-color:rgba(255,154,154,.4); background:var(--bad-soft); color:#ffaaaa; }
      @media (max-width:1100px) {
        .metrics { grid-template-columns:repeat(2,minmax(0,1fr)); }
        .request-table-head { display:none; }
        .request-head { grid-template-columns:minmax(0,1fr) minmax(0,1fr); align-items:start; }
        .approve-grid { grid-template-columns:1fr; }
        .approve-submit { justify-content:flex-start; }
        .pill, .open-hint { justify-self:start; }
      }
      @media (max-width:760px) {
        main { width:min(100vw - 24px, 1120px); padding-top:18px; }
        header { grid-template-columns:minmax(0,1fr) auto; gap:.65rem; }
        header > div:first-child { min-width:0; }
        header h1 { font-size:clamp(1.65rem, 8vw, 2.9rem); }
        header .muted { font-size:.86rem; }
        .section-head, .approval-head { display:flex; align-items:flex-start; flex-direction:column; }
        .actions { width:min(62vw, 14rem); justify-content:flex-end; flex-wrap:nowrap; gap:.35rem; }
        .actions > a, .actions > form { flex:1 1 0; min-width:0; }
        .actions > form > button, header .actions a { width:100%; min-width:0; }
        header .actions a, header .actions button { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; padding:.54rem .52rem; font-size:.78rem; border-radius:12px; }
        .tabs { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); border-radius:999px; width:100%; }
        .tab { justify-content:space-between; }
        .metrics, .request-head { grid-template-columns:1fr; }
        .request-body { padding:.78rem; }
        .action-panel, .approve-form { border-radius:14px; padding:.78rem; }
        .approve-form select { font-size:.88rem; }
        .email-note textarea { min-height:6.5rem; font-size:.9rem; }
        .action-panel { align-items:flex-start; }
        .action-panel .request-actions { margin-left:0; }
        .approve-submit, .approve-submit button, .request-actions, .request-actions a, .request-actions form, .request-actions button { width:100%; }
      }
    </style>
  </head>
  <body>
    <main>
      <header>
        <div>
          <div class="eyebrow">NTC Newark</div>
          <h1>Recording Requests</h1>
          <p class="muted">Approve message, worship, and testimony recording requests.</p>
        </div>
        <div class="actions">
          <a href="{{ recordings_url_for('testimony_review') }}">Recorder Review</a>
          <a href="{{ recordings_url_for('public_form') }}">Public Form</a>
          <form method="post" action="{{ recordings_url_for('admin_logout') }}"><button type="submit">Sign Out</button></form>
        </div>
      </header>
      {% if message %}<div class="banner">{{ message }}</div>{% endif %}
      {% if error %}<div class="banner error">{{ error }}</div>{% endif %}
      <section class="metrics" aria-label="Recording request status">
        <div class="metric"><span>Pending</span><strong>{{ pending_count }}</strong><small>Needs review</small></div>
        <div class="metric"><span>Completed</span><strong>{{ completed_count }}</strong><small>{{ active_count }} active · {{ closed_count + archived_count }} closed</small></div>
        <div class="metric"><span>Library</span><strong>{{ recording_count }}</strong><small>{{ recording_counts_by_kind.get("message", 0) }} messages · {{ recording_counts_by_kind.get("worship", 0) }} worship · {{ recording_counts_by_kind.get("testimony", 0) }} testimonies</small></div>
        <div class="metric"><span>Delivery</span><strong>{{ "Email" if email_enabled else "Link" }}</strong><small>{{ "Email delivery enabled" if email_enabled else "Manual approval" }}</small></div>
      </section>
      <nav class="tabs" aria-label="Request list">
        <a class="tab {{ 'active' if active_tab == 'pending' else '' }}" {% if active_tab == "pending" %}aria-current="page"{% endif %} href="{{ recordings_url_for('admin_panel', tab='pending') }}">Pending <strong>{{ pending_count }}</strong></a>
        <a class="tab {{ 'active' if active_tab == 'completed' else '' }}" {% if active_tab == "completed" %}aria-current="page"{% endif %} href="{{ recordings_url_for('admin_panel', tab='completed') }}">Completed <strong>{{ completed_count }}</strong></a>
      </nav>
      <div class="grid">
        <section class="card">
          <div class="section-head">
            <div>
              <h2>{{ tab_title }}</h2>
              <p class="muted">{{ tab_description }}</p>
              {% if active_tab == "completed" and auto_archive_days > 0 %}
                <p class="muted">Auto-archive is on: older revoked requests are marked archived but stay in Completed.</p>
              {% endif %}
            </div>
            <span class="pill">{{ requests|length }} request{{ "" if requests|length == 1 else "s" }}</span>
          </div>
	          {% if request_groups %}
	            <div class="request-groups">
	              {% for group in request_groups %}
	                <section class="request-group">
	                  <div class="request-group-head">
	                    <h3>{{ group.label }}</h3>
	                    <span class="request-group-count">{{ group.requests|length }} request{{ "" if group.requests|length == 1 else "s" }}</span>
	                  </div>
	                  <div class="request-table-head" aria-hidden="true">
	                    <span>{{ "Recipient" if active_tab == "completed" else "Requester" }}</span>
	                    <span>Date</span>
	                    <span>{{ "Status" if active_tab == "completed" else "Selection" }}</span>
	                    <span>{{ "Last Update" if active_tab == "completed" else "Submitted" }}</span>
	                    <span>Action</span>
	                  </div>
	                  <div class="request-list">
	                    {% for item in group.requests %}
	                      <details class="request {{ 'archived' if item.archived_at else item.status }} {{ 'completed-row' if active_tab == 'completed' else '' }}">
	                        <summary>
	                          <div class="request-head">
	                            <div class="request-title queue-cell">
	                              <span class="queue-label">{{ "Recipient" if active_tab == "completed" else "Requester" }}</span>
	                              <strong class="queue-value">{{ item.requester_name }}</strong>
	                              <span class="queue-subvalue">{{ item.email }}</span>
	                              {% if item.secondary_email %}
	                                <span class="queue-subvalue">CC {{ item.secondary_email }}</span>
	                              {% elif item.phone %}
	                                <span class="queue-subvalue">{{ item.phone }}</span>
	                              {% endif %}
	                            </div>
	                            <div class="queue-cell">
	                              <span class="queue-label">Date</span>
	                              <span class="queue-value">{{ format_date(item.requested_date) }}</span>
	                            </div>
	                            <div class="queue-cell">
	                              <span class="queue-label">{{ "Status" if active_tab == "completed" else "Selection" }}</span>
	                              <span class="queue-value">
	                                {% if active_tab == "completed" %}
	                                  {% if item.archived_at %}
	                                    Archived
	                                  {% elif item.status == "revoked" %}
	                                    Access revoked
	                                  {% elif item.status == "sent" %}
	                                    Link sent
	                                  {% else %}
	                                    Link prepared
	                                  {% endif %}
	                                {% else %}
	                                  {{ item.recording_title or "Selected by date" }}
	                                {% endif %}
	                              </span>
	                              {% if active_tab == "completed" %}
	                                <span class="queue-subvalue">{{ item.recording_title or "Selected by date" }}</span>
	                              {% endif %}
	                            </div>
	                            <div class="queue-cell submitted-cell">
	                              <span class="queue-label">{{ "Last Update" if active_tab == "completed" else "Submitted" }}</span>
	                              <span class="queue-value">{{ format_datetime(item.archived_at or item.revoked_at or item.sent_at or item.created_at) if active_tab == "completed" else format_datetime(item.created_at) }}</span>
	                            </div>
	                            <span class="open-hint">{{ "Manage" if active_tab == "completed" else "Review" }}</span>
	                          </div>
	                        </summary>
	                        <div class="request-body">
	                          {% if item.share_token and item.status != "revoked" %}
	                            <section class="action-panel">
	                              <div>
	                                <div class="meta">Active Share</div>
	                                <strong>{{ "Emailed link" if item.status == "sent" else "Prepared link" }}</strong>
	                                <p>This link stays active until access is revoked.</p>
	                              </div>
	                              <div class="request-actions">
	                                <a href="{{ item.share_url or recordings_url_for('share_recording', token=item.share_token) }}">Open prepared share link</a>
	                                <form method="post" action="{{ recordings_url_for('revoke_request_link', request_id=item.id) }}">
	                                  <input type="hidden" name="tab" value="{{ active_tab }}">
	                                  <button class="danger" type="submit">Revoke Access</button>
	                                </form>
	                              </div>
	                            </section>
	                          {% elif active_tab == "completed" and item.status == "revoked" %}
	                            <section class="action-panel">
	                              <div>
	                                <div class="meta">Closed Access</div>
	                                <strong>Access is revoked</strong>
	                                <p>This request is complete. Mark it archived only if you want the internal history flag.</p>
	                              </div>
	                              <div class="request-actions">
	                                <form method="post" action="{{ recordings_url_for('archive_request', request_id=item.id) }}">
	                                  <input type="hidden" name="tab" value="{{ active_tab }}">
	                                  <button type="submit">Archive</button>
	                                </form>
	                              </div>
	                            </section>
	                          {% endif %}
	                          {% if item.status in ["pending", "ready"] %}
	                            {% set candidates = candidates_by_request.get(item.id, []) %}
	                            {% if candidates %}
	                              <form class="approve-form" method="post" action="{{ recordings_url_for('send_request_link', request_id=item.id) }}">
	                                <input type="hidden" name="tab" value="{{ active_tab }}">
	                                <div class="approval-head">
	                                  <div>
	                                    <div class="meta">{{ "Delivery Retry" if item.status == "ready" else "Approval" }}</div>
	                                    <strong>{{ "Send prepared link by email" if item.status == "ready" else "Confirm selection and send access" }}</strong>
	                                  </div>
	                                </div>
	                                {% if item.notes %}
	                                  <p class="request-note"><strong>Additional instructions:</strong> {{ item.notes }}</p>
	                                {% endif %}
	                                <div class="approve-grid">
	                                  <label>
	                                    Recording or folder
	                                    <select name="recording_id" required>
	                                      {% for group in candidate_groups_by_request.get(item.id, []) %}
	                                        <optgroup label="{{ group.label }}">
	                                          {% for candidate in group.options %}
	                                            <option value="{{ candidate.id }}" data-title="{{ candidate.title }}" {% if candidate.id == item.recording_id %}selected{% endif %}>
	                                              {{ candidate_option_label(candidate) }}
	                                            </option>
	                                          {% endfor %}
	                                        </optgroup>
	                                      {% endfor %}
	                                    </select>
	                                  </label>
	                                  <div class="approve-submit">
	                                    <button type="submit">{{ "Send Link by Email" if email_enabled else "Prepare Link" }}</button>
	                                  </div>
	                                </div>
	                                <details class="email-details">
	                                  <summary>Edit email message</summary>
	                                  <label class="email-note">
	                                    Email message
	                                    <textarea name="email_message">{{ item.email_message or default_email_message(item, candidates[0]) }}</textarea>
	                                  </label>
	                                </details>
	                              </form>
	                            {% else %}
	                              <p class="muted">No exact date match found. Confirm the request date or add that recording to the library.</p>
	                            {% endif %}
	                          {% elif item.notes %}
	                            <p class="request-note"><strong>Additional instructions:</strong> {{ item.notes }}</p>
	                          {% endif %}
	                          {% if item.email_error %}
	                            <section class="note-strip">
	                              <div class="meta">Delivery Note</div>
	                              <p>{{ item.email_error }}</p>
	                            </section>
	                          {% endif %}
	                        </div>
	                      </details>
	                    {% endfor %}
	                  </div>
	                </section>
	              {% endfor %}
	            </div>
	          {% else %}
	            <p class="muted">{{ empty_message }}</p>
	          {% endif %}
        </section>
      </div>
    </main>
  </body>
</html>
"""


TESTIMONY_REVIEW_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ title }} Recorder Review</title>
    <style>
      :root {
        color-scheme:dark;
        --bg:#07121e;
        --surface:rgba(10,21,36,.92);
        --surface-2:rgba(18,34,53,.9);
        --surface-3:rgba(6,13,24,.62);
        --line:rgba(143,211,255,.2);
        --line-strong:rgba(143,211,255,.34);
        --text:#edf7ff;
        --muted:#9fb2c6;
        --accent:#8fd3ff;
        --good:#74ddb4;
        --good-soft:rgba(116,221,180,.1);
        --warn:#ffc875;
        --warn-soft:rgba(255,200,117,.1);
        --bad:#ffaaa8;
        --bad-soft:rgba(255,170,168,.1);
        --ink:#06101d;
        --shadow:0 22px 70px rgba(0,0,0,.34);
        --mono:ui-monospace,"SFMono-Regular",Consolas,monospace;
      }
      * { box-sizing:border-box; }
      body {
        margin:0;
        min-height:100vh;
        color:var(--text);
        background:
          radial-gradient(circle at 10% 0%, rgba(143,211,255,.2), transparent 28rem),
          linear-gradient(145deg,#050913,var(--bg)),
          var(--bg);
        font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      }
      body::before {
        content:"";
        position:fixed;
        inset:0;
        z-index:-1;
        pointer-events:none;
        background:url("{{ recordings_url_for('ntc_brand_background') }}") center / min(1120px,118vw) auto no-repeat;
        opacity:.31;
        filter:saturate(1.08) contrast(1.04);
      }
      main { width:min(1400px, calc(100vw - 32px)); margin:0 auto; padding:30px 0 44px; }
      header { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:1rem; margin-bottom:1rem; align-items:start; }
      h1, h2, h3, p { margin:0; }
      h1 { font-size:clamp(34px,5.2vw,64px); line-height:.95; letter-spacing:-.055em; }
      h2 { font-size:1.15rem; letter-spacing:-.02em; }
      h3 { font-size:1.02rem; letter-spacing:-.01em; }
      .eyebrow, .meta {
        color:var(--accent);
        font:800 .72rem var(--mono);
        letter-spacing:.12em;
        text-transform:uppercase;
      }
      .eyebrow + h1 { margin-top:.38rem; }
      .muted { color:var(--muted); line-height:1.45; }
      .actions { display:flex; gap:.5rem; flex-wrap:wrap; justify-content:flex-end; }
      a, button, input, textarea, select {
        border:1px solid var(--line);
        border-radius:14px;
        background:var(--surface-2);
        color:var(--text);
        padding:.72rem .9rem;
        text-decoration:none;
        font:inherit;
      }
      a, button { font-weight:850; }
      button { cursor:pointer; }
      button:hover, a:hover, input:hover, textarea:hover, select:hover { border-color:var(--line-strong); }
      input, textarea, select { width:100%; background:var(--surface-3); }
      textarea { min-height:5.8rem; resize:vertical; line-height:1.45; }
      label { display:grid; gap:.35rem; color:var(--muted); font-weight:850; }
      label span { font:800 .64rem var(--mono); letter-spacing:.12em; text-transform:uppercase; }
      audio { width:100%; min-height:44px; }
      .banner {
        margin-bottom:1rem;
        border:1px solid rgba(116,221,180,.35);
        background:var(--good-soft);
        color:var(--good);
        border-radius:16px;
        padding:.85rem;
        font-weight:850;
      }
      .banner.error { border-color:rgba(255,154,154,.4); background:var(--bad-soft); color:#ffaaaa; }
      .banner-stack:empty { display:none; }
      .metrics {
        display:grid;
        grid-template-columns:repeat(7,minmax(0,1fr));
        gap:.65rem;
        margin:.65rem 0 1rem;
      }
      .metric {
        border:1px solid var(--line);
        border-radius:18px;
        background:rgba(5,13,24,.58);
        padding:.78rem .85rem;
        min-width:0;
      }
      .metric span {
        display:block;
        color:var(--muted);
        font:800 .64rem var(--mono);
        letter-spacing:.12em;
        text-transform:uppercase;
      }
      .metric strong { display:block; margin-top:.24rem; font-size:1.42rem; line-height:1; letter-spacing:-.04em; }
      .metric small { display:block; margin-top:.32rem; color:var(--muted); line-height:1.35; }
      .toolbar {
        display:grid;
        grid-template-columns:minmax(0,1fr) auto;
        gap:.8rem;
        align-items:center;
        margin:.85rem 0 1rem;
      }
      .toolbar-actions {
        display:flex;
        justify-content:flex-end;
        align-items:flex-end;
        gap:.6rem;
        flex-wrap:wrap;
      }
      .tabs {
        display:flex;
        flex-wrap:wrap;
        gap:.35rem;
        padding:.28rem;
        border:1px solid var(--line);
        border-radius:999px;
        background:rgba(5,13,24,.58);
        width:max-content;
        max-width:100%;
      }
      .tab {
        display:flex;
        align-items:center;
        gap:.44rem;
        border-color:transparent;
        border-radius:999px;
        background:transparent;
        color:var(--muted);
        padding:.5rem .72rem;
        font-size:.84rem;
        line-height:1;
      }
      .tab.active {
        color:var(--text);
        background:linear-gradient(135deg,rgba(143,211,255,.18),rgba(143,245,200,.12));
        border-color:rgba(143,211,255,.42);
      }
      .tab strong {
        display:inline-grid;
        place-items:center;
        min-width:1.45rem;
        min-height:1.45rem;
        padding:0 .34rem;
        border-radius:999px;
        background:rgba(143,211,255,.1);
        color:inherit;
        font-size:.76rem;
      }
      .tab.active strong { background:linear-gradient(135deg,#8fd3ff,#8ff5c8); color:var(--ink); }
      .probe-form {
        display:flex;
        justify-content:flex-end;
        align-items:flex-end;
        gap:.45rem;
        flex-wrap:wrap;
      }
      .probe-form label {
        display:flex;
        flex-direction:column;
        justify-content:flex-end;
        gap:.28rem;
      }
      .probe-form input {
        width:5.8rem;
        height:3.05rem;
      }
      .probe-form button { min-height:3.05rem; }
      .probe-form.action-only { min-height:3.05rem; }
      .job-panel {
        display:flex;
        align-items:center;
        justify-content:space-between;
        gap:.8rem;
        margin:-.15rem 0 1rem;
        border:1px solid rgba(143,211,255,.18);
        border-radius:18px;
        background:rgba(143,211,255,.055);
        padding:.78rem .9rem;
        color:var(--muted);
      }
      .job-panel strong { color:var(--text); }
      .job-panel span { color:var(--accent); font:800 .68rem var(--mono); letter-spacing:.1em; text-transform:uppercase; }
      .panel {
        border:1px solid var(--line);
        border-radius:24px;
        background:var(--surface);
        padding:1rem;
        box-shadow:var(--shadow);
      }
      .panel-head {
        display:flex;
        align-items:end;
        justify-content:space-between;
        gap:1rem;
        flex-wrap:wrap;
        margin-bottom:.8rem;
      }
      .panel-head p { margin-top:.25rem; }
      .review-list { display:grid; gap:.62rem; }
      .review-card {
        border:1px solid rgba(143,211,255,.16);
        border-radius:18px;
        background:linear-gradient(135deg,rgba(255,255,255,.045),rgba(143,211,255,.02));
        overflow:hidden;
      }
      .review-card[open] { border-color:var(--line-strong); }
      .review-card.is-saving { opacity:.72; pointer-events:none; }
      .review-card summary { list-style:none; cursor:pointer; padding:.85rem .9rem; }
      .review-card summary::-webkit-details-marker { display:none; }
      .review-card[open] summary { border-bottom:1px solid var(--line); background:rgba(143,211,255,.035); }
      .review-row {
        display:grid;
        grid-template-columns:minmax(2.4rem,.16fr) minmax(16rem,1.3fr) minmax(7rem,.42fr) minmax(7rem,.42fr) minmax(10rem,.6fr) minmax(8rem,.42fr);
        align-items:center;
        gap:.78rem;
      }
      .row-number {
        display:grid;
        place-items:center;
        width:2.1rem;
        height:2.1rem;
        border:1px solid rgba(143,211,255,.26);
        border-radius:999px;
        background:rgba(143,211,255,.075);
        color:#cce4f7;
        font:850 .78rem var(--mono);
        letter-spacing:.02em;
      }
      .cell { min-width:0; }
      .cell-label {
        display:block;
        margin-bottom:.18rem;
        color:var(--muted);
        font:800 .62rem var(--mono);
        letter-spacing:.12em;
        text-transform:uppercase;
      }
      .cell-value {
        display:block;
        color:var(--text);
        font-weight:850;
        overflow:hidden;
        text-overflow:ellipsis;
        white-space:nowrap;
      }
      .cell-subvalue {
        display:block;
        margin-top:.12rem;
        color:var(--muted);
        overflow:hidden;
        text-overflow:ellipsis;
        white-space:nowrap;
      }
      .pill {
        display:inline-flex;
        justify-content:center;
        width:max-content;
        border:1px solid var(--line);
        border-radius:999px;
        padding:.35rem .58rem;
        color:var(--muted);
        font:800 .68rem var(--mono);
        letter-spacing:.08em;
        text-transform:uppercase;
      }
      .pill.needs_review { color:var(--warn); border-color:rgba(255,200,117,.35); background:var(--warn-soft); }
      .pill.identified, .pill.already_named { color:var(--good); border-color:rgba(116,221,180,.34); background:var(--good-soft); }
      .pill.grouped { color:var(--accent); border-color:rgba(143,211,255,.34); background:rgba(143,211,255,.08); }
      .pill.message_review { color:var(--accent); border-color:rgba(143,211,255,.34); background:rgba(143,211,255,.08); }
      .pill.not_testimony { color:var(--bad); border-color:rgba(255,170,168,.35); background:var(--bad-soft); }
      .pill.duplicate { color:var(--accent); border-color:rgba(143,211,255,.34); background:rgba(143,211,255,.08); }
      .review-body {
        display:grid;
        grid-template-columns:minmax(16rem,.8fr) minmax(0,1.2fr);
        gap:.95rem;
        padding:1rem;
        background:linear-gradient(180deg,rgba(143,211,255,.028),rgba(4,11,20,.12));
        align-items:start;
      }
      .listen-panel, .edit-panel, .path-panel {
        border:1px solid rgba(143,211,255,.16);
        border-radius:18px;
        background:rgba(5,13,24,.45);
        padding:.9rem;
      }
      .listen-panel { display:grid; gap:.75rem; align-content:start; }
      .file-facts {
        display:grid;
        grid-template-columns:repeat(2,minmax(0,1fr));
        gap:.55rem;
      }
      .fact {
        border:1px solid rgba(143,211,255,.12);
        border-radius:14px;
        background:rgba(255,255,255,.035);
        padding:.65rem;
      }
      .fact span {
        display:block;
        color:var(--muted);
        font:800 .62rem var(--mono);
        letter-spacing:.12em;
        text-transform:uppercase;
      }
      .fact strong { display:block; margin-top:.18rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .review-form { display:grid; gap:.75rem; align-self:start; }
      .form-grid {
        display:grid;
        grid-template-columns:minmax(0,.72fr) minmax(0,1fr);
        gap:.72rem;
      }
      .wide { grid-column:1 / -1; }
      .path-panel {
        grid-column:1 / -1;
        display:grid;
        gap:.35rem;
      }
      .path-panel code {
        display:block;
        overflow-wrap:anywhere;
        border:1px solid rgba(143,211,255,.14);
        border-radius:14px;
        background:rgba(4,11,20,.62);
        padding:.72rem;
        color:#dbeaff;
        font-family:var(--mono);
        font-size:.82rem;
        line-height:1.45;
      }
      .button-row { display:flex; gap:.55rem; flex-wrap:wrap; justify-content:flex-end; align-items:center; padding-top:.1rem; }
      .button-row button { width:auto; min-height:2.7rem; padding:.62rem .82rem; line-height:1.1; border-radius:13px; }
      .save { color:#dcfff0; background:rgba(116,221,180,.15); border-color:rgba(116,221,180,.45); }
      .danger { color:#ffd7d7; border-color:rgba(255,170,168,.35); background:var(--bad-soft); }
      .secondary { color:var(--muted); background:rgba(143,211,255,.06); }
      .suggestion-panel {
        display:grid;
        grid-template-columns:minmax(0,1fr) auto;
        gap:.7rem;
        align-items:center;
        border:1px solid rgba(125,236,204,.26);
        border-radius:16px;
        background:rgba(116,221,180,.08);
        padding:.75rem;
      }
      .suggestion-panel.subdued {
        border-color:rgba(143,211,255,.16);
        background:rgba(143,211,255,.045);
      }
      .suggestion-panel span {
        display:block;
        color:#a7d9ff;
        font:800 .62rem var(--mono);
        letter-spacing:.12em;
        text-transform:uppercase;
      }
      .suggestion-panel strong { display:block; margin-top:.14rem; color:#eff6ff; font-size:1.08rem; }
      .suggestion-panel small { display:block; margin-top:.12rem; color:var(--muted); }
      .suggestion-panel p {
        grid-column:1 / -1;
        margin:0;
        color:var(--muted);
        line-height:1.45;
      }
      .transcript-panel {
        grid-template-columns:minmax(0,1fr);
        border-color:rgba(143,211,255,.18);
        background:rgba(143,211,255,.04);
      }
      .transcript-panel p {
        max-height:10.5rem;
        overflow:auto;
      }
      .transcript-full {
        grid-column:1 / -1;
        border-top:1px solid rgba(143,211,255,.16);
        padding-top:.7rem;
      }
      .transcript-full summary {
        cursor:pointer;
        color:#c6d3e2;
        font-weight:800;
      }
      .transcript-full[open] summary { margin-bottom:.55rem; }
      .transcript-full p {
        white-space:pre-wrap;
        max-height:22rem;
      }
      .empty {
        border:1px dashed rgba(143,211,255,.25);
        border-radius:18px;
        padding:1.2rem;
        color:var(--muted);
        text-align:center;
      }
      @media (max-width:1100px) {
        .metrics { grid-template-columns:repeat(2,minmax(0,1fr)); }
        .toolbar, .review-body { grid-template-columns:1fr; }
        .toolbar-actions, .probe-form { justify-content:flex-start; }
        .job-panel { flex-direction:column; align-items:flex-start; }
        .review-row { grid-template-columns:2.3rem minmax(0,1fr) minmax(0,1fr); align-items:start; }
        .row-number { align-self:center; }
      }
      @media (max-width:760px) {
        main { width:min(100vw - 24px, 1120px); padding-top:18px; }
        header { grid-template-columns:minmax(0,1fr) auto; gap:.65rem; }
        header > div:first-child { min-width:0; }
        header h1 { font-size:clamp(1.65rem, 8vw, 2.9rem); }
        header .muted { font-size:.86rem; }
        .panel-head { display:flex; flex-direction:column; align-items:flex-start; }
        .actions { width:min(62vw, 14rem); justify-content:flex-end; flex-wrap:nowrap; gap:.35rem; }
        .actions > a, .actions > form { flex:1 1 0; min-width:0; }
        .actions > form > button, header .actions a { width:100%; min-width:0; }
        header .actions a, header .actions button { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; padding:.54rem .52rem; font-size:.78rem; border-radius:12px; }
        .tabs { width:100%; border-radius:22px; }
        .tab { width:100%; justify-content:space-between; }
        .metrics, .file-facts, .form-grid { grid-template-columns:1fr; }
        .review-row { grid-template-columns:2.3rem minmax(0,1fr); }
        .wide { grid-column:auto; }
        .suggestion-panel { grid-template-columns:1fr; }
        .button-row, .button-row button, .toolbar-actions, .probe-form, .probe-form input, .probe-form button { width:100%; }
      }
    </style>
  </head>
  <body>
    <main>
      <header>
        <div>
          <div class="eyebrow">NTC Newark</div>
          <h1>Recorder Review</h1>
          <p class="muted">Review recorder pulls, identify speakers, and flag message or event recordings that need filing.</p>
        </div>
        <div class="actions">
          <a href="{{ recordings_url_for('admin_panel') }}">Requests</a>
          <a href="{{ recordings_url_for('public_form') }}">Public Form</a>
          <form method="post" action="{{ recordings_url_for('admin_logout') }}"><button type="submit">Sign Out</button></form>
        </div>
      </header>
      <datalist id="speaker-name-options">
        {% for speaker_name in speaker_names %}
          <option value="{{ speaker_name }}"></option>
        {% endfor %}
      </datalist>
      <div class="banner-stack" data-banner-stack>
        {% if message %}<div class="banner">{{ message }}</div>{% endif %}
        {% if error %}<div class="banner error">{{ error }}</div>{% endif %}
      </div>
      <section class="metrics" aria-label="Recorder review status">
        <div class="metric"><span>Needs Review</span><strong data-count="needs_review">{{ counts.needs_review }}</strong><small>Awaiting identification</small></div>
        <div class="metric"><span>Message/Event</span><strong data-count="message_review">{{ counts.message_review }}</strong><small>Needs title or event filing</small></div>
        <div class="metric"><span>Identified</span><strong data-count="identified">{{ counts.identified }}</strong><small>Speaker confirmed or already named</small></div>
        <div class="metric"><span>Grouped</span><strong data-count="grouped">{{ counts.grouped }}</strong><small>Event testimony sets</small></div>
        <div class="metric"><span>Not Needed</span><strong data-count="not_testimony">{{ counts.not_testimony }}</strong><small>Reject junk or unusable clips</small></div>
        <div class="metric"><span>Duplicates</span><strong data-count="duplicate">{{ counts.duplicate }}</strong><small>Already covered by another file</small></div>
        <div class="metric"><span>Files Found</span><strong data-count="all">{{ counts.all }}</strong><small>Folder connected</small></div>
      </section>
      <div class="toolbar">
        <nav class="tabs" aria-label="Recorder review filters">
          {% for key, label in [("needs_review", "Needs Review"), ("message_review", "Message/Event"), ("identified", "Identified"), ("grouped", "Grouped"), ("not_testimony", "Not Needed"), ("duplicate", "Duplicate"), ("all", "All")] %}
            <a class="tab {{ 'active' if status_filter == key else '' }}" {% if status_filter == key %}aria-current="page"{% endif %} href="{{ recordings_url_for('testimony_review', status=key, sort=sort, limit=limit) }}">{{ label }} <strong data-count="{{ key }}">{{ counts[key] }}</strong></a>
          {% endfor %}
        </nav>
        <div class="toolbar-actions">
          {% if status_filter in ["needs_review", "all"] %}
            <form class="probe-form" method="post" action="{{ recordings_url_for('probe_testimony_durations') }}">
              <input type="hidden" name="status" value="{{ status_filter }}">
              <input type="hidden" name="sort" value="{{ sort }}">
              <label>
                <span>Probe</span>
                <input name="limit" type="number" min="1" max="120" value="{{ probe_limit }}">
              </label>
              <button type="submit">Check Durations</button>
            </form>
            <form class="probe-form action-only" method="post" action="{{ recordings_url_for('suggest_all_testimony_speakers') }}">
              <input type="hidden" name="status" value="{{ status_filter }}">
              <input type="hidden" name="sort" value="{{ sort }}">
              <button type="submit" data-process-suggestions-button {% if suggestion_job.state == "running" %}disabled{% endif %}>Process Suggestions</button>
            </form>
          {% endif %}
          {% if status_filter in ["needs_review", "message_review", "identified", "grouped", "all"] %}
            <form class="probe-form action-only" method="post" action="{{ recordings_url_for('transcribe_identified_testimonies') }}">
              <input type="hidden" name="status" value="{{ status_filter }}">
              <input type="hidden" name="sort" value="{{ sort }}">
              <button type="submit" data-process-transcripts-button {% if transcript_job.state == "running" %}disabled{% endif %}>Process Transcripts</button>
            </form>
          {% endif %}
          {% if status_filter in ["not_testimony", "duplicate", "all"] %}
            <form class="probe-form action-only" method="post" action="{{ recordings_url_for('quarantine_testimony_reviews') }}">
              <input type="hidden" name="status" value="{{ status_filter }}">
              <input type="hidden" name="sort" value="{{ sort }}">
              <button type="submit">
                {% if status_filter == "not_testimony" %}
                  Quarantine Not Needed
                {% elif status_filter == "duplicate" %}
                  Quarantine Duplicates
                {% else %}
                  Quarantine Rejected
                {% endif %}
              </button>
            </form>
          {% endif %}
        </div>
      </div>
      <div class="job-panel" data-suggestion-job data-status-url="{{ recordings_url_for('testimony_suggestion_status') }}" data-state="{{ suggestion_job.state }}" {% if suggestion_job.state not in ["running", "finished", "failed"] %}hidden{% endif %}>
        <div>
          <span>Recorder Suggestions</span>
          <strong data-job-message>{{ suggestion_job.message or "Idle." }}</strong>
          <div data-job-counts>
            {{ suggestion_job.processed }} / {{ suggestion_job.total }} processed · {{ suggestion_job.found }} suggested · {{ suggestion_job.checked }} checked · {{ suggestion_job.errors }} errors
          </div>
        </div>
        <div data-job-current>{% if suggestion_job.current %}Now checking {{ suggestion_job.current }}{% endif %}</div>
      </div>
      <div class="job-panel" data-transcript-job data-status-url="{{ recordings_url_for('testimony_transcript_status') }}" data-state="{{ transcript_job.state }}" {% if transcript_job.state not in ["running", "finished", "failed"] %}hidden{% endif %}>
        <div>
          <span>Recorder Transcripts</span>
          <strong data-job-message>{{ transcript_job.message or "Idle." }}</strong>
          <div data-job-counts>
            {{ transcript_job.processed }} / {{ transcript_job.total }} processed · {{ transcript_job.saved }} saved · {{ transcript_job.errors }} errors
          </div>
        </div>
        <div data-job-current>{% if transcript_job.current %}Now transcribing {{ transcript_job.current }}{% endif %}</div>
      </div>
      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>{{ status_label(status_filter) }}</h2>
            <p class="muted">Listen, confirm the service date, then save the speaker, group an event, or flag message recordings that need title/event filing.</p>
          </div>
          <form class="probe-form" method="get" action="{{ recordings_url_for('testimony_review') }}">
            <input type="hidden" name="status" value="{{ status_filter }}">
            <label>
              <span>Sort</span>
              <select name="sort">
                <option value="shortest" {% if sort == "shortest" %}selected{% endif %}>Shortest first</option>
                <option value="newest" {% if sort == "newest" %}selected{% endif %}>Newest first</option>
                <option value="name" {% if sort == "name" %}selected{% endif %}>Name</option>
              </select>
            </label>
            <label>
              <span>Limit</span>
              <input name="limit" type="number" min="1" max="500" value="{{ limit }}">
            </label>
            <button type="submit">Apply</button>
          </form>
        </div>
        {% if not testimony_source_exists %}
          <div class="empty">Recorder source folder is not connected.</div>
        {% elif items %}
          <div class="review-list" data-review-list data-active-filter="{{ status_filter }}" data-empty-message="No recordings match this filter.">
            {% for item in items %}
              <details class="review-card {{ item.status }}" data-review-id="{{ item.id }}" data-status="{{ item.status }}">
                <summary>
                  <div class="review-row">
                    <span class="row-number" data-row-number aria-label="Row {{ loop.index }}">#{{ loop.index }}</span>
                    <div class="cell">
                      <span class="cell-label">Recording</span>
                      <span class="cell-value" data-field="title">{{ item.title }}</span>
                      <span class="cell-subvalue" data-field="source-label">{{ item.source_label }}</span>
                    </div>
                    <div class="cell">
                      <span class="cell-label">Duration</span>
                      <span class="cell-value">{{ item.duration_label }}</span>
                    </div>
                    <div class="cell">
                      <span class="cell-label">Date</span>
                      <span class="cell-value" data-field="service-date-label">{{ format_date(item.service_date or item.recording_date) }}</span>
                    </div>
                    <div class="cell">
                      <span class="cell-label">Speaker</span>
                      <span class="cell-value" data-field="speaker">{{ item.speaker_name or "Not set" }}</span>
                    </div>
                    <span class="pill {{ item.status }}" data-field="status-pill">{{ item.status_label }}</span>
                  </div>
                </summary>
                <div class="review-body">
                  <section class="listen-panel">
                    <div>
                      <div class="meta">Listen</div>
                      <h3 data-field="listen-title">{{ item.title }}</h3>
                    </div>
                    <audio controls preload="none" data-src="{{ item.audio_url }}"></audio>
                    <div class="file-facts">
                      <div class="fact"><span>Size</span><strong>{{ item.size_label }}</strong></div>
                      <div class="fact"><span>Modified</span><strong>{{ item.modified_label }}</strong></div>
                      <div class="fact wide"><span>File</span><strong data-field="file-fact">{{ item.source_label }}</strong></div>
                    </div>
                  </section>
                  <form class="review-form" method="post" action="{{ recordings_url_for('update_testimony_review', recording_id=item.id) }}">
                    <input type="hidden" name="sort" value="{{ sort }}">
                    <input type="hidden" name="status_filter" value="{{ status_filter }}">
                    <input type="hidden" name="source_path" value="{{ item.source_path }}">
                    <section class="edit-panel">
                      <div class="form-grid">
                        <label>
                          <span>Service Date</span>
                          <input name="service_date" type="date" value="{{ item.service_date }}">
                        </label>
                        <label>
                          <span>Speaker</span>
                          <input name="speaker_name" value="{{ item.speaker_name }}" placeholder="Type speaker name" list="speaker-name-options">
                        </label>
                        <label>
                          <span>Group / Event Title</span>
                          <input name="group_title" value="{{ item.group_title }}" placeholder="Testimonies Part 1 or Closing Program">
                        </label>
                      </div>
                      {% if item.event_group %}
                        <div class="suggestion-panel subdued">
                          <div>
                            <span>Event Folder</span>
                            <strong>{{ item.event_group }}</strong>
                            <small>Grouped clips save into this event when marked grouped.</small>
                          </div>
                        </div>
                      {% endif %}
                      {% if item.suggested_speaker %}
                        <div class="suggestion-panel speaker-assist-panel">
                          <div>
                            <span>Suggested Speaker</span>
                            <strong>{{ item.suggested_speaker }}</strong>
                            {% if item.suggestion_source_label %}<small>{{ item.suggestion_source_label }}</small>{% endif %}
                          </div>
                          {% if item.suggested_speaker != item.speaker_name %}
                            <button class="secondary apply-suggestion" type="button" data-speaker="{{ item.suggested_speaker }}">Use Suggestion</button>
                          {% endif %}
                          {% if item.suggestion_text and item.suggestion_source not in ["transcript_intro", "transcript_excerpt"] %}<p>{{ item.suggestion_text }}</p>{% endif %}
                        </div>
                      {% elif not item.speaker_name and item.suggestion_source %}
                        <div class="suggestion-panel subdued speaker-assist-panel">
                          <div>
                            <span>Suggested Speaker</span>
                            <strong>No suggested speaker</strong>
                          </div>
                        </div>
                      {% elif not item.speaker_name %}
                        <div class="suggestion-panel subdued speaker-assist-panel">
                          <div>
                            <span>Suggested Speaker</span>
                            <strong>No suggestion yet</strong>
                            <small>Use Suggest Speaker or Process Transcript for this row.</small>
                          </div>
                          <button class="secondary" type="submit" formaction="{{ recordings_url_for('suggest_testimony_speaker', recording_id=item.id) }}" formmethod="post">Suggest Speaker</button>
                        </div>
                      {% endif %}
                      {% if item.transcript_preview or item.transcript_text or item.transcript_error or item.status in ["identified", "grouped", "already_named"] %}
                        <div class="suggestion-panel subdued transcript-panel speaker-transcript-panel">
                          <div>
                            <span>Transcript</span>
                          </div>
                          {% if item.transcript_preview %}
                            <p>{{ item.transcript_preview }}</p>
                          {% elif item.transcript_error %}
                            <p>{{ item.transcript_error }}</p>
                          {% else %}
                            <small>Not processed yet.</small>
                          {% endif %}
                          {% if item.transcript_text %}
                            <details class="transcript-full">
                              <summary>View full transcript</summary>
                              <p>{{ item.transcript_text }}</p>
                            </details>
                          {% endif %}
                        </div>
                      {% endif %}
                      {% if item.quarantined %}
                        <div class="suggestion-panel subdued transcript-panel">
                          <div>
                            <span>Quarantine</span>
                            <strong>Moved to rejected holding folder</strong>
                            {% if item.quarantined_label %}<small>{{ item.quarantined_label }}</small>{% endif %}
                          </div>
                        </div>
                      {% endif %}
                    </section>
                    <div class="button-row">
                      <button class="secondary" type="submit" name="status" value="needs_review">Needs Review</button>
                      <button class="danger" type="submit" name="status" value="not_testimony">Mark Not Needed</button>
                      <button class="secondary" type="submit" name="status" value="duplicate">Mark Duplicate</button>
                      <button class="secondary" type="submit" formaction="{{ recordings_url_for('transcribe_testimony_recording', recording_id=item.id) }}" formmethod="post">Process Transcript</button>
                      <button class="secondary" type="submit" name="status" value="message_review">Save Message/Event</button>
                      <button class="secondary" type="submit" name="status" value="grouped">Save Grouped</button>
                      <button class="save" type="submit" name="status" value="identified">Save Speaker</button>
                    </div>
                  </form>
                </div>
              </details>
            {% endfor %}
          </div>
        {% else %}
          <div class="empty">No recordings match this filter.</div>
        {% endif %}
      </section>
    </main>
    <script>
      const openCardsKey = "ntc-recorder-review-open-cards";
      const statusClasses = ["needs_review", "message_review", "identified", "grouped", "not_testimony", "duplicate", "already_named"];
      const bannerStack = document.querySelector("[data-banner-stack]");
      const reviewList = document.querySelector("[data-review-list]");
      const activeReviewFilter = reviewList ? reviewList.dataset.activeFilter || "needs_review" : "needs_review";

      function storedOpenCards() {
        try {
          return new Set(JSON.parse(window.localStorage.getItem(openCardsKey) || "[]"));
        } catch (error) {
          return new Set();
        }
      }

      function saveOpenCards() {
        const ids = Array.from(document.querySelectorAll(".review-card[open]"))
          .map((card) => card.dataset.reviewId || "")
          .filter(Boolean);
        window.localStorage.setItem(openCardsKey, JSON.stringify(ids));
      }

      function restoreOpenCards() {
        const ids = storedOpenCards();
        if (!ids.size) return;
        document.querySelectorAll(".review-card").forEach((card) => {
          if (ids.has(card.dataset.reviewId || "")) {
            card.open = true;
            hydrateReviewAudio(card);
          }
        });
      }

      function showBanner(message, isError = false) {
        if (!bannerStack || !message) return;
        bannerStack.replaceChildren();
        const banner = document.createElement("div");
        banner.className = `banner${isError ? " error" : ""}`;
        banner.textContent = message;
        bannerStack.appendChild(banner);
      }

      function setText(card, field, value) {
        const node = card.querySelector(`[data-field="${field}"]`);
        if (node && value !== undefined && value !== null) {
          node.textContent = value || "";
        }
      }

      function setInputValue(card, name, value) {
        const input = card.querySelector(`[name="${name}"]`);
        if (input && value !== undefined && value !== null) {
          input.value = value || "";
        }
      }

      function countStatus(status) {
        return status === "already_named" ? "identified" : status;
      }

      function changeCount(status, delta) {
        const key = countStatus(status);
        if (!key || !delta) return;
        document.querySelectorAll(`[data-count="${key}"]`).forEach((node) => {
          const current = Number.parseInt(node.textContent || "0", 10);
          node.textContent = String(Math.max(0, (Number.isNaN(current) ? 0 : current) + delta));
        });
      }

      function updateStatusCounts(previousStatus, nextStatus) {
        const previousKey = countStatus(previousStatus);
        const nextKey = countStatus(nextStatus);
        if (!previousKey || !nextKey || previousKey === nextKey) return;
        changeCount(previousKey, -1);
        changeCount(nextKey, 1);
      }

      function belongsInActiveFilter(status) {
        const key = countStatus(status);
        if (activeReviewFilter === "all") return true;
        if (activeReviewFilter === "identified") return key === "identified";
        return key === activeReviewFilter;
      }

      function setFormBusy(form, isBusy) {
        if (!form) return;
        form.setAttribute("aria-busy", isBusy ? "true" : "false");
        form.querySelectorAll("button").forEach((button) => {
          if (isBusy) {
            button.dataset.wasDisabled = button.disabled ? "1" : "0";
            button.disabled = true;
          } else {
            button.disabled = button.dataset.wasDisabled === "1";
            delete button.dataset.wasDisabled;
          }
        });
      }

      function replaceEmptyReviewList() {
        if (!reviewList || reviewList.querySelector(".review-card")) return;
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = reviewList.dataset.emptyMessage || "No recordings match this filter.";
        reviewList.replaceWith(empty);
      }

      function renumberReviewRows() {
        if (!reviewList) return;
        reviewList.querySelectorAll(".review-card").forEach((card, index) => {
          const number = card.querySelector("[data-row-number]");
          if (!number) return;
          const value = index + 1;
          number.textContent = `#${value}`;
          number.setAttribute("aria-label", `Row ${value}`);
        });
      }

      function updateReviewCard(card, data) {
        const previousStatus = card.dataset.status || "";
        setText(card, "title", data.title);
        setText(card, "listen-title", data.title);
        setText(card, "source-label", data.source_label);
        setText(card, "file-fact", data.source_label);
        setText(card, "service-date-label", data.service_date_label);
        setText(card, "speaker", data.speaker_name || "Not set");

        const statusPill = card.querySelector('[data-field="status-pill"]');
        if (statusPill && data.status) {
          statusPill.textContent = data.status_label || data.status;
          statusPill.classList.remove(...statusClasses);
          statusPill.classList.add(data.status);
        }
        if (data.status) {
          card.classList.remove(...statusClasses);
          card.classList.add(data.status);
          card.dataset.status = data.status;
        }
        if (data.recording_id) {
          card.dataset.reviewId = data.recording_id;
        }

        const form = card.querySelector(".review-form");
        if (form && data.review_url) {
          form.action = data.review_url;
        }
        const sourcePath = card.querySelector('input[name="source_path"]');
        if (sourcePath && data.source_path) {
          sourcePath.value = data.source_path;
        }
        const serviceDate = card.querySelector('input[name="service_date"]');
        if (serviceDate && data.service_date) {
          serviceDate.value = data.service_date;
        }
        setInputValue(card, "speaker_name", data.speaker_name || "");
        setInputValue(card, "group_title", data.group_title || "");

        const audio = card.querySelector("audio[data-src]");
        if (audio && data.audio_url && audio.dataset.src !== data.audio_url) {
          audio.pause();
          audio.removeAttribute("src");
          audio.dataset.src = data.audio_url;
          audio.load();
          if (card.open) hydrateReviewAudio(card);
        }
        if (!(data.suggested_speaker || data.suggestion_source || data.suggestion_text)) {
          card.querySelectorAll(".speaker-assist-panel").forEach((panel) => panel.remove());
        }
        if (data.status) {
          updateStatusCounts(previousStatus, data.status);
          if (!belongsInActiveFilter(data.status)) {
            pauseCardAudio(card);
            card.remove();
            renumberReviewRows();
            replaceEmptyReviewList();
          }
        }
        saveOpenCards();
      }

      function renderSuggestion(card, data) {
        const form = card.querySelector(".review-form");
        const editPanel = form ? form.querySelector(".edit-panel") : null;
        if (!editPanel) return;
        editPanel.querySelectorAll(".speaker-assist-panel").forEach((panel) => panel.remove());
        editPanel.querySelectorAll(".speaker-transcript-panel").forEach((panel) => panel.remove());

        const speakerInput = form.querySelector('input[name="speaker_name"]');
        const currentSpeaker = speakerInput ? speakerInput.value : (data.speaker_name || "");
        const hasSuggestionState = Boolean(data.suggested_speaker || data.suggestion_source || data.suggestion_text);
        if (data.suggested_speaker || (!currentSpeaker && hasSuggestionState)) {
          const panel = document.createElement("div");
          panel.className = data.suggested_speaker ? "suggestion-panel speaker-assist-panel" : "suggestion-panel subdued speaker-assist-panel";
          const textWrap = document.createElement("div");
          const label = document.createElement("span");
          label.textContent = "Suggested Speaker";
          const strong = document.createElement("strong");
          strong.textContent = data.suggested_speaker || "No suggested speaker";
          textWrap.append(label, strong);
          if (data.suggested_speaker && data.suggestion_source_label) {
            const small = document.createElement("small");
            small.textContent = data.suggestion_source_label;
            textWrap.appendChild(small);
          }
          panel.appendChild(textWrap);
          if (data.suggested_speaker && data.suggested_speaker !== currentSpeaker) {
            const button = document.createElement("button");
            button.className = "secondary apply-suggestion";
            button.type = "button";
            button.dataset.speaker = data.suggested_speaker;
            button.textContent = "Use Suggestion";
            panel.appendChild(button);
          }
          if (data.suggestion_text && !["transcript_intro", "transcript_excerpt"].includes(data.suggestion_source || "")) {
            const paragraph = document.createElement("p");
            paragraph.textContent = data.suggestion_text;
            panel.appendChild(paragraph);
          }
          editPanel.appendChild(panel);
        }

        const transcriptPreview = data.transcript_preview || data.transcript_excerpt || (["transcript_intro", "transcript_excerpt"].includes(data.suggestion_source || "") ? data.suggestion_text : "");
        if (transcriptPreview || data.transcript_text || data.transcript_error) {
          const transcriptPanel = document.createElement("div");
          transcriptPanel.className = "suggestion-panel subdued transcript-panel speaker-transcript-panel";
          const textWrap = document.createElement("div");
          const label = document.createElement("span");
          label.textContent = "Transcript";
          textWrap.appendChild(label);
          transcriptPanel.appendChild(textWrap);
          const paragraph = document.createElement("p");
          paragraph.textContent = transcriptPreview || data.transcript_error || "Not processed yet.";
          transcriptPanel.appendChild(paragraph);
          if (data.transcript_text) {
            const details = document.createElement("details");
            details.className = "transcript-full";
            const summary = document.createElement("summary");
            summary.textContent = "View full transcript";
            const full = document.createElement("p");
            full.textContent = data.transcript_text;
            details.append(summary, full);
            transcriptPanel.appendChild(details);
          }
          editPanel.appendChild(transcriptPanel);
        }
      }

      async function readJsonResponse(response) {
        const contentType = response.headers.get("Content-Type") || "";
        if (contentType.toLowerCase().includes("application/json")) {
          return response.json();
        }
        const text = await response.text();
        return {
          ok: false,
          error: text.trim().startsWith("<") ? `The server returned a page instead of a testimony update (HTTP ${response.status}). Sign in again if the admin page expired, then retry.` : `The server response was not JSON (HTTP ${response.status}).`,
        };
      }

      function submissionUrl(form, submitter) {
        const override = submitter ? submitter.getAttribute("formaction") : "";
        const target = override || form.getAttribute("action") || form.action;
        return new URL(target, window.location.href).toString();
      }

      document.addEventListener("click", (event) => {
        const button = event.target.closest(".apply-suggestion");
        if (!button) return;
        const form = button.closest("form");
        const input = form ? form.querySelector('input[name="speaker_name"]') : null;
        if (!input) return;
        input.value = button.dataset.speaker || "";
        input.focus();
      });
      function hydrateReviewAudio(card) {
        const audio = card.querySelector("audio[data-src]");
        if (!audio || audio.src) return;
        audio.src = audio.dataset.src || "";
        audio.preload = "metadata";
      }

      function pauseCardAudio(card) {
        if (!card) return;
        card.querySelectorAll("audio").forEach((audio) => {
          if (!audio.paused) {
            audio.pause();
          }
        });
      }

      function pauseOtherReviewAudio(activeAudio) {
        document.querySelectorAll(".review-card audio").forEach((audio) => {
          if (audio !== activeAudio && !audio.paused) {
            audio.pause();
          }
        });
      }

      document.addEventListener("play", (event) => {
        const audio = event.target;
        if (!audio || !audio.matches || !audio.matches(".review-card audio")) return;
        pauseOtherReviewAudio(audio);
      }, true);

      document.addEventListener("toggle", (event) => {
        const card = event.target;
        if (card.matches && card.matches(".review-card") && card.open) {
          hydrateReviewAudio(card);
        }
        if (card.matches && card.matches(".review-card") && !card.open) {
          pauseCardAudio(card);
        }
        if (card.matches && card.matches(".review-card")) {
          saveOpenCards();
        }
      }, true);

      document.addEventListener("submit", async (event) => {
        const form = event.target.closest(".review-form");
        saveOpenCards();
        if (!form || !window.fetch) return;
        event.preventDefault();

        const card = form.closest(".review-card");
        const submitter = event.submitter;
        const url = submissionUrl(form, submitter);
        let formData;
        try {
          formData = new FormData(form, submitter);
        } catch (error) {
          formData = new FormData(form);
          if (submitter && submitter.name) {
            formData.append(submitter.name, submitter.value);
          }
        }

        card.classList.add("is-saving");
        setFormBusy(form, true);
        try {
          const response = await fetch(url, {
            method: "POST",
            body: formData,
            headers: { "Accept": "application/json", "X-Requested-With": "fetch" },
          });
          const data = await readJsonResponse(response);
          if (!response.ok || !data.ok) {
            throw new Error(data.error || data.message || "The testimony update failed.");
          }
          showBanner(data.message || "Recorder review updated.");
          updateReviewCard(card, data);
          if (data.suggested_speaker || data.suggestion_source || data.suggestion_text || data.transcript_preview || data.transcript_text || data.transcript_error) {
            renderSuggestion(card, data);
          }
        } catch (error) {
          showBanner(error.message || "The testimony update failed.", true);
        } finally {
          card.classList.remove("is-saving");
          setFormBusy(form, false);
        }
      });

      function updateSuggestionJob(job) {
        const panel = document.querySelector("[data-suggestion-job]");
        if (!panel || !job) return;
        const message = panel.querySelector("[data-job-message]");
        const counts = panel.querySelector("[data-job-counts]");
        const current = panel.querySelector("[data-job-current]");
        const button = document.querySelector("[data-process-suggestions-button]");
        const priorState = panel.dataset.state || "";

        panel.hidden = !["running", "finished", "failed"].includes(job.state);
        panel.dataset.state = job.state || "";
        if (message) message.textContent = job.message || "Idle.";
        if (counts) {
          counts.textContent = `${job.processed || 0} / ${job.total || 0} processed · ${job.found || 0} suggested · ${job.checked || 0} checked · ${job.errors || 0} errors`;
        }
        if (current) {
          current.textContent = job.current ? `Now checking ${job.current}` : "";
        }
        if (button) {
          button.disabled = job.state === "running";
        }
        if (priorState === "running" && ["finished", "failed"].includes(job.state)) {
          const tag = document.activeElement ? document.activeElement.tagName : "";
          const editing = ["INPUT", "TEXTAREA", "SELECT"].includes(tag);
          if (!editing && job.state === "finished") {
            saveOpenCards();
            window.setTimeout(() => window.location.reload(), 900);
          }
        }
      }

      async function pollSuggestionJob() {
        const panel = document.querySelector("[data-suggestion-job]");
        if (!panel || !panel.dataset.statusUrl) return;
        try {
          const response = await fetch(panel.dataset.statusUrl, { headers: { "Accept": "application/json" } });
          if (!response.ok) return;
          const job = await response.json();
          updateSuggestionJob(job);
          if (job.state === "running") {
            window.setTimeout(pollSuggestionJob, 3000);
          }
        } catch (error) {
          window.setTimeout(pollSuggestionJob, 6000);
        }
      }

      function updateTranscriptJob(job) {
        const panel = document.querySelector("[data-transcript-job]");
        if (!panel || !job) return;
        const message = panel.querySelector("[data-job-message]");
        const counts = panel.querySelector("[data-job-counts]");
        const current = panel.querySelector("[data-job-current]");
        const button = document.querySelector("[data-process-transcripts-button]");
        const priorState = panel.dataset.state || "";

        panel.hidden = !["running", "finished", "failed"].includes(job.state);
        panel.dataset.state = job.state || "";
        if (message) message.textContent = job.message || "Idle.";
        if (counts) {
          counts.textContent = `${job.processed || 0} / ${job.total || 0} processed · ${job.saved || 0} saved · ${job.errors || 0} errors`;
        }
        if (current) {
          current.textContent = job.current ? `Now transcribing ${job.current}` : "";
        }
        if (button) {
          button.disabled = job.state === "running";
        }
        if (priorState === "running" && ["finished", "failed"].includes(job.state)) {
          const tag = document.activeElement ? document.activeElement.tagName : "";
          const editing = ["INPUT", "TEXTAREA", "SELECT"].includes(tag);
          if (!editing && job.state === "finished") {
            saveOpenCards();
            window.setTimeout(() => window.location.reload(), 900);
          }
        }
      }

      async function pollTranscriptJob() {
        const panel = document.querySelector("[data-transcript-job]");
        if (!panel || !panel.dataset.statusUrl) return;
        try {
          const response = await fetch(panel.dataset.statusUrl, { headers: { "Accept": "application/json" } });
          if (!response.ok) return;
          const job = await response.json();
          updateTranscriptJob(job);
          if (job.state === "running") {
            window.setTimeout(pollTranscriptJob, 3000);
          }
        } catch (error) {
          window.setTimeout(pollTranscriptJob, 6000);
        }
      }

      restoreOpenCards();
      document.querySelectorAll(".review-card[open]").forEach(hydrateReviewAudio);
      if (document.querySelector("[data-suggestion-job]")?.dataset.state === "running") {
        pollSuggestionJob();
      }
      if (document.querySelector("[data-transcript-job]")?.dataset.state === "running") {
        pollTranscriptJob();
      }
    </script>
  </body>
</html>
"""


RECORDING_SHARE_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ title }}</title>
    <style>
      :root { color-scheme: dark; --bg:#07121e; --surface:#101e31; --line:rgba(143,211,255,.22); --text:#edf7ff; --muted:#a6b6c9; --accent:#8fd3ff; }
      * { box-sizing:border-box; }
      body { margin:0; min-height:100vh; display:grid; place-items:center; background:radial-gradient(circle at top left, rgba(143,211,255,.22), transparent 28rem), linear-gradient(145deg,#050913,var(--bg)); color:var(--text); font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
      main { width:min(680px, calc(100vw - 32px)); }
      section { border:1px solid var(--line); border-radius:28px; background:var(--surface); padding:34px; text-align:center; box-shadow:0 24px 80px rgba(0,0,0,.36); }
      h1 { margin:0 0 .75rem; font-size:clamp(32px,5vw,54px); line-height:1; letter-spacing:-.05em; }
      p { color:var(--muted); line-height:1.5; }
      ul { margin:1rem auto 0; max-width:32rem; padding-left:1.2rem; color:var(--muted); text-align:left; line-height:1.55; }
      audio { width:100%; margin-top:1rem; }
      .notice { border:1px solid var(--line); border-radius:16px; background:rgba(143,211,255,.1); color:var(--muted); margin-top:1rem; padding:.85rem 1rem; }
    </style>
  </head>
  <body>
    <main>
      <section>
        <h1>{{ title }}</h1>
        <p>Requested date: {{ format_date(request_row.requested_date) }}</p>
        {% if is_folder %}
          <p>This request is a collection folder.</p>
          {% if folder_items %}
            <ul>
              {% for item in folder_items[:12] %}
                <li>{{ item }}</li>
              {% endfor %}
              {% if folder_items|length > 12 %}
                <li>{{ folder_items|length - 12 }} more file{{ "" if folder_items|length - 12 == 1 else "s" }}</li>
              {% endif %}
            </ul>
          {% endif %}
        {% else %}
          <audio controls controlsList="nodownload" preload="metadata" src="{{ stream_url }}"></audio>
          <p class="notice">Download access is disabled for shared recording links.</p>
        {% endif %}
      </section>
    </main>
  </body>
</html>
"""


RECORDING_SHARE_MISSING_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Recording link unavailable</title>
    <style>
      :root { color-scheme: dark; --bg:#07121e; --surface:#101e31; --line:rgba(143,211,255,.22); --text:#edf7ff; --muted:#a6b6c9; }
      * { box-sizing:border-box; }
      body { margin:0; min-height:100vh; display:grid; place-items:center; background:radial-gradient(circle at top left, rgba(143,211,255,.16), transparent 28rem), linear-gradient(145deg,#050913,var(--bg)); color:var(--text); font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
      main { width:min(560px, calc(100vw - 32px)); }
      section { border:1px solid var(--line); border-radius:28px; background:var(--surface); padding:30px; text-align:center; box-shadow:0 24px 80px rgba(0,0,0,.36); }
      h1 { margin:0 0 .75rem; font-size:clamp(30px,5vw,48px); line-height:1; letter-spacing:-.05em; }
      p { margin:0; color:var(--muted); line-height:1.5; }
    </style>
  </head>
  <body>
    <main>
      <section>
        <h1>Recording link unavailable</h1>
        <p>This recording link was not found or is no longer available.</p>
      </section>
    </main>
  </body>
</html>
"""


app = create_app()


if __name__ == "__main__":
    host = os.getenv("NTC_RECORDINGS_HOST", "0.0.0.0")
    port = int(os.getenv("NTC_RECORDINGS_PORT", "7777"))
    app.run(host=host, port=port)
