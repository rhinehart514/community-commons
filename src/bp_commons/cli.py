import argparse
import json
import sqlite3
from .importer import import_snapshot
from .store import Commons


def main():
    parser = argparse.ArgumentParser(description="BP Commons — inspectable ecosystem evidence")
    parser.add_argument("--db", default="data/commons.sqlite")
    commands = parser.add_subparsers(dest="command", required=True)
    load = commands.add_parser("import", help="Import a complete export and retain history")
    load.add_argument("source", help="Directory containing people, evidence, and connections JSONL")
    refresh = commands.add_parser("refresh", help="Refresh from a complete export and report changes")
    refresh.add_argument("source")
    history = commands.add_parser("history")
    history.add_argument("--limit", type=int, default=20)
    changes = commands.add_parser("changes")
    changes.add_argument("snapshot_id")
    changes.add_argument("--limit", type=int, default=20)
    changes.add_argument("--offset", type=int, default=0)
    commands.add_parser("stats")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--collector")
    unresolved = commands.add_parser("unresolved", help="Inspect relationships needing identity review")
    unresolved.add_argument("--limit", type=int, default=20)
    unresolved.add_argument("--offset", type=int, default=0)
    show = commands.add_parser("show")
    show.add_argument("record_id")
    show.add_argument("--snapshot")
    compare = commands.add_parser("compare", help="Reconcile two source-scoped JSON datasets")
    compare.add_argument("left")
    compare.add_argument("right")
    compare.add_argument("--audit-db", default="data/reconciliation.sqlite")
    compare.add_argument("--output")
    review = commands.add_parser("review", help="Record an auditable pair decision")
    review.add_argument("run_id")
    review.add_argument("left_id")
    review.add_argument("right_id")
    review.add_argument("verdict", choices=["same", "different", "unsure"])
    review.add_argument("--reviewer", required=True)
    review.add_argument("--reason", required=True)
    review.add_argument("--audit-db", default="data/reconciliation.sqlite")
    run = commands.add_parser("comparison")
    run.add_argument("run_id")
    run.add_argument("--audit-db", default="data/reconciliation.sqlite")
    run.add_argument("--status", choices=["matched", "possible_match", "unmatched", "incomplete"])
    run.add_argument("--limit", type=int, default=20)
    run.add_argument("--offset", type=int, default=0)
    pair = commands.add_parser("pair", help="Inspect original records before reviewing a match")
    pair.add_argument("run_id")
    pair.add_argument("left_id")
    pair.add_argument("right_id")
    pair.add_argument("--audit-db", default="data/reconciliation.sqlite")
    export = commands.add_parser("export-dataset")
    export.add_argument("dataset_id")
    export.add_argument("output")
    export.add_argument("--collector")
    ror = commands.add_parser("ror", help="Fetch and preserve public ROR organization candidates")
    ror.add_argument("query")
    ror.add_argument("output")
    benchmark = commands.add_parser("evaluate")
    benchmark.add_argument("left")
    benchmark.add_argument("right")
    benchmark.add_argument("labels")
    oa = commands.add_parser("openalex-institution", help="Fetch one public institution and preserve its source response")
    oa.add_argument("institution_id")
    oa.add_argument("output")
    serve = commands.add_parser("serve", help="Open the local web workbench")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--audit-db", default="data/reconciliation.sqlite")
    enrich = commands.add_parser("enrich", help="Durable public evidence queue and bounded worker")
    enrich.add_argument("action", choices=["seed", "run", "status", "claims", "transitions"])
    enrich.add_argument("--evidence-db", default="data/enrichment.sqlite")
    enrich.add_argument("--limit", type=int, default=50)
    enrich.add_argument("--offset", type=int, default=0)
    enrich.add_argument("--query", default="")
    enrich.add_argument("--institution", action="append", default=[])
    enrich.add_argument("--max-requests", type=int, default=10)
    enrich.add_argument("--max-seconds", type=int, default=300)
    enrich.add_argument("--interval", type=float, default=2)
    enrich.add_argument("--watch", action="store_true", help="Wait for due work within the same time/request budget")
    discovery = commands.add_parser("discover", help="Headless source and enriched-evidence discovery")
    discovery.add_argument("query", nargs="?", default="")
    discovery.add_argument("--workspace", default="personal")
    discovery.add_argument("--workspace-db", default="data/workspaces.sqlite")
    discovery.add_argument("--evidence-db", default="data/enrichment.sqlite")
    discovery.add_argument("--collector")
    discovery.add_argument("--mode", choices=["all", "new", "shortlisted", "dismissed"], default="all")
    discovery.add_argument("--offset", type=int, default=0)
    workspace = commands.add_parser("workspace", help="Optional private consumer records")
    workspace.add_argument("action", choices=["list", "create", "show", "save"])
    workspace.add_argument("--workspace-db", default="data/workspaces.sqlite")
    workspace.add_argument("--workspace", default="personal")
    workspace.add_argument("--name")
    workspace.add_argument("--profile")
    workspace.add_argument("--input", help="JSON file with independent markers, selection, reviewer and reason")
    path = commands.add_parser("paths", help="Inspect a bounded path through documented shared entities")
    path.add_argument("source")
    path.add_argument("target")
    path.add_argument("--max-hops", type=int, default=4)
    feed = commands.add_parser("feed", help="Pull replayable changes in collected claims")
    feed.add_argument("--evidence-db", default="data/enrichment.sqlite")
    feed.add_argument("--after", type=int, default=0)
    feed.add_argument("--limit", type=int, default=100)
    subscription = commands.add_parser("subscription", help="Headless saved queries and bounded enrichment cycles")
    subscription.add_argument("action", choices=["create", "list", "evaluate", "events", "plan", "cycle", "pause", "resume"])
    subscription.add_argument("--consumer", required=True)
    subscription.add_argument("--id")
    subscription.add_argument("--name")
    subscription.add_argument("--query-file")
    subscription.add_argument("--annotations-file")
    subscription.add_argument("--subscription-db", default="data/subscriptions.sqlite")
    subscription.add_argument("--evidence-db", default="data/enrichment.sqlite")
    subscription.add_argument("--after", type=int, default=0)
    subscription.add_argument("--limit", type=int, default=25)
    subscription.add_argument("--apply", action="store_true")
    subscription.add_argument("--max-requests", type=int, default=10)
    subscription.add_argument("--max-seconds", type=int, default=300)
    args = parser.parse_args()
    try:
        if args.command == "feed":
            from .enrichment import Enrichment
            from .streams import ChangeFeed
            with Enrichment(args.evidence_db) as evidence:
                result = ChangeFeed(evidence).read(args.after, args.limit)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == "subscription":
            from pathlib import Path
            from .subscriptions import Subscriptions
            from .enrichment import Enrichment
            from .streams import ChangeFeed
            with Subscriptions(args.subscription_db) as subscriptions:
                if args.action == "create":
                    if not args.query_file:
                        raise ValueError('Creating a subscription requires --query-file')
                    result = subscriptions.create(args.consumer, args.name, json.loads(Path(args.query_file).read_text()))
                elif args.action == "list":
                    result = subscriptions.list(args.consumer)
                elif args.action == "events":
                    result = subscriptions.events(args.consumer, args.after, args.limit)
                elif args.action in ("pause", "resume"):
                    result = subscriptions.pause(args.consumer, args.id, args.action == "pause")
                else:
                    with Commons(args.db) as store, Enrichment(args.evidence_db) as evidence:
                        annotations = json.loads(Path(args.annotations_file).read_text()) if args.annotations_file else None
                        if annotations is not None and not isinstance(annotations, dict):
                            raise ValueError('Annotation input must be a profile ID mapping')
                        if args.action == "plan":
                            result = subscriptions.plan(args.consumer, args.id, store, evidence, args.limit, args.apply)
                        elif args.action == "cycle":
                            saved = subscriptions.get(args.consumer, args.id)
                            if saved['query']['mode'] != 'all' and annotations is None:
                                raise ValueError('This query requires --annotations-file')
                            if saved['paused']:
                                result = {'paused': True}
                            else:
                                if not 1 <= args.max_requests <= 100000 or not 1 <= args.max_seconds <= 86400:
                                    raise ValueError('Invalid worker budget')
                                plan = subscriptions.plan(args.consumer, args.id, store, evidence, args.limit, True)
                                worker = evidence.run(args.max_requests, args.max_seconds, targets=[(t['provider'], t['subject']) for t in plan['tasks']])
                                changes = ChangeFeed(evidence).sync()
                                result = {'plan': plan, 'worker': worker, 'claim_changes': changes, 'evaluation': subscriptions.evaluate(args.consumer, args.id, store, evidence, annotations)}
                        else:
                            result = subscriptions.evaluate(args.consumer, args.id, store, evidence, annotations)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == "paths":
            from .paths import paths
            with Commons(args.db) as store:
                result = paths(store, args.source, args.target, args.max_hops)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command in ("discover", "workspace"):
            from pathlib import Path
            from .workspaces import Workspaces, discover
            from .enrichment import Enrichment
            with Workspaces(args.workspace_db) as work:
                if args.command == "discover":
                    with Commons(args.db) as store, Enrichment(args.evidence_db) as evidence:
                        result = discover(store, evidence, work.records(args.workspace), args.query, args.collector, args.mode, args.offset)
                elif args.action == "list":
                    result = work.list()
                elif args.action == "create":
                    result = work.create(args.name)
                elif args.action == "show":
                    result = work.records(args.workspace)
                else:
                    if not args.profile or not args.input:
                        raise ValueError('Saving requires --profile and --input')
                    with Commons(args.db) as store:
                        store.profile(args.profile)
                    result = work.save(args.workspace, args.profile, json.loads(Path(args.input).read_text()))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == "enrich":
            from .enrichment import Enrichment
            with Enrichment(args.evidence_db) as evidence:
                if args.action == "seed":
                    result = evidence.seed(args.db, args.limit)
                elif args.action == "run":
                    result = evidence.run(args.max_requests, args.max_seconds, args.interval, args.watch)
                elif args.action == "claims":
                    result = evidence.claims(args.query, args.limit, args.offset)
                elif args.action == "transitions":
                    result = evidence.transitions(args.institution)
                else:
                    result = evidence.stats()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == "serve":
            from .web import create_app
            create_app(args.db, args.audit_db).run(host="127.0.0.1", port=args.port, debug=False)
            return
        if args.command in {"compare", "review", "comparison", "pair", "export-dataset", "ror", "evaluate", "openalex-institution"}:
            from pathlib import Path
            from .datasets import load_dataset, from_commons
            from .reviews import Reconciliation
            from .external import ror_lookup, openalex_institution
            if args.command == "openalex-institution":
                result = openalex_institution(args.institution_id, args.output)
            elif args.command == "evaluate":
                from .evaluate import evaluate
                result = evaluate(load_dataset(args.left), load_dataset(args.right), json.loads(Path(args.labels).read_text()))
            elif args.command == "ror":
                result = ror_lookup(args.query, args.output)
            elif args.command == "export-dataset":
                result = from_commons(args.db, args.dataset_id, args.collector)
                with Path(args.output).open('x') as output:
                    json.dump(result, output, ensure_ascii=False, indent=2)
                result = {"output": args.output, "records": len(result["records"])}
            else:
                with Reconciliation(args.audit_db) as audit:
                    if args.command == "compare":
                        result = audit.compare(load_dataset(args.left), load_dataset(args.right))
                        if args.output:
                            with Path(args.output).open('x') as output:
                                json.dump(result, output, ensure_ascii=False, indent=2)
                            result = {"run_id": result['run_id'], "summary": result['summary'], "output": args.output}
                    elif args.command == "pair":
                        result = audit.pair(args.run_id, args.left_id, args.right_id)
                    elif args.command == "review":
                        result = audit.review(args.run_id, args.left_id, args.right_id, args.verdict, args.reviewer, args.reason)
                    else:
                        if not 1 <= args.limit <= 100 or args.offset < 0:
                            raise ValueError("limit must be 1–100 and offset nonnegative")
                        result = audit.run(args.run_id)
                        rows = [r for r in result['results'] if not args.status or r['status'] == args.status]
                        result['filtered_count'] = len(rows)
                        result['results'] = rows[args.offset:args.offset + args.limit]
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command in {"import", "refresh"}:
            report = import_snapshot(args.source, args.db)
        with Commons(args.db) as commons:
            if args.command in {"import", "refresh"}:
                result = {**report, "stats": commons.stats()}
            elif args.command == "stats":
                result = commons.stats()
            elif args.command == "history":
                result = commons.history(limit=args.limit)
            elif args.command == "changes":
                result = commons.changes(args.snapshot_id, limit=args.limit, offset=args.offset)
            elif args.command == "search":
                result = commons.search(args.query, limit=args.limit, collector=args.collector)
            elif args.command == "unresolved":
                result = commons.unresolved(limit=args.limit, offset=args.offset)
            else:
                result = commons.profile(args.record_id, snapshot_id=args.snapshot)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"bp-commons: {error}\n")


if __name__ == "__main__":
    main()
