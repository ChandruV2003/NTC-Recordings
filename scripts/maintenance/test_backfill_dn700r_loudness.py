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
