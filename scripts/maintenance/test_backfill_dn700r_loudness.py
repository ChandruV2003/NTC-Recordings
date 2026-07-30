import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("backfill_dn700r_loudness.py")
SPEC = importlib.util.spec_from_file_location("backfill_dn700r_loudness", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class DN700RLoudnessBackfillTests(unittest.TestCase):
    def test_completed_hashes_include_skipped_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "audit.jsonl"
            log_path.write_text(
                "\n".join(
                    [
                        '{"status":"normalized","path":"/one.mp3","output_sha256":"output"}',
                        '{"status":"skipped","path":"/two.mp3","input_sha256":"input"}',
                        '{"status":"error","path":"/three.mp3","input_sha256":"error"}',
                    ]
                )
            )

            self.assertEqual(
                MODULE._completed_hashes(log_path),
                {"/one.mp3": "output", "/two.mp3": "input"},
            )

    def test_json_object_ignores_ffmpeg_trailer_text(self):
        output = """
        [Parsed_loudnorm_0]
        {
            "input_i": "-25.10",
            "input_tp": "-1.52",
            "input_lra": "6.20",
            "input_thresh": "-35.20",
            "output_i": "-18.01",
            "output_tp": "-1.50",
            "output_lra": "5.90",
            "output_thresh": "-28.10",
            "normalization_type": "dynamic",
            "target_offset": "0.01"
        }
        video:0KiB audio:1024KiB muxing overhead: 0.1%
        """

        self.assertEqual(MODULE._json_object(output)["output_i"], "-18.01")

    def test_manifest_targets_follow_review_moves_and_exclude_other_lanes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.sqlite3"
            review_db = root / "review.sqlite3"
            staged = root / "intake.mp3"
            final = root / "TestimonyRecordings" / "July 26, 2026 - Speaker's Testimony.mp3"
            cvav = root / "cvav.mp3"
            duplicate_link = root / "duplicate.mp3"
            final.parent.mkdir()
            staged.write_bytes(b"staged")
            final.write_bytes(b"final")
            cvav.write_bytes(b"cvav")
            duplicate_link.hardlink_to(final)

            with sqlite3.connect(manifest) as connection:
                connection.execute(
                    """
                    CREATE TABLE recorder_files (
                        id INTEGER PRIMARY KEY,
                        source_name TEXT,
                        staged_path TEXT,
                        matched_path TEXT
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO recorder_files (source_name, staged_path, matched_path)
                    VALUES (?, ?, ?)
                    """,
                    [
                        ("DN700R-primary", str(staged), ""),
                        ("DN700R-primary", str(duplicate_link), str(final)),
                        ("CVAV-DN700R", str(cvav), ""),
                    ],
                )
            with sqlite3.connect(review_db) as connection:
                connection.execute(
                    """
                    CREATE TABLE recorder_review_history (
                        id INTEGER PRIMARY KEY,
                        source_path TEXT,
                        target_path TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO recorder_review_history (source_path, target_path)
                    VALUES (?, ?)
                    """,
                    (str(staged), str(final)),
                )

            self.assertEqual(
                MODULE._manifest_targets(manifest, review_db),
                [final],
            )


if __name__ == "__main__":
    unittest.main()
