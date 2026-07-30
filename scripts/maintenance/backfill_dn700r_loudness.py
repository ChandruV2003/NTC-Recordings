#!/usr/bin/env python3
"""Apply an audited, one-time loudness pass to NTC DN-700R recordings."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_MANIFEST = Path("/root/NTC-Runtime/autosyncmix/recorders/DN700R/manifest.sqlite3")
DEFAULT_REVIEW_DB = Path("/root/NTC-Runtime/recording-requests.db")
DEFAULT_STATE_ROOT = Path("/root/NTC-Runtime/audio-normalization")
DEFAULT_RECORDINGS_ROOT = Path("/mnt/MainRecordings/Recordings")
SUPPORTED_SUFFIXES = {".mp3"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(output: str) -> dict:
    decoder = json.JSONDecoder()
    for start in reversed([index for index, value in enumerate(output) if value == "{"]):
        try:
            payload, _ = decoder.raw_decode(output[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "input_i" in payload:
            return payload
    raise ValueError("ffmpeg did not return loudness measurements")


def _run(command: list[str], *, timeout: int = 7200) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _probe(path: Path) -> dict:
    completed = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels:format=duration",
            "-of",
            "json",
            str(path),
        ],
        timeout=120,
    )
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout or "ffprobe failed").strip())
    payload = json.loads(completed.stdout)
    stream = (payload.get("streams") or [{}])[0]
    return {
        "sample_rate": int(stream.get("sample_rate") or 48000),
        "channels": int(stream.get("channels") or 2),
        "duration": float((payload.get("format") or {}).get("duration") or 0),
    }


def _measure(path: Path, target_lufs: float, true_peak: float, loudness_range: float) -> dict:
    completed = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-af",
            f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={loudness_range}:print_format=json",
            "-f",
            "null",
            "-",
        ]
    )
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout or "loudness analysis failed").strip())
    return _json_object(completed.stderr)


def _normalize(
    source: Path,
    output: Path,
    *,
    measurement: dict,
    sample_rate: int,
    target_lufs: float,
    true_peak: float,
    loudness_range: float,
) -> dict:
    filter_value = (
        f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={loudness_range}:"
        f"measured_I={measurement['input_i']}:"
        f"measured_TP={measurement['input_tp']}:"
        f"measured_LRA={measurement['input_lra']}:"
        f"measured_thresh={measurement['input_thresh']}:"
        f"offset={measurement['target_offset']}:"
        "linear=false:print_format=json"
    )
    completed = _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-map_metadata",
            "0",
            "-af",
            filter_value,
            "-ar",
            str(sample_rate),
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "320k",
            str(output),
        ]
    )
    if completed.returncode or not output.exists() or output.stat().st_size <= 0:
        detail = (completed.stderr or completed.stdout or "normalization failed").strip()
        raise RuntimeError(detail[-800:])
    return _json_object(completed.stderr)


def _history_edges(review_db: Path) -> dict[str, str]:
    if not review_db.exists():
        return {}
    with sqlite3.connect(review_db) as connection:
        rows = connection.execute(
            """
            SELECT source_path, target_path
            FROM recorder_review_history
            WHERE source_path <> '' AND target_path <> ''
            ORDER BY id
            """
        ).fetchall()
    return {str(source): str(target) for source, target in rows if source and target}


def _terminal_path(value: str, edges: dict[str, str]) -> Path:
    current = value
    seen = set()
    while current in edges and current not in seen:
        seen.add(current)
        current = edges[current]
    return Path(current)


def _manifest_targets(manifest: Path, review_db: Path) -> list[Path]:
    if not manifest.exists():
        raise FileNotFoundError(f"DN-700R manifest not found: {manifest}")
    edges = _history_edges(review_db)
    with sqlite3.connect(manifest) as connection:
        rows = connection.execute(
            """
            SELECT staged_path, matched_path
            FROM recorder_files
            WHERE source_name = 'DN700R-primary'
            ORDER BY id
            """
        ).fetchall()
    targets = []
    seen_files = set()
    for staged_path, matched_path in rows:
        choices = []
        if matched_path:
            choices.append(_terminal_path(str(matched_path), edges))
        if staged_path:
            choices.append(_terminal_path(str(staged_path), edges))
        selected = next(
            (
                path
                for path in choices
                if path.exists() and path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
            ),
            None,
        )
        if not selected:
            continue
        stat = selected.stat()
        file_key = (stat.st_dev, stat.st_ino)
        if file_key in seen_files:
            continue
        seen_files.add(file_key)
        targets.append(selected)
    return sorted(targets, key=str)


def _completed_hashes(log_path: Path) -> dict[str, str]:
    completed = {}
    if not log_path.exists():
        return completed
    for line in log_path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("status") == "normalized" and entry.get("path") and entry.get("output_sha256"):
            completed[str(entry["path"])] = str(entry["output_sha256"])
    return completed


def _append_log(log_path: Path, entry: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=True, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="replace source files after backup and validation")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--review-db", type=Path, default=DEFAULT_REVIEW_DB)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--recordings-root", type=Path, default=DEFAULT_RECORDINGS_ROOT)
    parser.add_argument("--target-lufs", type=float, default=-18.0)
    parser.add_argument("--true-peak", type=float, default=-1.5)
    parser.add_argument("--loudness-range", type=float, default=11.0)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    state_root = args.state_root
    log_path = state_root / "dn700r-alc-backfill.jsonl"
    backup_root = state_root / "backups" / "DN700R-alc-backfill"
    lock_path = state_root / "dn700r-alc-backfill.lock"
    state_root.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another DN-700R loudness backfill is already running.", file=sys.stderr)
            return 2

        targets = _manifest_targets(args.manifest, args.review_db)
        if args.limit > 0:
            targets = targets[: args.limit]
        completed = _completed_hashes(log_path)
        pending = []
        for path in targets:
            current_hash = _sha256(path)
            if completed.get(str(path)) == current_hash:
                continue
            pending.append((path, current_hash))
        print(
            json.dumps(
                {
                    "mode": "apply" if args.apply else "dry-run",
                    "selected": len(targets),
                    "already_completed": len(targets) - len(pending),
                    "pending": len(pending),
                    "target_lufs": args.target_lufs,
                    "true_peak": args.true_peak,
                },
                sort_keys=True,
            )
        )
        for path, current_hash in pending:
            print(path)
            if not args.apply:
                continue
            entry = {
                "at": _utc_now(),
                "path": str(path),
                "input_sha256": current_hash,
                "status": "started",
            }
            try:
                try:
                    path.resolve().relative_to(args.recordings_root.resolve())
                except ValueError as exc:
                    raise RuntimeError(
                        f"{path} is outside the configured NTC recordings root"
                    ) from exc
                probe = _probe(path)
                measurement = _measure(path, args.target_lufs, args.true_peak, args.loudness_range)
                relative = path.resolve().relative_to(args.recordings_root.resolve())
                backup_path = backup_root / relative
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                if backup_path.exists():
                    if _sha256(backup_path) != current_hash:
                        raise RuntimeError(f"backup collision at {backup_path}")
                else:
                    shutil.copy2(path, backup_path)
                    if _sha256(backup_path) != current_hash:
                        raise RuntimeError(f"backup verification failed for {path.name}")
                with tempfile.NamedTemporaryFile(
                    prefix=f".{path.stem}.alc-",
                    suffix=path.suffix,
                    dir=path.parent,
                    delete=False,
                ) as temporary:
                    output_path = Path(temporary.name)
                try:
                    output_measurement = _normalize(
                        path,
                        output_path,
                        measurement=measurement,
                        sample_rate=probe["sample_rate"],
                        target_lufs=args.target_lufs,
                        true_peak=args.true_peak,
                        loudness_range=args.loudness_range,
                    )
                    output_probe = _probe(output_path)
                    duration_delta = abs(probe["duration"] - output_probe["duration"])
                    if duration_delta > 0.25:
                        raise RuntimeError(f"duration changed by {duration_delta:.3f} seconds")
                    output_hash = _sha256(output_path)
                    os.replace(output_path, path)
                finally:
                    if output_path.exists():
                        output_path.unlink()
                entry.update(
                    {
                        "status": "normalized",
                        "backup_path": str(backup_path),
                        "output_sha256": output_hash,
                        "input_lufs": float(measurement["input_i"]),
                        "input_true_peak": float(measurement["input_tp"]),
                        "output_lufs": float(output_measurement["output_i"]),
                        "output_true_peak": float(output_measurement["output_tp"]),
                        "sample_rate": probe["sample_rate"],
                        "duration_seconds": probe["duration"],
                    }
                )
            except Exception as exc:
                entry.update({"status": "error", "error": str(exc)})
            _append_log(log_path, entry)
            print(json.dumps(entry, sort_keys=True))
            if entry["status"] == "error":
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
