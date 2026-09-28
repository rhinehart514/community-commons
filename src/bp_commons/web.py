"""Loopback-only workbench over the evidence and reconciliation stores."""
import json
from pathlib import Path
from .enrichment import Enrichment
from urllib.parse import urlsplit
from flask import Flask, request, jsonify, abort
from werkzeug.exceptions import HTTPException
from .store import Commons
from .reviews import Reconciliation
from .workspaces import Workspaces, discover
from .paths import paths


def create_app(database, audit_database):
    app = Flask(__name__, static_url_path='/static')
    app.config.update(MAX_CONTENT_LENGTH=50 * 1024 * 1024, TRUSTED_HOSTS=['localhost', '127.0.0.1', '[::1]'])

    @app.before_request
    def local_requests():
        if request.method == 'POST':
            if request.headers.get('X-BP-Commons') != 'workbench':
                abort(403)
            origin = request.headers.get('Origin')
            if origin and urlsplit(origin).netloc != request.host:
                abort(403)

    @app.after_request
    def headers(response):
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(KeyError)
    def missing(error):
        return jsonify(error='Record or dataset not found'), 404

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    def body():
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError('Expected a JSON object')
        return data

    @app.get('/')
    def index():
        return app.send_static_file('index.html')

    @app.get('/api/stats')
    def stats():
        with Commons(database) as store:
            return {**store.stats(), 'collectors': [dict(r) for r in store.connection.execute('SELECT collector, count(*) AS count FROM profiles GROUP BY collector ORDER BY collector')]}

    @app.get('/api/profiles')
    def profiles():
        with Commons(database) as store:
            return store.browse(request.args.get('q', ''), collector=request.args.get('collector') or None, offset=int(request.args.get('offset', 0)))

    @app.get('/api/profile/<path:record_id>')
    def profile(record_id):
        with Commons(database) as store:
            return store.profile(record_id)

    @app.get('/api/history')
    def history():
        with Commons(database) as store:
            return jsonify(store.history(limit=100))

    @app.get('/api/changes/<snapshot_id>')
    def changes(snapshot_id):
        with Commons(database) as store:
            return jsonify(store.changes(snapshot_id, limit=50, offset=int(request.args.get('offset', 0))))

    @app.get('/api/catalog')
    def catalog():
        with Reconciliation(audit_database) as audit:
            return audit.catalog()

    @app.get('/api/runs/<run_id>')
    def run(run_id):
        with Reconciliation(audit_database) as audit:
            result = audit.run(run_id)
        if request.args.get('download'):
            response = jsonify(result)
            response.headers['Content-Disposition'] = f'attachment; filename="comparison-{run_id}.json"'
            return response
        offset = int(request.args.get('offset', 0))
        if offset < 0:
            raise ValueError('Offset must be nonnegative')
        rows = [r for r in result['results'] if not request.args.get('status') or r['status'] == request.args['status']]
        return {**result, 'total': len(rows), 'results': rows[offset:offset + 30]}

    @app.get('/api/runs/<run_id>/pair')
    def pair(run_id):
        with Reconciliation(audit_database) as audit:
            return audit.pair(run_id, request.args['left'], request.args['right'])

    @app.post('/api/compare')
    def compare():
        data = body()
        with Reconciliation(audit_database) as audit:
            left = data.get('left')
            right = data.get('right')
            if isinstance(left, str):
                left = audit.dataset(left)
            if isinstance(right, str):
                right = audit.dataset(right)
            if not isinstance(left, dict) or not isinstance(right, dict):
                raise ValueError('Select or upload two datasets')
            result = audit.compare(left, right)
            return {'run_id': result['run_id']}

    @app.post('/api/runs/<run_id>/review')
    def review(run_id):
        data = body()
        keys = ('left_id', 'right_id', 'verdict', 'reviewer', 'reason')
        if any(not isinstance(data.get(k), str) for k in keys):
            raise ValueError('Review requires two records, a verdict, your name, and a reason')
        with Reconciliation(audit_database) as audit:
            saved = audit.review(run_id, **{k: data[k] for k in keys})
        return saved

    @app.post('/api/runs/<run_id>/rerun')
    def rerun(run_id):
        with Reconciliation(audit_database) as audit:
            row = audit.db.execute('SELECT left_digest,right_digest FROM runs WHERE id=?', (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            result = audit.compare(audit.dataset(row[0]), audit.dataset(row[1]))
            return {'run_id': result['run_id']}

    evidence_path = Path(audit_database).with_name('enrichment.sqlite')

    @app.get('/api/enrichment')
    def enrichment():
        with Enrichment(evidence_path) as evidence:
            return {**evidence.claims(request.args.get('q', ''), offset=int(request.args.get('offset', 0))), 'stats': evidence.stats()}

    @app.get('/api/transitions')
    def transitions():
        with Enrichment(evidence_path) as evidence:
            return jsonify(evidence.transitions(request.args.getlist('institution')))

    @app.get('/api/evidence/<int:response_id>')
    def raw_evidence(response_id):
        with Enrichment(evidence_path) as evidence:
            row = evidence.db.execute('SELECT raw FROM responses WHERE id=?', (response_id,)).fetchone()
            if row is None:
                raise KeyError(response_id)
            return app.response_class(row[0], mimetype='application/json', headers={'Content-Disposition': f'attachment; filename="evidence-{response_id}.json"'})

    workspace_path = Path(audit_database).with_name('workspaces.sqlite')

    @app.get('/api/workspaces')
    def workspaces():
        with Workspaces(workspace_path) as work:
            return jsonify(work.list())

    @app.post('/api/workspaces')
    def create_workspace():
        with Workspaces(workspace_path) as work:
            return work.create(body().get('name'))

    @app.get('/api/discover')
    def discovery():
        with Workspaces(workspace_path) as work, Commons(database) as store, Enrichment(evidence_path) as evidence:
            return discover(store, evidence, work.records(request.args.get('workspace', 'personal')), request.args.get('q', ''), request.args.get('collector') or None, request.args.get('mode', 'all'), int(request.args.get('offset', 0)))

    @app.get('/api/workspaces/<workspace>/profiles/<profile_id>')
    def annotation(workspace, profile_id):
        with Workspaces(workspace_path) as work:
            return {'current': work.records(workspace).get(profile_id, {}), 'history': work.history(workspace, profile_id)}

    @app.post('/api/workspaces/<workspace>/profiles/<profile_id>')
    def save_annotation(workspace, profile_id):
        with Commons(database) as store:
            store.profile(profile_id)
        with Workspaces(workspace_path) as work:
            return work.save(workspace, profile_id, body())

    @app.get('/api/workspaces/<workspace>/export')
    def export_workspace(workspace):
        with Workspaces(workspace_path) as work, Commons(database) as store:
            rows = []
            for profile_id, state in work.records(workspace).items():
                if state['selection'] != 'shortlisted':
                    continue
                try:
                    profile = store.profile(profile_id)['profile']
                except KeyError:
                    profile = None
                rows.append({'profile_id': profile_id, 'profile': profile, 'workspace_record': state})
            response = jsonify({'workspace': workspace, 'records': rows})
            response.headers['Content-Disposition'] = 'attachment; filename="shortlist.json"'
            return response

    @app.get('/api/paths')
    def evidence_paths():
        with Commons(database) as store:
            return paths(store, request.args['source'], request.args['target'])

    return app
