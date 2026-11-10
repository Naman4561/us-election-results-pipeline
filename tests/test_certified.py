"""Certified results imported from a separate certified file (``deploy certify
--certified-file``): the operator's transcription of a state's certificate.

Each transcription is built from South Dakota's recorded official feed, so its
candidates and counties are the reviewed roster's.
"""

import csv
import hashlib
import io
import os
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from loading import migrated
from test_new_states import members

from election_pipeline.manual import import_file
from election_pipeline.parsers.certified import HEADER, TRANSCRIPTION
from election_pipeline.parsing import parse_source
from election_pipeline.settings import load_config
from election_pipeline.storage import ArchiveStore

ROOT = Path(__file__).resolve().parents[1]


class CertifiedFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings, sources = load_config(ROOT / "config/sources.toml")
        self.source = sources["sd_2026_general"]
        self.rows = self.feed_rows()

    def feed_rows(self):
        """South Dakota's feed, parsed, as transcription rows."""
        raw = self.root / "feed.zip"
        with zipfile.ZipFile(raw, "w") as archive:
            for name, value in members("SD").items():
                archive.writestr(name, value)
        sha = hashlib.sha256(raw.read_bytes()).hexdigest()
        target = ArchiveStore(self.root, self.source).archive_path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw.read_bytes())
        report = parse_source(self.source, self.settings, self.root, sha256=sha)
        self.assertEqual(report["outcome"], "parsed", report)
        self.feed = report
        with (Path(report["output_dir"]) / "results.csv").open(encoding="utf-8") as handle:
            return [
                {
                    "contest_id": found["contest_id"],
                    "county": found["county"],
                    "source_contest_id": found["source_contest_id"],
                    "source_candidate_id": found["source_candidate_id"],
                    "choice": found["choice"],
                    "party": found["choice_party"],
                    "votes": found["total_votes"],
                }
                for found in csv.DictReader(handle)
            ]

    def transcribe(self, rows):
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, HEADER)
        writer.writeheader()
        writer.writerows(rows)
        path = self.root / "inbox" / "sd-certified.csv"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(stream.getvalue().encode("utf-8"))
        return path

    def imported(self, rows, name=TRANSCRIPTION):
        path = self.transcribe(rows)
        return import_file(
            self.source,
            self.settings,
            self.root,
            path,
            datetime.now(timezone.utc).isoformat(),
            hashlib.sha256(path.read_bytes()).hexdigest(),
            certified=name,
        )

    def test_a_transcription_is_archived_alone_and_parses_to_the_same_totals(self):
        receipt = self.imported(self.rows)
        self.assertEqual(receipt["certified_file"], TRANSCRIPTION)
        self.assertIsNone(receipt["source_generated_at"])
        archive = ArchiveStore(self.root, self.source).archive_path(receipt["sha256"])
        with zipfile.ZipFile(archive) as zipped:
            self.assertEqual(zipped.namelist(), [TRANSCRIPTION])
        report = parse_source(self.source, self.settings, self.root, sha256=receipt["sha256"])
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["certified_file"], TRANSCRIPTION)
        self.assertEqual(report["contest_total_reconciliation"], "passed")
        self.assertNotIn("official_results", report)  # the attestation makes it official

    def test_statewide_only_certificates_are_accepted(self):
        statewide = [r for r in self.rows if not r["county"]]
        report = parse_source(
            self.source,
            self.settings,
            self.root,
            sha256=self.imported(statewide)["sha256"],
        )
        self.assertEqual(report["outcome"], "parsed", report)
        self.assertEqual(report["contest_total_reconciliation"], "statewide_only")

    def test_transcriptions_that_disagree_or_leave_anything_out_are_refused(self):
        def county_off_by_one(rows):
            first = next(r for r in rows if r["county"])
            first["votes"] = str(int(first["votes"]) + 1)

        def missing_contest(rows):
            rows[:] = [r for r in rows if r["contest_id"] != "SD-GOV"]

        def stranger(rows):
            for found in rows:
                if found["contest_id"] == "SD-AL":
                    found["source_candidate_id"] = "99999"

        def missing_county(rows):
            county = next(r["county"] for r in rows if r["county"])
            rows[:] = [r for r in rows if r["county"] != county]

        def fraction(rows):
            rows[0]["votes"] = "12.5"

        for mutate in (county_off_by_one, missing_contest, stranger, missing_county, fraction):
            rows = [dict(r) for r in self.rows]
            mutate(rows)
            with self.subTest(mutate.__name__), self.assertRaisesRegex(ValueError, "rejected"):
                self.imported(rows)

    def test_only_certified_names_and_known_formats_are_imported(self):
        with self.assertRaisesRegex(ValueError, "starts with certified-"):
            self.imported(self.rows, name="results.csv")
        with self.assertRaisesRegex(ValueError, "no reader for the certified file"):
            self.imported(self.rows, name="certified-recap.csv")


ATTESTED = {
    "evidence_url": "https://sdsos.gov/elections-voting/certified.pdf",
    "certified_on": "2026-11-10",
    "attested_by": "Operator",
    "attested_at": "2026-11-10T20:00:00+00:00",
}


@unittest.skipUnless(
    os.getenv("ELECTION_TEST_DATABASE"), "Requires disposable native PostgreSQL *_test"
)
class AttestedPublicationDatabaseTests(unittest.TestCase):
    def test_both_attested_modes_publish_official_results_with_their_attestation(self):
        from election_pipeline import database, publication

        db = migrated()
        with database.connect(db) as conn:
            conn.execute("TRUNCATE ops.sources CASCADE")
        f = CertifiedFileTests()
        f.setUp()
        self.addCleanup(f.doCleanups)

        def load(report):
            loaded = database.load_parsed(
                f.source, f.settings, f.root, Path(report["output_dir"]), db
            )
            self.assertEqual(loaded["outcome"], "succeeded", loaded)
            return loaded["release_id"]

        def attestation(publication_id):
            with database.connect(db) as conn:
                return conn.execute(
                    "SELECT status, attestation FROM serving.publications WHERE publication_id=%s",
                    (publication_id,),
                ).fetchone()

        feed = load(f.feed)
        live = publication.publish(ROOT, db, feed, "unofficial")
        self.assertEqual(attestation(live["publication_id"])["attestation"], None)
        # The feed has no official marker; without an attestation it stays unofficial.
        with self.assertRaisesRegex(ValueError, "does not mark this snapshot as official"):
            publication.publish(ROOT, db, feed, "official")

        approved = {**ATTESTED, "mode": "approved_snapshot"}
        official = publication.publish(ROOT, db, feed, "official", attested=approved)
        self.assertEqual(
            attestation(official["publication_id"]), {"status": "official", "attestation": approved}
        )

        receipt = f.imported(f.rows)
        report = parse_source(f.source, f.settings, f.root, sha256=receipt["sha256"])
        certified_release = load(report)
        # An approval is only for the snapshot already shown; a certified file is new.
        with self.assertRaisesRegex(ValueError, "currently published snapshot"):
            publication.publish(ROOT, db, certified_release, "official", attested=approved)
        filed = {**ATTESTED, "mode": "certified_file"}
        published = publication.publish(ROOT, db, certified_release, "official", attested=filed)
        self.assertEqual(
            attestation(published["publication_id"]), {"status": "official", "attestation": filed}
        )
        with database.connect(db) as conn, self.assertRaises(Exception):
            conn.execute(
                "UPDATE serving.publications SET status='unofficial' WHERE publication_id=%s",
                (published["publication_id"],),
            )


if __name__ == "__main__":
    unittest.main()
