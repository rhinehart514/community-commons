import json
from pathlib import Path
import tempfile
import unittest
from bp_commons.importer import import_snapshot
from bp_commons.web import create_app


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        people = [{'record_id': f'p{i}', 'name': f'Example Person {i}', 'collector': 'test', 'source_url': 'https://example.org', 'role': 'Robotics researcher', 'regional_evidence': 'Fictional test roster'} for i in range(35)]
        for filename, rows in [('people', people), ('evidence', people), ('connections', [])]:
            (self.root / (filename + '.jsonl')).write_text(''.join(json.dumps(r) + '\n' for r in rows))
        database = self.root / 'commons.sqlite'
        import_snapshot(self.root, database)
        self.client = create_app(database, self.root / 'audit.sqlite').test_client()
        self.headers = {'X-BP-Commons': 'workbench', 'Origin': 'http://localhost'}
        examples = Path(__file__).resolve().parents[1] / 'examples'
        self.left = json.loads((examples / 'people-left.json').read_text())
        self.right = json.loads((examples / 'people-right.json').read_text())

    def post(self, path, data):
        return self.client.post('/api/' + path, json=data, headers=self.headers)

    def test_browse_search_pagination_and_evidence(self):
        data = self.client.get('/api/profiles?q=robotics').json
        self.assertEqual(data['total'], 35)
        self.assertEqual(len(data['profiles']), 30)
        second = self.client.get('/api/profiles?q=robotics&offset=30').json
        self.assertEqual(len(second['profiles']), 5)
        self.assertFalse({r['id'] for r in data['profiles']} & {r['id'] for r in second['profiles']})
        self.assertEqual(self.client.get('/api/profiles?collector=missing').json['total'], 0)
        self.assertEqual(self.client.get('/api/profiles?q=%22%20*').json['total'], 0)
        self.assertEqual(self.client.get('/api/profile/p1').json['observations'][0]['name'], 'Example Person 1')
        self.assertEqual(self.client.get('/api/profile/missing').status_code, 404)
        self.assertEqual(self.client.get('/api/profiles?offset=-1').status_code, 400)
        self.assertEqual(self.client.get('/api/stats').json['profiles'], 35)
        snapshot = self.client.get('/api/history').json[0]['snapshot_id']
        self.assertTrue(self.client.get('/api/changes/' + snapshot).json)

    def test_review_is_preserved_and_rerun_is_explicit(self):
        result = self.post('compare', {'left': self.left, 'right': self.right})
        self.assertEqual(result.status_code, 200)
        run_id = result.json['run_id']
        before = self.client.get('/api/runs/' + run_id).json
        pair = self.client.get(f'/api/runs/{run_id}/pair?left=p2&right=r2').json
        self.assertEqual(pair['left']['record_id'], 'p2')
        decision = self.post(f'runs/{run_id}/review', {'left_id': 'p2', 'right_id': 'r2', 'verdict': 'same', 'reviewer': 'Fixture reviewer', 'reason': 'Fictional fixture alias confirmed'})
        self.assertEqual(decision.status_code, 200)
        self.assertEqual(self.client.get('/api/runs/' + run_id).json, before)
        new_id = self.post(f'runs/{run_id}/rerun', {}).json['run_id']
        after = self.client.get('/api/runs/' + new_id).json
        self.assertNotEqual(run_id, new_id)
        self.assertGreater(after['summary']['matched'], before['summary']['matched'])
        catalog = self.client.get('/api/catalog').json
        self.assertEqual(len(catalog['datasets']), 2)
        self.assertEqual(len(catalog['runs']), 2)
        saved = self.post('compare', {'left': catalog['datasets'][0]['digest'], 'right': catalog['datasets'][1]['digest']})
        self.assertEqual(saved.status_code, 200)
        self.assertIn('attachment', self.client.get(f'/api/runs/{run_id}?download=1').headers['Content-Disposition'])

    def test_validation_and_cross_origin_protection(self):
        self.assertEqual(self.client.post('/api/compare', json={}).status_code, 403)
        self.assertEqual(self.client.post('/api/compare', json={}, headers={'X-BP-Commons': 'workbench', 'Origin': 'https://foreign.example'}).status_code, 403)
        self.assertEqual(self.client.get('/api/stats', headers={'Host': 'foreign.example'}).status_code, 400)
        self.assertEqual(self.post('compare', {'left': {}, 'right': {}}).status_code, 400)
        self.assertEqual(self.post('compare', []).status_code, 400)
        self.assertEqual(self.post('compare', {'left': 'missing', 'right': 'missing'}).status_code, 404)
        self.assertEqual(self.client.get('/api/catalog').json['runs'], [])
        with self.client.get("/") as response:
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        with self.client.get("/static/app.js") as response:
            self.assertEqual(response.status_code, 200)


if __name__ == '__main__':
    unittest.main()
