import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from bp_commons.works import Works, source_identity
from bp_commons.importer import import_snapshot


class WorkEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lane = self.root / 'lane'
        self.lane.mkdir()
        raw = b'Example Builder designed the fictional prototype.'
        (self.lane / 'raw.html').write_bytes(raw)
        self.work = dict(id='fictional-work', title='Fictional prototype', kind='capstone',
                         source_url='https://example.org/project', observed_at='2026-01-01T00:00:00Z',
                         work_date='2025', organization='Example University', regional_evidence='Fictional regional project',
                         geography_scope='Fictional region', description='A fictional test fixture',
                         raw_path='raw.html', raw_sha256=hashlib.sha256(raw).hexdigest())
        self.credit = dict(work_id='fictional-work', name='Example Builder', role='designer',
                           person_url='', identifiers={}, evidence=raw.decode(), source_url='https://example.org/project')
        self.write()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self):
        (self.lane / 'works.jsonl').write_text(json.dumps(self.work) + '\n')
        (self.lane / 'contributions.jsonl').write_text(json.dumps(self.credit) + '\n')
        (self.lane / 'manifest.json').write_text(json.dumps(dict(lane='builders_business', complete=False,
            scope='One fictional project', works=1, contributions=1)))

    def test_idempotent_ingest_retains_raw_bytes(self):
        with Works(self.root / 'works.sqlite') as archive:
            self.assertEqual(archive.ingest(self.lane)['status'], 'imported')
            self.assertEqual(archive.ingest(self.lane)['status'], 'unchanged')
            self.assertEqual(archive.stats(), dict(runs=1, works=1, contributions=1, artifacts=1))
            self.assertEqual(archive.db.execute('SELECT bytes FROM artifacts').fetchone()[0], (self.lane / 'raw.html').read_bytes())

    def test_bad_hash_and_unknown_work_fail_without_partial_import(self):
        with Works(self.root / 'works.sqlite') as archive:
            self.work['raw_sha256'] = '0' * 64
            self.write()
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                archive.ingest(self.lane)
            self.assertEqual(archive.stats()['runs'], 0)
            self.work['raw_sha256'] = hashlib.sha256((self.lane / 'raw.html').read_bytes()).hexdigest()
            self.credit['work_id'] = 'missing'
            self.write()
            with self.assertRaisesRegex(ValueError, 'unknown work'):
                archive.ingest(self.lane)
            self.assertEqual(archive.stats()['runs'], 0)

    def test_unmatched_source_response_is_preserved(self):
        raw = self.lane / 'raw'
        raw.mkdir()
        (raw / 'blocked.html').write_text('Source returned a challenge, not records')
        with Works(self.root / 'works.sqlite') as archive:
            archive.ingest(self.lane)
            self.assertEqual(archive.stats()['artifacts'], 2)
            self.assertIsNotNone(archive.db.execute("SELECT sha256 FROM run_artifacts WHERE path='raw/blocked.html'").fetchone())

    def test_artifact_cannot_escape_lane(self):
        self.work['raw_path'] = '../outside.html'
        (self.root / 'outside.html').write_bytes((self.lane / 'raw.html').read_bytes())
        self.write()
        with Works(self.root / 'works.sqlite') as archive:
            with self.assertRaisesRegex(ValueError, 'outside batch'):
                archive.ingest(self.lane)

    def test_same_name_on_different_sources_is_not_merged(self):
        base = self.root / 'base'
        base.mkdir()
        original = dict(record_id='original', name='Original Person', source_url='https://example.org/original', regional_evidence='Original evidence')
        (base / 'people.jsonl').write_text(json.dumps(original) + '\n')
        (base / 'evidence.jsonl').write_text('')
        (base / 'connections.jsonl').write_text('')
        database = self.root / 'commons.sqlite'
        import_snapshot(base, database)
        with Works(self.root / 'works.sqlite') as archive:
            archive.ingest(self.lane)
            self.work['id'] = 'other-work'
            self.work['source_url'] = 'https://example.org/different-project'
            self.credit['work_id'] = 'other-work'
            self.credit['source_url'] = self.work['source_url']
            self.write()
            archive.ingest(self.lane)
            result = archive.export(database, self.root / 'export')
            self.assertEqual(result['work_profiles'], 2)

    def test_count_mismatch_rejects_batch(self):
        manifest = json.loads((self.lane / 'manifest.json').read_text())
        manifest['contributions'] = 7
        (self.lane / 'manifest.json').write_text(json.dumps(manifest))
        with Works(self.root / 'works.sqlite') as archive:
            with self.assertRaisesRegex(ValueError, 'counts'):
                archive.ingest(self.lane)

    def test_observation_keeps_only_its_own_affiliation_and_role(self):
        self.credit['person_url'] = 'https://example.org/person'
        self.write()
        base = self.root / 'base'
        base.mkdir()
        for name in ('people', 'evidence', 'connections'):
            (base / (name + '.jsonl')).write_text('')
        (base / 'people.jsonl').write_text(json.dumps(dict(record_id='original', name='Example Person',
            source_url='https://example.org/original', regional_evidence='Original evidence')) + '\n')
        database = self.root / 'commons.sqlite'
        import_snapshot(base, database)
        with Works(self.root / 'works.sqlite') as archive:
            archive.ingest(self.lane)
            self.work.update(id='second-work', organization='Another University',
                             regional_evidence='Another documented affiliation')
            self.credit.update(work_id='second-work', role='adviser')
            self.write()
            archive.ingest(self.lane)
            archive.export(database, self.root / 'export')
        observations = [json.loads(line) for line in (self.root / 'export' / 'evidence.jsonl').read_text().splitlines()]
        self.assertEqual(len(observations), 2)
        self.assertEqual(observations[0]['organization'], 'Example University')
        self.assertEqual(observations[0]['role'], 'designer')
        self.assertEqual(observations[1]['organization'], 'Another University')
        self.assertEqual(observations[1]['regional_evidence'], 'Another documented affiliation')

    def test_bulk_file_does_not_merge_same_name_across_works(self):
        other = {**self.credit, 'work_id': 'another-project'}
        self.assertNotEqual(source_identity('funded_inventions', self.credit), source_identity('funded_inventions', other))
        self.credit['identifiers'] = {'nih_profile_id': '12345'}
        other['identifiers'] = {'nih_profile_id': '12345'}
        self.assertEqual(source_identity('funded_inventions', self.credit), source_identity('funded_inventions', other))

    def test_refresh_preserves_base_and_does_not_duplicate_work_profiles(self):
        self.credit['person_url'] = 'https://example.org/person'
        self.write()
        base = self.root / 'base'
        base.mkdir()
        profile = dict(record_id='original', name='Original Person', profile_url=self.credit['person_url'], source_url='https://example.org/original', regional_evidence='Original evidence')
        (base / 'people.jsonl').write_text(json.dumps(profile) + '\n')
        (base / 'evidence.jsonl').write_text(json.dumps(profile) + '\n')
        (base / 'connections.jsonl').write_text('')
        database = self.root / 'commons.sqlite'
        import_snapshot(base, database)
        with Works(self.root / 'works.sqlite') as archive:
            archive.ingest(self.lane)
            first = archive.export(database, self.root / 'export1')
            self.assertEqual(first['people'], 1)
            self.assertEqual(first['new_work_profiles'], 0)
            self.assertEqual(first['enriched_existing_profiles'], 1)
            import_snapshot(self.root / 'export1', database)
            import sqlite3
            with sqlite3.connect(database) as db:
                self.assertIsNotNone(db.execute('SELECT profile_id FROM relationships').fetchone()[0])
            second = archive.export(database, self.root / 'export2')
            self.assertEqual(second['people'], 1)
            rows = [json.loads(x) for x in (self.root / 'export2' / 'people.jsonl').read_text().splitlines()]
            self.assertEqual({k: rows[0][k] for k in profile}, profile)
            self.assertEqual(rows[0]['discovered_work'][0]['contribution']['role'], 'designer')
            self.assertEqual(second['evidence'], first['evidence'])
            self.assertEqual(second['connections'], first['connections'])


if __name__ == '__main__':
    unittest.main()
