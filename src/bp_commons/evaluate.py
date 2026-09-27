"""Small labeled benchmark runner. Report retrieval separately from automatic decisions."""
from .reconcile import reconcile


def evaluate(left, right, labels):
    report = reconcile(left, right)
    known_left = {r['record_id'] for r in left['records']}
    known_right = {r['record_id'] for r in right['records']}
    if not isinstance(labels, dict) or set(labels) != known_left:
        raise ValueError('Benchmark labels must cover every left record')
    for targets in labels.values():
        if not isinstance(targets, list) or any(t not in known_right for t in targets):
            raise ValueError('Benchmark target IDs must exist on the right')
    gold = {(a,b) for a, targets in labels.items() for b in targets}
    automatic, retrieved = set(), set()
    for row in report['results']:
        for match in row['candidates']:
            if match['status'] != 'rejected':
                retrieved.add((row['left_id'],match['right_id']))
            if row['status'] == 'matched' and match['status'] == 'matched':
                automatic.add((row['left_id'],match['right_id']))
    return {'model_version': report['model_version'], 'labeled_left_records': len(labels),
            'true_pairs': len(gold), 'automatic_matches': len(automatic),
            'false_automatic_matches': len(automatic-gold),
            'automatic_precision': len(automatic & gold)/len(automatic) if automatic else None,
            'automatic_recall': len(automatic & gold)/len(gold) if gold else None,
            'candidate_recall': len(retrieved & gold)/len(gold) if gold else None,
            'missed_pairs': sorted(gold-retrieved),
            'caveat': 'Results apply only to these labeled records; synthetic examples do not establish real-world accuracy.'}
