import json
from pathlib import Path
import tempfile
import unittest
from bp_commons import Commons, Enrichment, Workspaces, discover, paths
from bp_commons.importer import import_snapshot
from bp_commons.web import create_app


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / 'commons.sqlite'
        people = [{'record_id': f'p{i}', 'name': f'Fictional Person {i}', 'collector': 'fixture', 'source_url': f'https://example.org/people/{i}', 'profile_url': f'https://example.org/people/{i}', 'regional_evidence': 'Fictional robotics roster'} for i in range(35)]
        edges = []
        for person, entity in [(0,'a'), (1,'a'), (1,'b'), (2,'b')]:
            edges.append({'subject_name': people[person]['name'], 'subject_url': people[person]['source_url'], 'object_url': f'https://example.org/organizations/{entity}', 'object_name': entity, 'relation': 'affiliated_with', 'source_url': people[person]['source_url'], 'evidence': 'Fictional affiliation', 'evidence_date': '2020'})
        for name, rows in [('people', people), ('evidence', people), ('connections', edges)]:
            (self.root / (name + '.jsonl')).write_text(''.join(json.dumps(r) + '\n' for r in rows))
        import_snapshot(self.root, self.database)
        self.work = Workspaces(self.root / 'workspaces.sqlite')
        self.evidence = Enrichment(self.root / 'enrichment.sqlite')
        self.store = Commons(self.database)
        self.addCleanup(self.work.db.close)
        self.addCleanup(self.evidence.db.close)
        self.addCleanup(self.store.close)
        self.annotation = {'known': 'unknown', 'contacted': 'yes', 'participated': 'no', 'selection': 'shortlisted', 'reviewer': 'Test consumer', 'reason': 'Fictional contact record'}

    def test_independent_scoped_markers_audit_and_filters(self):
        second = self.work.create('Another consumer')['id']
        before = self.store.profile('p0')
        self.work.save('personal', 'p0', self.annotation)
        self.assertEqual(self.work.records(second), {})
        self.assertEqual(self.work.records('personal')['p0']['participated'], 'no')
        self.assertEqual(discover(self.store, self.evidence, self.work.records('personal'), mode='new')['total'], 34)
        self.assertEqual(discover(self.store, self.evidence, self.work.records(second), mode='new')['total'], 35)
        shortlist = discover(self.store, self.evidence, self.work.records('personal'), mode='shortlisted')
        self.assertEqual([p['id'] for p in shortlist['profiles']], ['p0'])
        self.work.save('personal', 'p0', {**self.annotation, 'contacted': 'unknown', 'selection': 'dismissed'})
        self.assertEqual(len(self.work.history('personal', 'p0')), 2)
        self.assertEqual(discover(self.store, self.evidence, self.work.records('personal'), mode='new')['total'], 34)
        self.assertEqual(self.store.profile('p0'), before)
        with self.assertRaises(ValueError):
            self.work.save('personal', 'p0', {**self.annotation, 'known': True})
        with self.assertRaises(KeyError):
            self.work.records('missing')

    def test_filtering_before_pagination_and_enrichment_search(self):
        for i in range(32):
            self.work.save('personal', f'p{i}', self.annotation)
        results = discover(self.store, self.evidence, self.work.records('personal'), mode='new')
        self.assertEqual(results['total'], 3)
        self.assertEqual(len(results['profiles']), 3)
        self.evidence.enqueue('github-profile', 'fixture', 'p34')
        self.evidence.step(lambda *_: b'{"login":"fixture","id":123,"bio":"Quantum sensors"}')
        results = discover(self.store, self.evidence, {}, query='quantum sensors')
        self.assertEqual(results['total'], 1)
        self.assertEqual(results['profiles'][0]['id'], 'p34')
        self.assertEqual(results['profiles'][0]['enriched_matches'][0]['source'], 'https://api.github.com/users/fixture')
        self.assertEqual(discover(self.store, self.evidence, {}, query='quantum', collector='other')['total'], 0)

    def test_bounded_sourced_paths(self):
        result = paths(self.store, 'p0', 'p2')
        self.assertTrue(result['found'])
        self.assertEqual(len(result['path']), 4)
        self.assertTrue(all(s['evidence']['source_url'] for s in result['path']))
        self.assertFalse(paths(self.store, 'p0', 'p2', max_hops=2)['found'])
        self.assertFalse(paths(self.store, 'p0', 'p30')['found'])
        self.assertTrue(paths(self.store, 'p0', 'p2', max_visited=1)['truncated'])
        self.assertTrue(paths(self.store, 'p0', 'p0')['found'])

    def test_http_consumer_contract(self):
        client = create_app(self.database, self.root / 'reconciliation.sqlite').test_client()
        headers = {'X-BP-Commons': 'workbench'}
        workspace = client.post('/api/workspaces', json={'name': 'Client app'}, headers=headers).json['id']
        response = client.post(f'/api/workspaces/{workspace}/profiles/p0', json=self.annotation, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get(f'/api/discover?workspace={workspace}&mode=new').json['total'], 34)
        self.assertEqual(client.get('/api/discover?workspace=personal&mode=new').json['total'], 35)
        exported = client.get(f'/api/workspaces/{workspace}/export')
        self.assertEqual(exported.json['records'][0]['profile_id'], 'p0')
        self.assertEqual(client.get('/api/paths?source=p0&target=p2').json['found'], True)
        self.assertEqual(client.post(f'/api/workspaces/{workspace}/profiles/missing', json=self.annotation, headers=headers).status_code, 404)
        self.assertEqual(client.get('/api/discover?mode=invalid').status_code, 400)
