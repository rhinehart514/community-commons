"""Bounded paths through documented shared entities, never inferred friendships."""
from collections import defaultdict, deque
import json
from urllib.parse import urlsplit


def paths(store, source, target, max_hops=4, max_visited=5000):
    if not 1 <= max_hops <= 4 or not 1 <= max_visited <= 10000:
        raise ValueError('Paths support 1–4 hops and at most 10000 visited nodes')
    store.profile(source)
    store.profile(target)
    if source == target:
        return {'path': [], 'found': True, 'truncated': False, 'meaning': 'Same source record'}
    graph = defaultdict(dict)
    # Only resolved profiles and explicit object URLs become graph nodes.
    for row in store.connection.execute('SELECT id,profile_id,payload FROM relationships WHERE profile_id IS NOT NULL ORDER BY id'):
        record = json.loads(row['payload'])
        url = record.get('object_url') or ''
        if urlsplit(url).scheme not in ('https', 'http'):
            continue
        a, b = 'profile:' + row['profile_id'], 'entity:' + url
        edge = {'profile_id': row['profile_id'], 'entity_url': url, 'entity_name': record.get('object_name'), 'relation': record.get('relation'), 'source_url': record.get('source_url'), 'evidence_date': record.get('evidence_date'), 'evidence': record.get('evidence'), 'relationship_id': row['id']}
        graph[a].setdefault(b, edge)
        graph[b].setdefault(a, edge)
    begin, end = 'profile:' + source, 'profile:' + target
    queue = deque([(begin, [])])
    seen = {begin}
    while queue:
        node, trail = queue.popleft()
        if len(trail) >= max_hops:
            continue
        for neighbor, evidence in graph[node].items():
            if neighbor in seen:
                continue
            step = {'from': node, 'to': neighbor, 'evidence': evidence}
            if neighbor == end:
                return {'path': trail + [step], 'found': True, 'truncated': False, 'meaning': 'Shared documented entities; not proof of acquaintance or an available introduction.'}
            if len(seen) >= max_visited:
                return {'path': [], 'found': False, 'truncated': True, 'meaning': 'Search budget exhausted; no absence conclusion.'}
            seen.add(neighbor)
            queue.append((neighbor, trail + [step]))
    return {'path': [], 'found': False, 'truncated': False, 'meaning': 'No path in the imported resolved evidence within this hop limit.'}
