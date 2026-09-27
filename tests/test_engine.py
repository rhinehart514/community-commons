import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from bp_commons import Commons
from bp_commons.importer import import_snapshot


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "commons.sqlite"
        self.person = {"record_id": "p1", "name": "Example Researcher", "collector": "test",
                       "source_url": "https://example.org/roster", "profile_url": "https://example.org/person",
                       "regional_evidence": "Listed on a 2023 roster", "role": "Researcher"}
        self.observation = {**self.person, "profile_url": "https://example.org/alias"}
        self.edge = {"subject_url": "https://example.org/alias", "subject_name": "Example Researcher",
                     "relation": "authored", "object_name": "Example paper", "source_url": "https://example.org/paper",
                     "evidence": "Research about computer vision", "evidence_date": "2023"}
        self.write("people", [self.person])
        self.write("evidence", [self.observation])
        self.write("connections", [self.edge])

    def write(self, name, rows):
        (self.root / (name + ".jsonl")).write_text("".join(json.dumps(row) + "\n" for row in rows))

    def test_alias_search_and_evidence_preservation(self):
        import_snapshot(self.root, self.database)
        with Commons(self.database) as store:
            self.assertEqual(store.search("computer vision")[0]["record_id"], "p1")
            self.assertEqual(store.profile("p1")["relationships"], [self.edge])
            self.assertEqual(store.stats()["unresolved_relationships"], 0)
            self.assertEqual(store.search('" OR *'), [])
            self.assertEqual(store.search("vision", collector="other"), [])

    def test_invalid_import_preserves_existing_snapshot(self):
        import_snapshot(self.root, self.database)
        original = self.database.read_bytes()
        self.write("evidence", [{**self.observation, "record_id": "missing"}])
        with self.assertRaises(sqlite3.IntegrityError):
            import_snapshot(self.root, self.database)
        self.assertEqual(self.database.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".bp-import-*")), [])

    def test_ambiguous_alias_is_not_assumed_to_be_a_connection(self):
        self.write("people", [self.person, {**self.person, "record_id": "p2"}])
        self.write("evidence", [self.observation, {**self.observation, "record_id": "p2"}])
        import_snapshot(self.root, self.database)
        with Commons(self.database) as store:
            self.assertEqual(store.stats()["unresolved_relationships"], 1)
            self.assertEqual(store.unresolved()[0]["evidence"], self.edge)
            self.assertEqual(store.profile("p1")["relationships"], [])

    def test_reimport_does_not_duplicate_records(self):
        import_snapshot(self.root, self.database)
        import_snapshot(self.root, self.database)
        with Commons(self.database) as store:
            self.assertEqual(store.stats()["profiles"], 1)
            self.assertEqual(store.stats()["relationships"], 1)

    def test_refresh_reports_changes_and_preserves_previous_profile(self):
        first = import_snapshot(self.root, self.database)
        updated = {**self.person, "role": "Professor"}
        added = {**self.person, "record_id": "p2", "name": "Second Person", "profile_url": "https://example.org/second"}
        self.write("people", [updated, added])
        self.write("connections", [])
        report = import_snapshot(self.root, self.database)
        self.assertEqual(report["changes"]["profiles"], {"added": 1, "changed": 1, "removed": 0})
        self.assertEqual(report["changes"]["relationships"]["removed"], 1)
        with Commons(self.database) as store:
            self.assertEqual(store.profile("p1")["profile"]["role"], "Professor")
            prior = store.profile("p1", snapshot_id=first["snapshot_id"])
            self.assertEqual(prior["profile"]["role"], "Researcher")
            self.assertEqual(prior["relationships"], [self.edge])
            self.assertEqual(len(store.history()), 2)
            changes = store.changes(report["snapshot_id"])
            self.assertEqual(len(changes), 3)

    def test_same_files_are_a_noop_and_observation_time_is_not_semantic_change(self):
        first = import_snapshot(self.root, self.database)
        original = self.database.read_bytes()
        repeat = import_snapshot(self.root, self.database)
        self.assertEqual(repeat["status"], "unchanged")
        self.assertEqual(repeat["snapshot_id"], first["snapshot_id"])
        self.assertEqual(original, self.database.read_bytes())
        self.write("evidence", [{**self.observation, "observed_at": "2026-09-26T00:00:00Z"}])
        report = import_snapshot(self.root, self.database)
        self.assertEqual(report["changes"]["observations"], {"added": 0, "changed": 0, "removed": 0})
        with Commons(self.database) as store:
            self.assertEqual(len(store.history()), 2)
            self.assertEqual(store.profile("p1")["observations"][0]["observed_at"], "2026-09-26T00:00:00Z")

    def test_removal_does_not_erase_history_and_can_reappear(self):
        self.write("people", [self.person, {**self.person, "record_id": "p2"}])
        first = import_snapshot(self.root, self.database)
        self.write("people", [{**self.person, "record_id": "p2"}])
        self.write("evidence", [])
        self.write("connections", [])
        removed = import_snapshot(self.root, self.database)
        self.assertEqual(removed["changes"]["profiles"]["removed"], 1)
        with Commons(self.database) as store:
            with self.assertRaises(KeyError):
                store.profile("p1")
            self.assertEqual(store.profile("p1", snapshot_id=first["snapshot_id"])["profile"], self.person)
        self.write("people", [self.person, {**self.person, "record_id": "p2"}])
        self.assertEqual(import_snapshot(self.root, self.database)["changes"]["profiles"]["added"], 1)

    def test_reordering_evidence_preserves_duplicate_multiplicity(self):
        second = {**self.observation, "role": "Advisor"}
        self.write("evidence", [self.observation, second])
        import_snapshot(self.root, self.database)
        self.write("evidence", [second, self.observation])
        report = import_snapshot(self.root, self.database)
        self.assertEqual(report["changes"]["observations"]["changed"], 0)
        self.write("evidence", [self.observation])
        report = import_snapshot(self.root, self.database)
        self.assertEqual(report["changes"]["observations"]["changed"], 1)


if __name__ == "__main__":
    unittest.main()
