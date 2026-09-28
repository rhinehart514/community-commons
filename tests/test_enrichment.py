import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from bp_commons.enrichment import Enrichment, endpoint, extract


def author(latest='I2', later=2025):
    return {'id': 'https://openalex.org/A1', 'display_name': 'Fictional Researcher',
            'affiliations': [{'institution': {'id': 'https://openalex.org/I1', 'display_name': 'Origin'}, 'years': [2018, 2020]},
                             {'institution': {'id': 'https://openalex.org/I2', 'display_name': 'Destination'}, 'years': [later]}],
            'last_known_institutions': [{'id': 'https://openalex.org/' + latest, 'display_name': 'Latest'}],
            'topics': [{'display_name': 'Robotics', 'id': 'T1', 'count': 3}]}


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Enrichment(Path(self.temp.name) / 'enrichment.sqlite')
        self.addCleanup(self.store.db.close)

    def test_preserved_claims_refresh_and_transitions(self):
        self.assertEqual(self.store.enqueue('openalex-author', 'A1', 'p1'), 1)
        self.assertEqual(self.store.enqueue('openalex-author', 'A1', 'p1'), 0)
        raw = json.dumps(author()).encode()
        result = self.store.step(lambda *_: raw, now=1000)
        self.assertEqual(result['status'], 'fetched')
        saved = self.store.db.execute('SELECT * FROM responses').fetchone()
        self.assertEqual(saved['raw'], raw)
        self.assertEqual(saved['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(len(self.store.transitions(['I1'])), 1)
        self.assertEqual(self.store.transitions(['I1', 'I2']), [])
        self.assertEqual(self.store.claims('robotics')['total'], 1)
        # Institution followups are deduplicated; successful jobs are not fetched until stale.
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 2)
        self.store.step(lambda *_: json.dumps({'id': 'https://openalex.org/I2', 'geo': {'city': 'Elsewhere'}}).encode(), now=1001)
        self.assertIsNone(self.store.step(lambda *_: self.fail('fresh job fetched'), now=1002))
        self.store.step(lambda *_: json.dumps(author(latest='I1')).encode(), now=1000 + 31*86400)
        self.assertEqual(self.store.transitions(['I1']), [])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM responses').fetchone()[0], 3)
        self.assertEqual(self.store.claims('robotics')['total'], 1)

    def test_concurrent_claim_and_dead_worker_recovery(self):
        self.store.enqueue('openalex-author', 'A1')
        self.assertIsNotNone(self.store.claim_job(100))
        with Enrichment(Path(self.temp.name) / 'enrichment.sqlite') as other:
            self.assertIsNone(other.claim_job(101))
            self.assertIsNotNone(other.claim_job(221))

    def test_rate_limit_and_invalid_response_preserved(self):
        self.store.enqueue('openalex-author', 'A1')
        def limited(*_):
            raise HTTPError('https://example.org?api_key=secret', 429, 'secret', {'Retry-After': '600'}, None)
        result = self.store.step(limited, now=1000)
        self.assertTrue(result['stop'])
        row = self.store.db.execute('SELECT * FROM jobs').fetchone()
        self.assertGreaterEqual(row['due'], 1600)
        self.assertNotIn('secret', row['error'])
        self.assertIsNone(self.store.step(limited, now=1001))
        result = self.store.step(lambda *_: b'{invalid', now=1600)
        self.assertEqual(result['error'], 'JSONDecodeError')
        self.assertEqual(self.store.stats()['responses'], 1)
        self.assertEqual(self.store.stats()['claims'], 0)

    def test_transition_needs_strict_chronology_and_no_origin_latest(self):
        for latest, year in [('I2', 2020), ('I1', 2025)]:
            claims, _ = extract('openalex-author', 'A1', author(latest, year))
            self.assertTrue(claims)
            self.store.enqueue('openalex-author', 'A1')
            self.store.db.execute('UPDATE jobs SET due=0')
            self.store.db.commit()
            self.store.step(lambda *_: json.dumps(author(latest, year)).encode())
            self.assertEqual(self.store.transitions(['I1']), [])

    def test_github_signals_and_fixed_endpoints(self):
        claims, _ = extract('github-repos', 'demo', [{'owner': {'login': 'demo'}, 'fork': False, 'language': 'Python', 'html_url': 'https://github.com/demo/test'}, {'owner': {'login': 'demo'}, 'fork': True}])
        self.assertEqual(len(claims), 2)
        self.assertEqual(claims[1][0], 'public_repository')
        self.assertEqual(claims[1][1]['language'], 'Python')
        claims, followups = extract('openalex-author', 'A1', {**author(), 'last_known_institutions': None})
        self.assertEqual(followups, [])
        with self.assertRaises(ValueError):
            endpoint('github-profile', '../private')
        with self.assertRaises(ValueError):
            extract('openalex-author', 'A2', author())
        with self.assertRaises(ValueError):
            self.store.run(max_requests=0)


if __name__ == '__main__':
    unittest.main()
