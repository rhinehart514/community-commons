"""Pairwise reconciliation. Similar names are candidates, never proof of identity."""
from collections import Counter, defaultdict
from rapidfuzz import fuzz, process
from .datasets import digest, identifiers, name_key, validate_dataset

STRONG = {'profile_url', 'orcid', 'ror', 'github', 'github_id'}
MODEL_VERSION = 'exact-identifiers-and-rapidfuzz-v1'


def pair_key(left_dataset, left_id, right_dataset, right_id):
    return tuple(sorted(((left_dataset, left_id), (right_dataset, right_id))))


def reconcile(left, right, reviews=()):
    validate_dataset(left)
    validate_dataset(right)
    if left['entity_type'] != right['entity_type']:
        raise ValueError('Datasets must have the same entity_type')
    if left['dataset_id'] == right['dataset_id']:
        raise ValueError('Use distinct dataset IDs for source-to-source reconciliation')
    right_rows = {r['record_id']: r for r in right['records']}
    index, names = defaultdict(set), {}
    right_counts, left_counts = Counter(), Counter()
    for row in right['records']:
        for identifier in identifiers(row):
            index[identifier].add(row['record_id'])
            right_counts[identifier] += 1
        for number, name in enumerate([row['name'], *row.get('aliases', [])]):
            if name_key(name):
                names[(row['record_id'], number)] = name_key(name)
    for row in left['records']:
        left_counts.update(identifiers(row))
    decisions = {}
    for review in reviews:
        decisions[pair_key(*review['left'], *review['right'])] = review
    by_left = defaultdict(set)
    for review in decisions.values():
        for a, b in ((review['left'], review['right']), (review['right'], review['left'])):
            if a[0] == left['dataset_id'] and b[0] == right['dataset_id'] and b[1] in right_rows:
                by_left[a[1]].add(b[1])
    results = []
    complete = left['coverage']['complete'] and right['coverage']['complete']
    for row in left['records']:
        row_ids = identifiers(row)
        candidates = set(by_left[row['record_id']])
        for identifier in row_ids:
            candidates.update(index.get(identifier, ()))
        for name in [row['name'], *row.get('aliases', [])]:
            for _, _, key in process.extract(name_key(name), names, scorer=fuzz.ratio, score_cutoff=75, limit=10):
                candidates.add(key[0])
        matches = []
        for rid in sorted(candidates):
            other = right_rows[rid]
            other_ids = identifiers(other)
            shared = sorted(row_ids & other_ids)
            conflicts = []
            for ns in STRONG - {'profile_url'}:
                a, b = {v for n, v in row_ids if n == ns}, {v for n, v in other_ids if n == ns}
                if a and b and not a & b:
                    conflicts.append(ns)
            reasons = [{'type': 'identifier_agreement', 'namespace': ns, 'value': value,
                        'unique_in_both_datasets': left_counts[(ns,value)] == right_counts[(ns,value)] == 1}
                       for ns, value in shared]
            score = max(fuzz.ratio(name_key(a), name_key(b)) for a in [row['name'], *row.get('aliases', [])]
                        for b in [other['name'], *other.get('aliases', [])])
            reasons.append({'type': 'name_similarity', 'score': round(score, 2), 'meaning': 'string similarity, not identity probability'})
            overlap = sorted({name_key(a) for a in row.get('affiliations', [])} & {name_key(a) for a in other.get('affiliations', [])})
            if overlap:
                reasons.append({'type': 'affiliation_overlap', 'values': overlap})
            decision = decisions.get(pair_key(left['dataset_id'], row['record_id'], right['dataset_id'], rid))
            decision_current = False
            if decision:
                expected = {tuple(decision['left']): decision['left_fingerprint'], tuple(decision['right']): decision['right_fingerprint']}
                decision_current = expected[(left['dataset_id'], row['record_id'])] == digest(row) and expected[(right['dataset_id'], rid)] == digest(other)
            strong = any(ns in STRONG and left_counts[(ns,v)] == right_counts[(ns,v)] == 1 for ns,v in shared)
            status = 'matched' if strong and not conflicts else 'possible_match'
            if decision and not decision_current:
                status = 'possible_match'
                reasons.append({'type': 'stale_review', 'review_id': decision['review_id']})
            elif decision_current:
                status = {'same': 'matched', 'different': 'rejected', 'unsure': 'possible_match'}[decision['verdict']]
                reasons.append({'type': 'review', 'review_id': decision['review_id'], 'verdict': decision['verdict'],
                                'reviewer': decision['reviewer'], 'reason': decision['reason']})
            matches.append({'right_id': rid, 'name': other['name'], 'source_ref': other['source_ref'],
                            'status': status, 'evidence': reasons, 'conflicting_identifier_namespaces': sorted(conflicts),
                            'right_fingerprint': digest(other)})
        matches.sort(key=lambda m: ({'matched': 0, 'possible_match': 1, 'rejected': 2}[m['status']],
                     -next(e['score'] for e in m['evidence'] if e['type'] == 'name_similarity'), m['right_id']))
        accepted = [m for m in matches if m['status'] == 'matched']
        possible = [m for m in matches if m['status'] == 'possible_match']
        status = 'matched' if len(accepted) == 1 else 'possible_match' if accepted or possible else 'unmatched'
        if status == 'unmatched' and (not complete or (not row_ids and len(name_key(row['name']).split()) < 2)):
            status = 'incomplete'
        results.append({'left_id': row['record_id'], 'name': row['name'], 'source_ref': row['source_ref'],
                        'left_fingerprint': digest(row), 'status': status, 'coverage_complete': complete,
                        'candidates': matches, 'reason': {
                            'matched': 'One unambiguous identifier or reviewed match.',
                            'possible_match': 'Candidate identities require review.',
                            'unmatched': 'No accepted or candidate match under this matcher in the supplied datasets.',
                            'incomplete': 'Dataset coverage or identifying fields are insufficient for a negative conclusion.'}[status]})
    # Do not silently assign multiple left records to the same right entity.
    owners = Counter(m['right_id'] for r in results if r['status'] == 'matched' for m in r['candidates'] if m['status'] == 'matched')
    for result in results:
        if result['status'] == 'matched' and any(owners[m['right_id']] > 1 for m in result['candidates'] if m['status'] == 'matched'):
            result['status'] = 'possible_match'
            result['reason'] = 'Multiple left records match this right record; review duplicate identities.'
    return {'model_version': MODEL_VERSION, 'left_dataset': left['dataset_id'], 'right_dataset': right['dataset_id'],
            'coverage': {'left': left['coverage'], 'right': right['coverage']},
            'summary': {s: sum(r['status'] == s for r in results) for s in ('matched','possible_match','unmatched','incomplete')},
            'results': results}
