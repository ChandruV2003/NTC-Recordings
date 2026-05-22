"""Standalone recording request and share-link panel.

This panel intentionally uses its own database and service port so the
recording-request workflow cannot destabilize the WebCall/phone-call path.
"""

from __future__ import annotations

import hashlib
import html
import hmac
import os
import re
import secrets
import smtplib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable

import requests
from flask import Flask, jsonify, redirect, render_template_string, request, send_file, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from ntc_env import install_legacy_env_aliases
from ntc_branding import install_branding

install_legacy_env_aliases()


AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".aac"}
DEFAULT_MESSAGE_RECORDING_DIR = "/mnt/MainRecordings/Recordings/MessageRecordings"
DEFAULT_WORSHIP_RECORDING_DIR = "/mnt/MainRecordings/Recordings/WorshipRecordings"
DEFAULT_RECORDING_DIR = DEFAULT_MESSAGE_RECORDING_DIR
DEFAULT_RECORDING_DIRS = f"message:{DEFAULT_MESSAGE_RECORDING_DIR},worship:{DEFAULT_WORSHIP_RECORDING_DIR}"
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
        NTC_RECORDINGS_MAX_SCAN_FILES=int(os.getenv("NTC_RECORDINGS_MAX_SCAN_FILES", "4000")),
        NTC_RECORDINGS_INDEX_REFRESH_SECONDS=float(
            os.getenv(
                "NTC_RECORDINGS_INDEX_REFRESH_SECONDS",
                os.getenv("NTC_RECORDINGS_SCAN_CACHE_SECONDS", "300"),
            )
        ),
        NTC_RECORDINGS_PANEL_TITLE=os.getenv("NTC_RECORDINGS_PANEL_TITLE", "NTC NAS Recordings"),
        NTC_RECORDINGS_PUBLIC_BASE_URL=os.getenv("NTC_RECORDINGS_PUBLIC_BASE_URL", ""),
        NTC_RECORDINGS_SHARE_PROVIDER=os.getenv("NTC_RECORDINGS_SHARE_PROVIDER", "internal"),
        NTC_RECORDINGS_ADMIN_PASSWORD=os.getenv("NTC_RECORDINGS_ADMIN_PASSWORD", ""),
        NTC_ADMIN_PASSWORD=os.getenv("NTC_ADMIN_PASSWORD", ""),
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

    install_branding(app)
    _init_db(app.config["NTC_RECORDINGS_DB_PATH"])
    app.recordings_index_lock = threading.Lock()

    def _admin_password() -> str:
        return (
            app.config.get("NTC_RECORDINGS_ADMIN_PASSWORD")
            or app.config.get("NTC_ADMIN_PASSWORD")
            or ""
        ).strip()

    def _is_admin() -> bool:
        return bool(session.get("recordings_admin"))

    def _require_admin():
        if _is_admin():
            return None
        return redirect(url_for("admin_login", error="Admin password required."))

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
            return redirect(url_for("public_form", error="Name, email, and recording date are required."))
        recordings = _get_recordings(app)
        candidate = _default_candidate_for_date(recordings, requested_date, recording_kind)
        if not candidate:
            return redirect(url_for("public_form", error="Please choose one of the available recording dates."))
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
        return redirect(url_for("public_form", message="Request submitted. We will email the recording link when it is approved."))

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
                session["recordings_admin"] = True
                session.modified = True
                return redirect(url_for("admin_panel"))
            return render_template_string(
                RECORDING_ADMIN_LOGIN_TEMPLATE,
                title=app.config["NTC_RECORDINGS_PANEL_TITLE"],
                error="Password was not accepted.",
            )
        if _is_admin():
            return redirect(url_for("admin_panel"))
        return render_template_string(
            RECORDING_ADMIN_LOGIN_TEMPLATE,
            title=app.config["NTC_RECORDINGS_PANEL_TITLE"],
            error=request.args.get("error"),
        )

    @app.post("/admin/logout")
    def admin_logout():
        session.pop("recordings_admin", None)
        session.modified = True
        return redirect(url_for("public_form"))

    @app.get("/admin/panel")
    def admin_panel():
        guard = _require_admin()
        if guard:
            return guard
        _auto_archive_completed_requests(app)
        recordings = _get_recordings(app)
        requests = _list_requests(app)
        active_tab = (request.args.get("tab") or "pending").strip().lower()
        if active_tab not in {"pending", "completed", "archived"}:
            active_tab = "pending"
        pending_requests = [item for item in requests if not item["archived_at"] and item["status"] in {"pending", "ready"}]
        completed_requests = [item for item in requests if not item["archived_at"] and item["status"] in {"sent", "revoked"}]
        archived_requests = [item for item in requests if item["archived_at"]]
        if active_tab == "pending":
            visible_requests = pending_requests
        elif active_tab == "completed":
            visible_requests = completed_requests
        else:
            visible_requests = archived_requests
        candidates_by_request = {}
        for item in visible_requests:
            candidates_by_request[item["id"]] = _candidate_options_for_request(recordings, item)
        return render_template_string(
            RECORDING_ADMIN_TEMPLATE,
            title=app.config["NTC_RECORDINGS_PANEL_TITLE"],
            requests=visible_requests,
            pending_count=len(pending_requests),
            completed_count=len(completed_requests),
            archived_count=len(archived_requests),
            recording_count=len(recordings),
            recording_counts_by_kind=_recording_counts_by_kind(recordings),
            active_tab=active_tab,
            auto_archive_days=int(app.config.get("NTC_RECORDINGS_AUTO_ARCHIVE_DAYS") or 0),
            candidates_by_request=candidates_by_request,
            email_enabled=_email_enabled(app),
            share_provider=(app.config.get("NTC_RECORDINGS_SHARE_PROVIDER") or "internal"),
            message=request.args.get("message"),
            error=request.args.get("error"),
            default_email_message=_default_recording_email_message,
            status_label=_status_label,
            format_date=_format_date,
            format_datetime=_format_datetime,
        )

    @app.post("/admin/requests/<int:request_id>/send")
    def send_request_link(request_id: int):
        guard = _require_admin()
        if guard:
            return guard
        row = _get_request(app, request_id)
        if not row:
            return redirect(url_for("admin_panel", tab="pending", error="Request was not found."))
        recording_id = (request.form.get("recording_id") or "").strip()
        candidate = _recording_by_id(app, recording_id)
        if not candidate:
            return redirect(url_for("admin_panel", tab="pending", error="Selected recording was not found."))
        email_message = (request.form.get("email_message") or "").strip()
        if not email_message:
            email_message = _default_recording_email_message(row, candidate)
        token = row["share_token"] or secrets.token_urlsafe(22)
        share_url, share_provider, share_external_id, share_error = _create_share_link(app, candidate, token)
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
            return redirect(url_for("admin_panel", tab="completed", message=f"Recording link emailed to {row['email']}."))
        return redirect(url_for("admin_panel", tab="pending", message="Share link is ready."))

    @app.post("/admin/requests/<int:request_id>/revoke")
    def revoke_request_link(request_id: int):
        guard = _require_admin()
        if guard:
            return guard
        row = _get_request(app, request_id)
        if not row:
            return redirect(url_for("admin_panel", tab="pending", error="Request was not found."))
        target_tab = (request.form.get("tab") or "").strip().lower()
        if target_tab not in {"pending", "completed", "archived"}:
            target_tab = "completed"
        revoke_error = _revoke_share_link(app, row)
        _mark_request_revoked(app, request_id, revoke_error=revoke_error)
        if revoke_error:
            return redirect(url_for("admin_panel", tab=target_tab, error=f"Request closed locally. Revoke warning: {revoke_error}"))
        return redirect(url_for("admin_panel", tab=target_tab, message="Recording access revoked."))

    @app.post("/admin/requests/<int:request_id>/archive")
    def archive_request(request_id: int):
        guard = _require_admin()
        if guard:
            return guard
        row = _get_request(app, request_id)
        if not row:
            return redirect(url_for("admin_panel", tab="completed", error="Request was not found."))
        if row["status"] not in {"sent", "revoked"}:
            return redirect(url_for("admin_panel", tab="pending", error="Only completed or revoked requests can be archived."))
        _archive_request(app, request_id)
        return redirect(url_for("admin_panel", tab="archived", message="Request archived."))

    @app.get("/share/<token>")
    def share_recording(token: str):
        row = _get_request_by_token(app, token)
        if not row or not row["recording_path"]:
            return render_template_string(RECORDING_SHARE_MISSING_TEMPLATE), 404
        return render_template_string(
            RECORDING_SHARE_TEMPLATE,
            title=row["recording_title"] or "Requested Recording",
            request_row=row,
            download_url=url_for("download_recording", token=token),
            format_date=_format_date,
        )

    @app.get("/share/<token>/download")
    def download_recording(token: str):
        row = _get_request_by_token(app, token)
        if not row or not row["recording_path"]:
            return jsonify({"error": "share link was not found"}), 404
        path = Path(row["recording_path"])
        if not _path_allowed(app, path) or not path.exists() or not path.is_file():
            return jsonify({"error": "recording file is unavailable"}), 404
        return send_file(path, as_attachment=True, download_name=path.name, conditional=True)

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
        "sent": "Completed",
        "revoked": "Revoked",
    }
    return labels.get(status, status.replace("_", " ").title())


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
            WHERE id = ? AND status IN ('sent', 'revoked')
            """,
            (_utc_now(), request_id),
        )


def _auto_archive_completed_requests(app: Flask) -> int:
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
              AND status IN ('sent', 'revoked')
              AND COALESCE(revoked_at, sent_at, created_at) <= ?
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
    for root in _library_dirs(app):
        try:
            resolved.relative_to(root.resolve())
            return True
        except (FileNotFoundError, ValueError):
            continue
    return False


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
            try:
                stat = path.stat()
            except OSError:
                continue
            recording_date = _extract_recording_date(" ".join(path.parts)) or ""
            try:
                relative_path = str(path.relative_to(root))
            except ValueError:
                relative_path = path.name
            recordings.append(
                RecordingCandidate(
                    id=_recording_id(path),
                    path=str(path),
                    title=_display_title(path),
                    recording_date=recording_date,
                    kind=root_kind,
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
    counts = {"message": 0, "worship": 0, "unsure": 0}
    for recording in recordings:
        kind = recording.kind if recording.kind in counts else "unsure"
        counts[kind] += 1
    return counts


def _default_candidate_for_date(
    recordings: Iterable[RecordingCandidate],
    requested_date: str,
    recording_kind: str,
) -> RecordingCandidate | None:
    recordings_list = [item for item in recordings if item.recording_date == requested_date]
    if recording_kind in {"message", "worship"}:
        return next((item for item in recordings_list if item.kind == recording_kind), None)
    return recordings_list[0] if recordings_list else None


def _candidate_options_for_request(recordings: Iterable[RecordingCandidate], row: sqlite3.Row) -> list[RecordingCandidate]:
    recordings_list = list(recordings)
    selected = next((item for item in recordings_list if item.id == row["recording_id"]), None)
    requested_kind = _normalize_recording_kind(row["recording_kind"] if "recording_kind" in row.keys() else "")
    same_date = [
        item
        for item in recordings_list
        if item.recording_date == row["requested_date"]
        and item.id != row["recording_id"]
        and (requested_kind not in {"message", "worship"} or item.kind == requested_kind)
    ]
    fallback_same_date = [
        item
        for item in recordings_list
        if item.recording_date == row["requested_date"]
        and item.id != row["recording_id"]
        and item not in same_date
    ]
    ordered = []
    if selected:
        ordered.append(selected)
    ordered.extend(same_date[:7])
    if len(ordered) < 8:
        ordered.extend(fallback_same_date[: 8 - len(ordered)])
    return ordered[:8]


def _normalize_recording_kind(value: str) -> str:
    normalized = re.sub(r"[^a-z]+", "", str(value or "").lower())
    if normalized in {"worship", "worshiprecording", "music", "song"}:
        return "worship"
    if normalized in {"message", "messagerecording", "sermon", "teaching"}:
        return "message"
    return "unsure"


def _recording_kind_for_path(path: Path) -> str:
    normalized = re.sub(r"[^a-z]+", "", str(path).lower())
    if "worshiprecordings" in normalized or "worship" in normalized:
        return "worship"
    if "messagerecordings" in normalized or "message" in normalized:
        return "message"
    return "unsure"


def _recording_kind_label(kind: str) -> str:
    labels = {
        "message": "Message",
        "worship": "Worship",
        "unsure": "Recording",
    }
    return labels.get(kind, "Recording")


def _recording_id(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:24]


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


def _create_share_link(app: Flask, candidate: RecordingCandidate, token: str) -> tuple[str, str, str, str]:
    provider = str(app.config.get("NTC_RECORDINGS_SHARE_PROVIDER") or "internal").strip().lower()
    internal_url = _share_url(app, token)
    if provider != "nextcloud":
        return internal_url, "internal", "", ""

    nextcloud_url, nextcloud_share_id, error = _create_nextcloud_share_link(app, candidate)
    if nextcloud_url:
        return nextcloud_url, "nextcloud", nextcloud_share_id, ""
    return internal_url, "internal", "", f"Nextcloud share fallback: {error or 'not configured'}"


def _create_nextcloud_share_link(app: Flask, candidate: RecordingCandidate) -> tuple[str, str, str]:
    base_url = str(app.config.get("NTC_NEXTCLOUD_BASE_URL") or "").strip().rstrip("/")
    username = str(app.config.get("NTC_NEXTCLOUD_USERNAME") or "").strip()
    password = str(app.config.get("NTC_NEXTCLOUD_APP_PASSWORD") or "").strip()
    if not base_url or not username or not password:
        return "", "", "Nextcloud credentials are not configured"

    nextcloud_path = _nextcloud_path_for_candidate(app, candidate)
    if not nextcloud_path:
        return "", "", "recording path could not be mapped into Nextcloud"

    endpoint = f"{base_url}/ocs/v2.php/apps/files_sharing/api/v1/shares"
    try:
        response = requests.post(
            endpoint,
            params={"format": "json"},
            headers={"OCS-APIRequest": "true"},
            auth=(username, password),
            data={"path": nextcloud_path, "shareType": 3, "permissions": 1},
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
    return share_url, share_id, ""


def _revoke_share_link(app: Flask, row: sqlite3.Row) -> str:
    provider = str(row["share_provider"] or "").strip().lower()
    external_id = str(row["share_external_id"] or "").strip()
    if provider != "nextcloud" or not external_id:
        return ""

    base_url = str(app.config.get("NTC_NEXTCLOUD_BASE_URL") or "").strip().rstrip("/")
    username = str(app.config.get("NTC_NEXTCLOUD_USERNAME") or "").strip()
    password = str(app.config.get("NTC_NEXTCLOUD_APP_PASSWORD") or "").strip()
    if not base_url or not username or not password:
        return "Nextcloud credentials are not configured"

    endpoint = f"{base_url}/ocs/v2.php/apps/files_sharing/api/v1/shares/{external_id}"
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


def _nextcloud_path_for_candidate(app: Flask, candidate: RecordingCandidate) -> str:
    for local_prefix, nextcloud_prefix in _nextcloud_path_mappings(app):
        try:
            relative = Path(candidate.path).resolve().relative_to(local_prefix.resolve())
        except (FileNotFoundError, ValueError):
            continue
        parts = [part for part in relative.parts if part and part != "."]
        if nextcloud_prefix:
            return "/" + "/".join([nextcloud_prefix, *parts])
        return "/" + "/".join(parts)
    local_prefix = Path(str(app.config.get("NTC_NEXTCLOUD_LOCAL_PATH_PREFIX") or DEFAULT_RECORDING_DIR))
    nextcloud_prefix = str(app.config.get("NTC_NEXTCLOUD_PATH_PREFIX") or "").strip().strip("/")
    try:
        relative = Path(candidate.path).resolve().relative_to(local_prefix.resolve())
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
    subject = "Newark Worship Recording Request"
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
    return (
        "Praise the Lord,\n\n"
        f"Your requested recording from {_format_date(row['requested_date'])} is ready.\n\n"
        "Please use the link below to listen to or download the recording.\n\n"
        f"Recording: {candidate.title}\n\n"
        "God bless,\n"
        "NTC Newark"
    )


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
    safe_message = html.escape(email_message or _default_recording_email_message(row, candidate)).replace("\n", "<br>")
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
        --bg: #080d18;
        --panel: rgba(12, 22, 38, 0.9);
        --panel-2: rgba(19, 35, 58, 0.9);
        --line: rgba(144, 202, 255, 0.2);
        --text: #eef7ff;
        --muted: #a4b4c8;
        --accent: #8fd3ff;
        --good: #7be4bb;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        color: var(--text);
        font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      body::before {
        content: "";
        position: fixed;
        inset: 0;
        z-index: -1;
        pointer-events: none;
        background: url("{{ url_for('ntc_brand_background') }}") center / min(1120px, 118vw) auto no-repeat;
        opacity: 0.34;
        filter: saturate(1.08) contrast(1.04);
      }
      main { width: min(1060px, calc(100vw - 32px)); margin: 0 auto; padding: 34px 0 48px; }
      h1, h2, p { margin: 0; }
      h1 { font-size: clamp(34px, 5.4vw, 66px); letter-spacing: -0.055em; line-height: 0.94; }
      .eyebrow { color: var(--accent); font: 800 0.78rem ui-monospace, monospace; letter-spacing: 0.2em; text-transform: uppercase; }
      .hero { display: grid; gap: 0.75rem; margin-bottom: 1.2rem; }
      .hero p { max-width: 44rem; color: var(--muted); font-size: 1.05rem; line-height: 1.5; }
      .grid { display: grid; grid-template-columns: minmax(0, 1.14fr) minmax(18rem, 0.72fr); gap: 1rem; align-items: start; }
      .card {
        border: 1px solid var(--line);
        border-radius: 28px;
        background: var(--panel);
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
        background: var(--panel-2);
        color: var(--text);
        padding: 0.86rem 0.95rem;
        font: inherit;
      }
      textarea { min-height: 7rem; resize: vertical; }
      select { cursor: pointer; }
      select option { background:#13233a; color:var(--text); }
      button {
        cursor: pointer;
        border-color: rgba(143, 211, 255, 0.42);
        background: linear-gradient(135deg, rgba(143, 211, 255, 0.25), rgba(123, 228, 187, 0.14));
        font-weight: 900;
      }
      button:disabled, select:disabled { opacity: 0.55; cursor: not-allowed; }
      .banner { margin-bottom: 1rem; border: 1px solid rgba(123, 228, 187, 0.35); background: rgba(123, 228, 187, 0.12); color: var(--good); border-radius: 16px; padding: 0.9rem; font-weight: 800; }
      .banner.error { border-color: rgba(255, 154, 154, 0.4); background: rgba(255, 154, 154, 0.10); color: #ffaaaa; }
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
      .meta { color: var(--muted); font: 800 0.72rem ui-monospace, monospace; letter-spacing: 0.08em; text-transform: uppercase; }
      .muted { color: var(--muted); margin-top: 0.8rem; }
      .hint { color:var(--muted); font-size:.9rem; line-height:1.45; margin-top:-.35rem; }
      @media (max-width: 840px) { .grid, .form-grid { grid-template-columns: 1fr; } .wide { grid-column:auto; } }
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
          <form method="post" action="{{ url_for('create_request') }}">
            <div class="form-grid">
              <label>First and Last Name <input name="requester_name" autocomplete="name" required></label>
              <label>Email <input name="email" type="email" autocomplete="email" required></label>
              <label>Send Copy To <span class="optional">Optional</span><input name="secondary_email" type="email" autocomplete="email" placeholder="Optional"></label>
              <label>Phone Number <span class="optional">Optional</span><input name="phone" autocomplete="tel" placeholder="Optional"></label>
              <label>
                Recording Type
                <select name="recording_kind" required>
                  <option value="message">Message recording</option>
                  <option value="worship">Worship recording</option>
                  <option value="unsure">Not sure</option>
                </select>
              </label>
              <label>
                Service Date
                <select name="requested_date" required {% if not recording_dates %}disabled{% endif %}>
                  <option value="">Choose an available service date</option>
                  {% for recording in recording_dates %}
                    <option value="{{ recording.date }}" data-kinds="{{ recording.kinds|join(',') }}">{{ recording.label }}</option>
                  {% endfor %}
                </select>
              </label>
              <p class="hint wide">Only dates already in the recording library are shown. The exact file is confirmed before the link is sent.</p>
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
              <strong>1. Choose the service date</strong>
              <span>Only dates with available recordings can be selected.</span>
            </div>
            <div class="step">
              <strong>2. We confirm the file</strong>
              <span>The exact recording is selected internally before access is sent.</span>
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
    <script>
      (() => {
        const kindSelect = document.querySelector('select[name="recording_kind"]');
        const dateSelect = document.querySelector('select[name="requested_date"]');
        if (!kindSelect || !dateSelect) return;
        const dateOptions = Array.from(dateSelect.options).filter((option) => option.value);
        const filterDates = () => {
          const kind = kindSelect.value;
          let visibleCount = 0;
          for (const option of dateOptions) {
            const kinds = (option.dataset.kinds || "").split(",").filter(Boolean);
            const visible = kind === "unsure" || kinds.includes(kind);
            option.hidden = !visible;
            option.disabled = !visible;
            if (!visible && dateSelect.value === option.value) {
              dateSelect.value = "";
            }
            if (visible) visibleCount += 1;
          }
          dateSelect.disabled = visibleCount === 0;
        };
        kindSelect.addEventListener("change", filterDates);
        filterDates();
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
      :root { color-scheme: dark; --bg:#08111d; --panel:#101d30; --line:rgba(144,202,255,.22); --text:#eef7ff; --muted:#a8b6c8; --accent:#8fd3ff; }
      * { box-sizing: border-box; }
      body { margin:0; min-height:100vh; display:grid; place-items:center; background:radial-gradient(circle at top left, rgba(143,211,255,.2), transparent 26rem), linear-gradient(145deg,#050913,var(--bg)); color:var(--text); font-family:ui-sans-serif,system-ui,sans-serif; }
      main { width:min(520px, calc(100vw - 32px)); }
      section { border:1px solid var(--line); border-radius:28px; background:var(--panel); padding:30px; box-shadow:0 24px 80px rgba(0,0,0,.38); }
      h1 { margin:0 0 .5rem; font-size:clamp(32px,5vw,52px); letter-spacing:-.05em; }
      p { margin:0 0 1rem; color:var(--muted); }
      form { display:grid; gap:.8rem; }
      input, button { border:1px solid var(--line); border-radius:16px; background:#14243a; color:var(--text); padding:.9rem; font:inherit; }
      button { cursor:pointer; font-weight:900; background:rgba(143,211,255,.16); }
      .error { color:#ffaaaa; margin-bottom:1rem; font-weight:800; }
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
      :root { color-scheme: dark; --bg:#07121e; --panel:rgba(10,21,36,.92); --panel2:rgba(18,34,53,.9); --panel3:rgba(6,13,24,.58); --line:rgba(143,211,255,.2); --line2:rgba(143,211,255,.34); --text:#edf7ff; --muted:#9fb2c6; --accent:#8fd3ff; --good:#74ddb4; --warn:#ffc875; --bad:#ffaaa8; --ink:#06101d; }
      * { box-sizing:border-box; }
      body { margin:0; min-height:100vh; background:radial-gradient(circle at 10% 0%, rgba(143,211,255,.2), transparent 28rem), linear-gradient(145deg,#050913,var(--bg)); color:var(--text); font-family:ui-sans-serif,system-ui,sans-serif; }
      main { width:min(1400px, calc(100vw - 32px)); margin:0 auto; padding:30px 0 44px; }
      header { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:1rem; margin-bottom:1rem; align-items:start; }
      h1, h2, p { margin:0; }
      h1 { font-size:clamp(34px,5.2vw,64px); letter-spacing:-.055em; line-height:.95; }
      .eyebrow, .meta { color:var(--accent); font:800 .72rem ui-monospace,monospace; letter-spacing:.12em; text-transform:uppercase; }
      .actions { display:flex; gap:.6rem; flex-wrap:wrap; }
      a, button, select { border:1px solid var(--line); border-radius:14px; background:var(--panel2); color:var(--text); padding:.72rem .9rem; text-decoration:none; font:inherit; font-weight:850; }
      button { cursor:pointer; }
      .tabs { display:inline-flex; gap:.28rem; flex-wrap:wrap; margin:.9rem 0; padding:.28rem; border:1px solid var(--line); border-radius:999px; background:rgba(5,13,24,.58); box-shadow:inset 0 1px 0 rgba(255,255,255,.04); }
      .tab { display:flex; align-items:center; gap:.44rem; border:1px solid transparent; border-radius:999px; color:var(--muted); background:transparent; padding:.5rem .72rem; font-size:.84rem; line-height:1; transition:background .16s ease, border-color .16s ease, color .16s ease, transform .16s ease; }
      .tab:hover { color:var(--text); border-color:rgba(143,211,255,.22); background:rgba(143,211,255,.06); transform:translateY(-1px); }
      .tab.active { color:var(--text); background:linear-gradient(135deg,rgba(143,211,255,.18),rgba(143,245,200,.12)); border-color:rgba(143,211,255,.42); box-shadow:0 10px 26px rgba(8,19,33,.26), inset 0 0 0 1px rgba(255,255,255,.04); }
      .tab strong { display:inline-grid; place-items:center; min-width:1.45rem; min-height:1.45rem; padding:0 .34rem; border-radius:999px; background:rgba(143,211,255,.1); color:inherit; font-size:.76rem; }
      .tab.active strong { background:linear-gradient(135deg,#8fd3ff,#8ff5c8); color:var(--ink); }
      .metrics { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:.65rem; margin:.65rem 0 1rem; }
      .metric { border:1px solid var(--line); border-radius:18px; background:rgba(5,13,24,.58); padding:.78rem .85rem; min-width:0; }
      .metric span { display:block; color:var(--muted); font:800 .64rem ui-monospace,monospace; letter-spacing:.12em; text-transform:uppercase; }
      .metric strong { display:block; margin-top:.24rem; font-size:1.42rem; line-height:1; letter-spacing:-.04em; }
      .metric small { display:block; margin-top:.32rem; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .grid { display:grid; gap:1rem; align-items:start; }
      .card { border:1px solid var(--line); border-radius:24px; background:var(--panel); padding:1rem; box-shadow:0 22px 70px rgba(0,0,0,.34); }
      .section-head { display:flex; justify-content:space-between; align-items:end; gap:1rem; margin-bottom:.85rem; }
      .section-head p { max-width:46rem; }
      .request-list { display:grid; gap:.55rem; }
      .request-table-head { display:grid; grid-template-columns:minmax(12rem,1.05fr) minmax(8rem,.62fr) minmax(16rem,1.3fr) minmax(8rem,.62fr) minmax(6rem,.34fr) minmax(5.3rem,.36fr); gap:.78rem; padding:0 .9rem .25rem; color:var(--muted); font:800 .62rem ui-monospace,monospace; letter-spacing:.12em; text-transform:uppercase; }
      .request { border:1px solid rgba(143,211,255,.16); border-radius:16px; background:linear-gradient(135deg,rgba(255,255,255,.04),rgba(143,211,255,.018)); overflow:hidden; transition:border-color .18s ease, transform .18s ease, background .18s ease; }
      .request:hover { border-color:var(--line2); transform:translateY(-1px); background:linear-gradient(135deg,rgba(255,255,255,.055),rgba(143,211,255,.035)); }
      .request.sent { border-color:rgba(116,221,180,.28); }
      .request.revoked { opacity:.72; border-color:rgba(255,170,168,.22); }
      .request.archived { border-color:rgba(159,178,198,.22); }
      .request summary { list-style:none; cursor:pointer; padding:.78rem .86rem; }
      .request summary::-webkit-details-marker { display:none; }
      .request[open] summary { border-bottom:1px solid var(--line); background:rgba(143,211,255,.035); }
      .request-head { display:grid; grid-template-columns:minmax(12rem,1.05fr) minmax(8rem,.62fr) minmax(16rem,1.3fr) minmax(8rem,.62fr) minmax(6rem,.34fr) minmax(5.3rem,.36fr); align-items:center; gap:.78rem; }
      .request-title strong { display:block; font-size:1.1rem; }
      .request-subtitle { color:var(--muted); line-height:1.45; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .queue-cell { min-width:0; }
      .queue-label { display:block; margin-bottom:.18rem; color:var(--muted); font:800 .62rem ui-monospace,monospace; letter-spacing:.12em; text-transform:uppercase; }
      .queue-value { display:block; color:var(--text); font-weight:850; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .queue-subvalue { display:block; margin-top:.12rem; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .open-hint { display:inline-flex; align-items:center; justify-content:center; border:1px solid var(--line2); border-radius:999px; padding:.45rem .58rem; color:var(--accent); background:rgba(143,211,255,.075); font:900 .68rem ui-monospace,monospace; letter-spacing:.12em; text-transform:uppercase; }
      .pill { border:1px solid var(--line); border-radius:999px; padding:.35rem .58rem; color:var(--muted); font:800 .68rem ui-monospace,monospace; letter-spacing:.08em; text-transform:uppercase; justify-self:end; }
      .pill.pending { color:var(--warn); border-color:rgba(255,200,117,.35); }
      .pill.sent, .pill.ready { color:var(--good); border-color:rgba(116,221,180,.35); }
      .pill.revoked { color:var(--bad); border-color:rgba(255,170,168,.35); }
      .pill.archived { color:var(--muted); border-color:rgba(159,178,198,.28); }
      .request-body { display:grid; gap:.85rem; padding:1rem; }
      .detail-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:.55rem; }
      .detail { border:1px solid var(--line); border-radius:14px; background:var(--panel3); padding:.7rem .75rem; min-width:0; }
      .detail span { display:block; color:var(--muted); font:800 .66rem ui-monospace,monospace; letter-spacing:.1em; text-transform:uppercase; }
      .detail strong { display:block; margin-top:.28rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .approve-form { display:grid; gap:.72rem; border:1px solid var(--line); border-radius:18px; padding:.85rem; background:rgba(143,211,255,.055); }
      .approve-form label { display:grid; gap:.35rem; color:var(--muted); font-weight:850; }
      .approve-form select { width:100%; min-height:3rem; background:rgba(4,11,20,.56); }
      .approve-form option { background:#13233a; color:var(--text); }
      .approve-footer { display:flex; gap:.65rem; align-items:center; justify-content:space-between; flex-wrap:wrap; }
      .approve-footer button { width:auto; white-space:nowrap; background:rgba(116,221,180,.12); border-color:rgba(116,221,180,.35); }
      .request-actions { display:flex; gap:.55rem; flex-wrap:wrap; align-items:center; }
      .request-actions form { margin:0; }
      .danger { color:#ffd7d7; border-color:rgba(255,170,168,.35); background:rgba(255,170,168,.1); }
      .email-note { display:grid; gap:.35rem; color:var(--muted); font-weight:850; }
      .email-note textarea { min-height:8rem; resize:vertical; border:1px solid var(--line); border-radius:14px; background:rgba(4,11,20,.56); color:var(--text); padding:.82rem; font:inherit; line-height:1.45; }
      .muted { color:var(--muted); }
      .banner { margin-bottom:1rem; border:1px solid rgba(116,221,180,.35); background:rgba(116,221,180,.1); color:var(--good); border-radius:16px; padding:.85rem; font-weight:850; }
      .banner.error { border-color:rgba(255,154,154,.4); background:rgba(255,154,154,.1); color:#ffaaaa; }
      @media (max-width:1100px) { .metrics { grid-template-columns:repeat(2,minmax(0,1fr)); } .request-table-head { display:none; } .request-head { grid-template-columns:minmax(0,1fr) minmax(0,1fr); align-items:start; } .detail-grid { grid-template-columns:1fr 1fr; } .pill, .open-hint { justify-self:start; } }
      @media (max-width:760px) { header, .section-head { display:flex; align-items:start; flex-direction:column; } .metrics { grid-template-columns:1fr; } .detail-grid { grid-template-columns:1fr; } .request-head { grid-template-columns:1fr; } .pill, .open-hint { justify-self:start; } }
    </style>
  </head>
  <body>
    <main>
      <header>
        <div>
          <div class="eyebrow">NTC Newark</div>
          <h1>Recording Requests</h1>
          <p class="muted">Approve message and worship recording requests from one queue.</p>
        </div>
        <div class="actions">
          <a href="{{ url_for('public_form') }}">Public Form</a>
          <form method="post" action="{{ url_for('admin_logout') }}"><button type="submit">Sign Out</button></form>
        </div>
      </header>
      {% if message %}<div class="banner">{{ message }}</div>{% endif %}
      {% if error %}<div class="banner error">{{ error }}</div>{% endif %}
      <section class="metrics" aria-label="Recording request status">
        <div class="metric"><span>Pending</span><strong>{{ pending_count }}</strong><small>Needs review</small></div>
        <div class="metric"><span>Completed</span><strong>{{ completed_count }}</strong><small>Sent or revoked</small></div>
        <div class="metric"><span>Library</span><strong>{{ recording_count }}</strong><small>{{ recording_counts_by_kind.get("message", 0) }} messages · {{ recording_counts_by_kind.get("worship", 0) }} worship</small></div>
        <div class="metric"><span>Delivery</span><strong>{{ "Email" if email_enabled else "Link" }}</strong><small>{{ share_provider|title }} sharing</small></div>
      </section>
      <nav class="tabs" aria-label="Request list">
        <a class="tab {{ 'active' if active_tab == 'pending' else '' }}" {% if active_tab == "pending" %}aria-current="page"{% endif %} href="{{ url_for('admin_panel', tab='pending') }}">Pending <strong>{{ pending_count }}</strong></a>
        <a class="tab {{ 'active' if active_tab == 'completed' else '' }}" {% if active_tab == "completed" %}aria-current="page"{% endif %} href="{{ url_for('admin_panel', tab='completed') }}">Completed <strong>{{ completed_count }}</strong></a>
        <a class="tab {{ 'active' if active_tab == 'archived' else '' }}" {% if active_tab == "archived" %}aria-current="page"{% endif %} href="{{ url_for('admin_panel', tab='archived') }}">Archived <strong>{{ archived_count }}</strong></a>
      </nav>
      <div class="grid">
        <section class="card">
          <div class="section-head">
            <div>
              <h2>{{ "Pending Requests" if active_tab == "pending" else ("Completed Requests" if active_tab == "completed" else "Archived Requests") }}</h2>
              <p class="muted">
                {% if active_tab == "pending" %}
                  New requests. Open a row, confirm the selected file, then send the link.
                {% elif active_tab == "completed" %}
                  Completed and revoked requests stay here until they are archived. Revoke access when a prepared link should stop working.
                {% else %}
                  Archived requests are kept for history. Active share links still work unless access is revoked.
                {% endif %}
              </p>
              {% if active_tab == "completed" and auto_archive_days > 0 %}
                <p class="muted">Auto-archive is on: completed requests move to Archived after {{ auto_archive_days }} days.</p>
              {% endif %}
            </div>
            <span class="pill">{{ requests|length }} request{{ "" if requests|length == 1 else "s" }}</span>
          </div>
          <div class="request-list">
          {% if requests %}
            <div class="request-table-head" aria-hidden="true">
              <span>Requester</span>
              <span>Date</span>
              <span>File</span>
              <span>Submitted</span>
              <span>Status</span>
              <span>Action</span>
            </div>
          {% endif %}
          {% for item in requests %}
            <details class="request {{ 'archived' if item.archived_at else item.status }}">
              <summary>
                <div class="request-head">
                  <div class="request-title queue-cell">
                    <span class="queue-label">Requester</span>
                    <strong class="queue-value">{{ item.requester_name }}</strong>
                    <span class="queue-subvalue">{{ item.email }}</span>
                  </div>
                  <div class="queue-cell">
                    <span class="queue-label">Recording</span>
                    <span class="queue-value">{{ format_date(item.requested_date) }}</span>
                    <span class="queue-subvalue">{{ item.recording_kind|title if item.recording_kind else "Message" }}</span>
                  </div>
                  <div class="queue-cell">
                    <span class="queue-label">Selected File</span>
                    <span class="queue-value">{{ item.recording_title or "Selected by date" }}</span>
                    <span class="queue-subvalue">{% if item.secondary_email %}CC {{ item.secondary_email }}{% elif item.phone %}{{ item.phone }}{% else %}No extra contact{% endif %}</span>
                  </div>
                  <div class="queue-cell">
                    <span class="queue-label">Submitted</span>
                    <span class="queue-value">{{ format_datetime(item.created_at) }}</span>
                  </div>
                  <span class="pill {{ 'archived' if item.archived_at else item.status }}">{{ "Archived" if item.archived_at else status_label(item.status) }}</span>
                  <span class="open-hint">Review</span>
                </div>
              </summary>
              <div class="request-body">
                <div class="detail-grid">
                  <div class="detail"><span>Send To</span><strong>{{ item.email }}</strong></div>
                  <div class="detail"><span>Additional Recipient</span><strong>{{ item.secondary_email or "None" }}</strong></div>
                  <div class="detail"><span>Phone</span><strong>{{ item.phone or "None" }}</strong></div>
                  <div class="detail"><span>Type</span><strong>{{ item.recording_kind|title if item.recording_kind else "Message" }}</strong></div>
                  <div class="detail"><span>{{ "Archived" if item.archived_at else ("Completed" if item.status == "sent" else ("Revoked" if item.status == "revoked" else "Status")) }}</span><strong>{{ format_datetime(item.archived_at) if item.archived_at else (format_datetime(item.sent_at) if item.status == "sent" else (format_datetime(item.revoked_at) if item.status == "revoked" else status_label(item.status))) }}</strong></div>
                </div>
                {% if item.recording_title %}
                  <div class="detail"><span>Requested Recording</span><strong>{{ item.recording_title }}</strong></div>
                {% endif %}
                {% if item.notes %}<p>{{ item.notes }}</p>{% endif %}
                {% if item.email_error %}
                  <p class="muted">Delivery note: {{ item.email_error }}</p>
                {% endif %}
                {% if item.share_token and item.status != "revoked" %}
                  <div class="request-actions">
                    <a href="{{ item.share_url or url_for('share_recording', token=item.share_token) }}">Open prepared share link</a>
                    <form method="post" action="{{ url_for('revoke_request_link', request_id=item.id) }}">
                      <input type="hidden" name="tab" value="{{ active_tab }}">
                      <button class="danger" type="submit">Revoke Access</button>
                    </form>
                    {% if active_tab == "completed" %}
                      <form method="post" action="{{ url_for('archive_request', request_id=item.id) }}">
                        <button type="submit">Archive</button>
                      </form>
                    {% endif %}
                  </div>
                  {% if item.share_provider %}<div class="meta">Share provider: {{ item.share_provider }}</div>{% endif %}
                {% elif active_tab == "completed" and item.status in ["sent", "revoked"] %}
                  <div class="request-actions">
                    <form method="post" action="{{ url_for('archive_request', request_id=item.id) }}">
                      <button type="submit">Archive</button>
                    </form>
                  </div>
                {% elif item.status == "pending" %}
                  <div class="request-actions">
                    <form method="post" action="{{ url_for('revoke_request_link', request_id=item.id) }}">
                      <input type="hidden" name="tab" value="completed">
                      <button class="danger" type="submit">Close Request</button>
                    </form>
                  </div>
                {% endif %}
                {% if item.status != "revoked" %}
                  {% set candidates = candidates_by_request.get(item.id, []) %}
                  {% if candidates %}
                    <form class="approve-form" method="post" action="{{ url_for('send_request_link', request_id=item.id) }}">
                      <label>
                        Selected file
                        <select name="recording_id" required>
                          {% for candidate in candidates %}
                            <option value="{{ candidate.id }}" data-title="{{ candidate.title }}" {% if candidate.id == item.recording_id %}selected{% endif %}>
                              {{ format_date(candidate.recording_date) }} - {{ candidate.kind|title }} - {{ candidate.title }} · {{ candidate.relative_path }}
                            </option>
                          {% endfor %}
                        </select>
                      </label>
                      <div class="meta">Options are limited to recordings found for the requested date. Change this only if the automatic match is wrong.</div>
                      <label class="email-note">
                        Email message
                        <textarea name="email_message">{{ item.email_message or default_email_message(item, candidates[0]) }}</textarea>
                      </label>
                      <div class="approve-footer">
                        <span class="muted">Review the selected file before sending access.</span>
                        <button type="submit">{{ "Send Link" if email_enabled else "Prepare Link" }}</button>
                      </div>
                    </form>
                  {% else %}
                    <p class="muted">No exact date match found. Confirm the request date or rename the recording file with the service date.</p>
                  {% endif %}
                {% endif %}
              </div>
            </details>
          {% else %}
            <p class="muted">{{ "No pending requests." if active_tab == "pending" else ("No completed requests yet." if active_tab == "completed" else "No archived requests yet.") }}</p>
          {% endfor %}
          </div>
        </section>
      </div>
    </main>
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
      :root { color-scheme: dark; --bg:#07121e; --panel:#101e31; --line:rgba(143,211,255,.22); --text:#edf7ff; --muted:#a6b6c9; --accent:#8fd3ff; }
      * { box-sizing:border-box; }
      body { margin:0; min-height:100vh; display:grid; place-items:center; background:radial-gradient(circle at top left, rgba(143,211,255,.22), transparent 28rem), linear-gradient(145deg,#050913,var(--bg)); color:var(--text); font-family:ui-sans-serif,system-ui,sans-serif; }
      main { width:min(680px, calc(100vw - 32px)); }
      section { border:1px solid var(--line); border-radius:28px; background:var(--panel); padding:34px; text-align:center; box-shadow:0 24px 80px rgba(0,0,0,.36); }
      h1 { margin:0 0 .75rem; font-size:clamp(32px,5vw,54px); letter-spacing:-.05em; }
      p { color:var(--muted); line-height:1.5; }
      a { display:inline-block; margin-top:1rem; border:1px solid var(--line); border-radius:16px; background:rgba(143,211,255,.14); color:var(--text); padding:.9rem 1.1rem; text-decoration:none; font-weight:900; }
    </style>
  </head>
  <body>
    <main>
      <section>
        <h1>{{ title }}</h1>
        <p>Requested date: {{ format_date(request_row.requested_date) }}</p>
        <a href="{{ download_url }}">Download Recording</a>
      </section>
    </main>
  </body>
</html>
"""


RECORDING_SHARE_MISSING_TEMPLATE = """
<!doctype html>
<html lang="en"><body><h1>Recording link unavailable</h1><p>This recording link was not found or is no longer available.</p></body></html>
"""


app = create_app()


if __name__ == "__main__":
    host = os.getenv("NTC_RECORDINGS_HOST", "0.0.0.0")
    port = int(os.getenv("NTC_RECORDINGS_PORT", "7777"))
    app.run(host=host, port=port)
