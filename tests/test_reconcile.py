import copy
import tempfile
import unittest
from bp_commons.datasets import validate_dataset
from bp_commons.reconcile import reconcile
from bp_commons.reviews import Reconciliation


def record(rid, name='Alex Example', **kwargs):
    return {'record_id': rid, 'name': name, 'source_ref': 'https://example.org/' + rid, **kwargs}


def dataset(did, records, complete=True, kind='person'):
    return {'dataset_id': did, 'entity_type': kind, 'source': 'https://example.org',
            'observed_at': '2026-09-26', 'coverage': {'complete': complete, 'scope': 'Test roster'}, 'records': records}


class ReconciliationTests(unittest.TestCase):
    def compare(self, a, b, **kwargs):
        return reconcile(dataset('a', a), dataset('b', b, **kwargs))['results']

    def test_unique_identifiers_match_and_names_do_not(self):
        ids = {'orcid': ['https://orcid.org/0000-0001-0000-0001']}
        r = self.compare([record('a', identifiers=ids)], [record('b', 'A. Example', identifiers={'orcid': ['0000-0001-0000-0001']})])
        self.assertEqual(r[0]['status'], 'matched')
        self.assertEqual(self.compare([record('a')], [record('b')])[0]['status'], 'possible_match')

    def test_shared_identifiers_and_conflicts_require_review(self):
        ids = {'github': ['alexexample']}
        left = [record('a', identifiers=ids)]
        self.assertEqual(self.compare(left, [record('b', identifiers=ids), record('c', identifiers=ids)])[0]['status'], 'possible_match')
        left[0]['identifiers']['orcid'] = ['1111']
        right = [record('b', identifiers={'github': ['alexexample'], 'orcid': ['2222']})]
        self.assertEqual(self.compare(left, right)[0]['status'], 'possible_match')

    def test_coverage_and_sparse_records(self):
        left = [record('a', 'Zara Completelydifferent')]
        right = [record('b', 'Robert Unrelated')]
        self.assertEqual(self.compare(left, right)[0]['status'], 'unmatched')
        self.assertEqual(self.compare(left, right, complete=False)[0]['status'], 'incomplete')
        self.assertEqual(self.compare([record('a', 'Z')], right)[0]['status'], 'incomplete')

    def test_organization_ror_and_alias(self):
        a = dataset('a', [record('a', 'Example Institute', identifiers={'ror': ['https://ror.org/01234abcd']})], kind='organization')
        b = dataset('b', [record('b', 'EI', identifiers={'ror': ['01234abcd']})], kind='organization')
        self.assertEqual(reconcile(a,b)['results'][0]['status'], 'matched')
        b['records'][0]['identifiers'] = {}
        b['records'][0]['aliases'] = ['Example Institute']
        self.assertEqual(reconcile(a,b)['results'][0]['status'], 'possible_match')

    def test_no_cross_entity_matching_or_duplicate_ids(self):
        a = dataset('a', [record('a')])
        with self.assertRaises(ValueError):
            reconcile(a, dataset('b', [record('b')], kind='organization'))
        with self.assertRaises(ValueError):
            validate_dataset(dataset('b', [record('b'),record('b')]))

    def test_review_persistence_reversal_and_staleness(self):
        a, b = dataset('a', [record('a')]), dataset('b', [record('b')])
        original = copy.deepcopy(a)
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/review.sqlite'
            with Reconciliation(path) as store:
                run = store.compare(a,b)
                store.review(run['run_id'], 'a','b','same','Tester','Confirmed with source owner')
            with Reconciliation(path) as store:
                matched = store.compare(a,b)
                self.assertEqual(matched['results'][0]['status'], 'matched')
                self.assertEqual(store.compare(b,a)['results'][0]['status'], 'matched')
                changed = copy.deepcopy(b)
                changed['records'][0]['name'] = 'Different Identity'
                self.assertEqual(store.compare(a,changed)['results'][0]['status'], 'possible_match')
                store.review(matched['run_id'], 'a','b','different','Tester','Correction: distinct people')
                self.assertEqual(store.compare(a,b)['results'][0]['status'], 'unmatched')
                self.assertEqual(store.run(run['run_id'])['results'][0]['status'], 'possible_match')
            self.assertEqual(a, original)

    def test_email_never_auto_matches_and_no_transitive_merges(self):
        ids = {'email': ['shared@example.org']}
        self.assertEqual(self.compare([record('a', identifiers=ids)], [record('b', identifiers=ids)])[0]['status'], 'possible_match')
        ids = {'github': ['same-account']}
        results = self.compare([record('a', identifiers=ids),record('c', identifiers=ids)], [record('b', identifiers=ids)])
        self.assertTrue(all(r['status'] == 'possible_match' for r in results))

    def test_exact_identifier_beats_name_only_distractor(self):
        r = self.compare([record('a', 'Example University', identifiers={'ror':['example-ror']})],
                         [record('b','Example University',identifiers={'ror':['example-ror']}),record('c','Example University')])
        self.assertEqual(r[0]['status'], 'matched')

    def test_manifest_count_validation_and_benchmark(self):
        from bp_commons.evaluate import evaluate
        a = dataset('a',[record('a',identifiers={'github':['test']})])
        b = dataset('b',[record('b',identifiers={'github':['test']})])
        report = evaluate(a,b,{'a':['b']})
        self.assertEqual(report['false_automatic_matches'],0)
        self.assertEqual(report['candidate_recall'],1)
        with self.assertRaises(ValueError):
            evaluate(a,b,{})
        a['coverage']['expected_records'] = 2
        with self.assertRaises(ValueError):
            validate_dataset(a)

    def test_different_profile_services_do_not_conflict_with_shared_registry_id(self):
        a = record('a', identifiers={'orcid':['same-orcid'], 'profile_url':['https://example.org/faculty/a']})
        b = record('b', identifiers={'orcid':['same-orcid'], 'profile_url':['https://other.example.org/author/b']})
        result = self.compare([a],[b])[0]
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['candidates'][0]['conflicting_identifier_namespaces'], [])
