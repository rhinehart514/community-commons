import json
from pathlib import Path
import tempfile
import unittest
from bp_commons import Commons, Enrichment, Subscriptions, ChangeFeed
from bp_commons.importer import import_snapshot


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.e = Enrichment(self.root / 'enrichment.sqlite')
        self.s = Subscriptions(self.root / 'subscriptions.sqlite')
        self.addCleanup(self.e.db.close)
        self.addCleanup(self.s.db.close)
        self.person = {'record_id': 'p1', 'name': 'Fictional Person', 'collector': 'demo', 'source_url': 'https://example.org/person', 'regional_evidence': 'Robotics researcher at I1', 'external_ids': json.dumps({'github': 'fixture'})}
        self.write_snapshot()
        self.store = Commons(self.root / 'commons.sqlite')
        self.addCleanup(self.store.close)
        self.e.enqueue('github-profile', 'fixture', 'p1')

    def write_snapshot(self):
        for name, rows in [('people', [self.person]), ('evidence', [self.person]), ('connections', [])]:
            (self.root / (name + '.jsonl')).write_text(''.join(json.dumps(r)+'\n' for r in rows))
        import_snapshot(self.root, self.root / 'commons.sqlite')

    def refresh(self, payload):
        with self.e.db:
            self.e.db.execute("UPDATE jobs SET due=0 WHERE provider='github-profile'")
        return self.e.step(lambda *_: json.dumps({'login': 'fixture', 'id': 1, **payload}).encode())

    def test_claim_feed_replay_changes_absence_and_failure(self):
        self.refresh({'bio': 'Robotics', 'location': 'Example City'})
        feed = ChangeFeed(self.e)
        first = feed.read(limit=1)
        self.assertEqual(first, feed.read(limit=1))
        self.assertTrue(first['has_more'])
        remaining = feed.read(after=first['cursor'])
        cursor = remaining['cursor']
        self.refresh({'bio': 'Robotics', 'location': 'Example City'})
        self.assertEqual(feed.read(cursor)['events'], [])
        self.refresh({'bio': 'Sensors'})
        changes = feed.read(cursor)['events']
        self.assertEqual({(e['predicate'],e['kind']) for e in changes}, {('self_reported_bio','changed'),('self_reported_location','absent_from_latest')})
        cursor = changes[-1]['sequence']
        with self.e.db:
            self.e.db.execute('UPDATE jobs SET due=0')
        self.e.step(lambda *_: b'not-json')
        self.assertEqual(feed.read(cursor)['events'], [])

    def test_subscription_idempotency_refresh_and_consumer_scope(self):
        self.refresh({'bio': 'Robotics'})
        sub = self.s.create('consumer-a','Robotics',{'query':'robotics'})['id']
        first = self.s.evaluate('consumer-a',sub,self.store,self.e)
        self.assertEqual(first['emitted'],1)
        self.assertEqual(self.s.evaluate('consumer-a',sub,self.store,self.e)['emitted'],0)
        self.refresh({'bio':'Robotics'})
        self.assertEqual(self.s.evaluate('consumer-a',sub,self.store,self.e)['emitted'],0)
        self.assertEqual(self.s.events('consumer-b')['events'],[])
        with self.assertRaises(KeyError):self.s.get('consumer-b',sub)
        self.assertEqual(self.s.events('consumer-a'),self.s.events('consumer-a'))
        self.s.pause('consumer-a',sub,True)
        self.assertTrue(self.s.evaluate('consumer-a',sub,self.store,self.e)['paused'])

    def test_exit_reentry_and_explicit_private_inputs(self):
        sub = self.s.create('a','New robotics',{'query':'robotics','mode':'new'})['id']
        with self.assertRaises(ValueError):self.s.evaluate('a',sub,self.store,self.e)
        self.s.evaluate('a',sub,self.store,self.e,{})
        self.s.evaluate('a',sub,self.store,self.e,{'p1':{'known':'yes'}})
        self.s.evaluate('a',sub,self.store,self.e,{})
        self.assertEqual([e['kind'] for e in self.s.events('a')['events']],['entered','exited','entered'])

    def test_planning_dry_run_idempotence_and_cooldown(self):
        sub = self.s.create('a','Robotics',{'query':'robotics'})['id']
        plan = self.s.plan('a',sub,self.store,self.e)
        self.assertEqual(len(plan['tasks']),2)
        self.assertEqual(plan['queued'],0)
        self.assertEqual(self.e.stats()['jobs'],{'pending':1})
        self.assertEqual(self.s.plan('a',sub,self.store,self.e,apply=True)['queued'],1)
        with self.e.db:self.e.db.execute('UPDATE jobs SET due=9999999999')
        self.assertEqual(self.s.plan('a',sub,self.store,self.e,apply=True)['queued'],0)
        self.assertIsNone(self.e.claim_job(100))
        self.assertEqual(self.s.plan('a',sub,self.store,self.e)['tasks'],[])

    def test_targeted_worker_does_not_drain_unrelated_queue(self):
        self.e.enqueue('github-profile','unrelated')
        job = self.e.claim_job(100, targets=[('github-profile','unrelated')])
        self.assertEqual(job['subject'],'unrelated')
        self.assertIsNone(self.e.claim_job(100, targets=[]))
        self.assertEqual(self.e.claim_job(100)['subject'],'fixture')

    def test_origin_planning_and_validation(self):
        sub = self.s.create('a','Origin',{'institutions':['I1']})['id']
        self.assertEqual(len(self.s.plan('a',sub,self.store,self.e)['tasks']),2)
        other = self.s.create('a','Other origin',{'institutions':['I10']})['id']
        self.assertEqual(self.s.plan('a',other,self.store,self.e)['tasks'],[])
        with self.assertRaises(ValueError):self.s.create('a','Invalid',{'mystery':1})
        with self.assertRaises(ValueError):ChangeFeed(self.e).read(after=-1)

    def test_http_feed_subscription_and_scope(self):
        from bp_commons.web import create_app
        client = create_app(self.root / 'commons.sqlite', self.root / 'audit.sqlite').test_client()
        headers = {'X-BP-Commons':'workbench'}
        created = client.post('/api/subscriptions', json={'consumer':'a','name':'Robotics','query':{'query':'robotics'}}, headers=headers)
        self.assertEqual(created.status_code,200)
        sid = created.json['id']
        self.assertEqual(client.post(f'/api/subscriptions/{sid}/evaluate', json={'consumer':'a'}, headers=headers).json['emitted'],1)
        self.assertEqual(client.post(f'/api/subscriptions/{sid}/evaluate', json={'consumer':'b'}, headers=headers).status_code,404)
        self.assertEqual(len(client.get('/api/subscription-events?consumer=a').json['events']),1)
        self.assertEqual(client.get('/api/subscription-events?consumer=b').json['events'],[])
        self.assertEqual(client.get('/api/feed?after=-1').status_code,400)

    def test_feed_does_not_lose_inflight_response(self):
        self.refresh({'bio':'Robotics'})
        feed = ChangeFeed(self.e)
        cursor = feed.read()['cursor']
        with self.e.db:
            row = self.e.db.execute('SELECT * FROM responses LIMIT 1').fetchone()
            rid = self.e.db.execute('INSERT INTO responses(job_id,observed,url,sha256,raw,parser_version) VALUES(?,?,?,?,?,?)', (row['job_id'],row['observed']+1,row['url'],'fixture-hash',b'{}','1')).lastrowid
        self.assertEqual(feed.read(cursor)['events'],[])
        with self.e.db:
            self.e.db.execute('INSERT INTO claims VALUES(?,?,?,?)',(rid,'fixture','self_reported_bio',json.dumps('Changed')))
        self.assertTrue(feed.read(cursor)['events'])

    def test_updated_result_and_cursor_pagination(self):
        self.refresh({'bio':'Robotics'})
        sid = self.s.create('a','Robotics',{'query':'robotics'})['id']
        self.s.evaluate('a',sid,self.store,self.e)
        self.refresh({'bio':'Robotics and sensors'})
        self.assertEqual(self.s.evaluate('a',sid,self.store,self.e)['emitted'],1)
        first = self.s.events('a',limit=1)
        self.assertTrue(first['has_more'])
        second = self.s.events('a',after=first['cursor'])
        self.assertEqual(second['events'][0]['kind'],'updated')
        self.assertFalse(second['has_more'])

    def test_planner_can_link_existing_fresh_evidence_without_refetch(self):
        self.refresh({'bio':'Robotics'})
        with self.e.db:
            self.e.db.execute('DELETE FROM links')
        sid = self.s.create('a','Robotics',{'query':'robotics'})['id']
        before = self.e.db.execute("SELECT due FROM jobs WHERE provider='github-profile'").fetchone()[0]
        plan = self.s.plan('a',sid,self.store,self.e,apply=True)
        self.assertTrue(any(t['provider']=='github-profile' for t in plan['tasks']))
        self.assertEqual(self.e.db.execute("SELECT due FROM jobs WHERE provider='github-profile'").fetchone()[0],before)
        self.assertIsNotNone(self.e.db.execute("SELECT 1 FROM links WHERE profile_id='p1' AND provider='github-profile'").fetchone())
