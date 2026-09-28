import csv
import importlib.util
import json
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit


COLLECTOR = Path(__file__).resolve().parents[1] / "scripts/work_discovery/funded_inventions.py"
SPEC = importlib.util.spec_from_file_location("funded_inventions_collector", COLLECTOR)
collector_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector_module)


class FundedInventionsCollectorTest(unittest.TestCase):
    def make_collector(self, root, max_requests=100):
        return collector_module.Collector(Path(root), max_requests=max_requests, max_seconds=30)

    def test_sbir_ids_are_stable_and_distinguish_collision_and_duplicate_rows(self):
        columns = [
            "Company", "Award Title", "Agency", "Branch", "Phase", "Program",
            "Agency Tracking Number", "Contract", "Proposal Award Date", "City", "State",
            "PI Name", "PI Title",
        ]
        rows = [
            ["Fictional Widgets LLC", "Synthetic valve", "NSF", "", "I", "SBIR", "DUP-1", "C-1", "2025-01-02", "Buffalo", "New York", "Synthetic PI One", "PI"],
            ["Fictional Widgets LLC", "Synthetic sensor", "NIH", "", "II", "STTR", "DUP-1", "C-2", "2025-01-03", "Buffalo", "New York", "Example PI Two", "PI"],
            ["Fictional Widgets LLC", "Synthetic sensor", "NIH", "", "II", "STTR", "DUP-1", "C-2", "2025-01-03", "Buffalo", "New York", "Example PI Two", "PI"],
        ]
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        writer.writerows(rows)
        payload = buffer.getvalue().encode("utf-8")

        def collect_ids():
            with tempfile.TemporaryDirectory() as tmp:
                current = self.make_collector(tmp)

                def fake_request(url, *, raw_name=None, **kwargs):
                    raw_path, digest = current.save_raw(raw_name, payload)
                    return payload, 200, raw_path, digest, None

                current.request_bytes = fake_request
                current.do_sbir()
                self.assertEqual(len(current.works), 3)
                self.assertEqual(len(current.contributions), 3)
                return {work_id for work_id in current.works}

        first_ids = collect_ids()
        self.assertEqual(first_ids, collect_ids())
        self.assertEqual(len(first_ids), 3)

    def test_nih_named_pi_uses_profile_id_and_organization_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = self.make_collector(tmp)
            result = {
                "meta": {"total": 1, "offset": 0, "limit": 500},
                "results": [{
                    "appl_id": 900001,
                    "subproject_id": None,
                    "fiscal_year": 2025,
                    "project_num": "1R01TEST0001-01",
                    "project_title": "Fictional regional project",
                    "project_start_date": "2025-10-01T00:00:00",
                    "project_end_date": "2026-09-30T00:00:00",
                    "project_detail_url": "https://reporter.nih.gov/project-details/900001",
                    "abstract_text": "This fictional proposal studies a synthetic topic.",
                    "organization": {"org_name": "Example Regional Institute", "org_city": "AMHERST", "org_state": "NY"},
                    "principal_investigators": [{
                        "profile_id": 123456,
                        "full_name": "Synthetic NIH Investigator",
                        "is_contact_pi": True,
                    }],
                }],
            }

            def fake_request(url, *, raw_name=None, **kwargs):
                payload = kwargs.get("data", b"")
                criteria = json.loads(payload).get("criteria", {})
                matched = "AMHERST" in criteria.get("org_cities", [])
                body = json.dumps(result if matched else {"meta": {"total": 0}, "results": []}).encode("utf-8")
                raw_path, digest = current.save_raw(raw_name, body)
                return body, 200, raw_path, digest, None

            current.request_bytes = fake_request
            current.do_nih()

            work = current.works["nih:900001"]
            self.assertEqual(work["geography_scope"], "Buffalo / Western New York")
            self.assertIn("AMHERST, NY", work["regional_evidence"])
            self.assertEqual(work["work_date"], "2025-10-01")
            person = next(iter(current.contributions.values()))
            self.assertEqual(person["name"], "Synthetic NIH Investigator")
            self.assertEqual(person["role"], "principal investigator (contact PI)")
            self.assertEqual(person["identifiers"], {"nih_profile_id": "123456"})

    def test_nsf_named_pis_use_award_fields_and_performance_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = self.make_collector(tmp)
            award = {
                "id": "SYNTHETIC-NSF-1",
                "title": "Fictional Buffalo award",
                "abstractText": "This fictional award supports a synthetic research proposal.",
                "awardeeName": "Example Regional University",
                "perfCity": "Buffalo",
                "perfStateCode": "NY",
                "perfLocation": "Example Regional University",
                "startDate": "03/04/2025",
                "pdPIName": "Synthetic NSF Investigator",
                "piId": "PI-123",
                "coPDPI": ["Example Co Investigator"],
            }

            def fake_request(url, *, raw_name=None, **kwargs):
                city = parse_qs(urlsplit(url).query).get("perfCity", [""])[0]
                body = json.dumps({
                    "response": {
                        "metadata": {"totalCount": 1 if city == "Buffalo" else 0},
                        "award": [award] if city == "Buffalo" else [],
                    }
                }).encode("utf-8")
                raw_path, digest = current.save_raw(raw_name, body)
                return body, 200, raw_path, digest, None

            current.request_bytes = fake_request
            current.do_nsf()

            work = current.works["nsf:SYNTHETIC-NSF-1"]
            self.assertEqual(work["geography_scope"], "Buffalo / Western New York")
            self.assertIn("Buffalo, NY", work["regional_evidence"])
            self.assertEqual(work["work_date"], "2025-03-04")
            people = {(item["name"], item["role"]): item for item in current.contributions.values()}
            self.assertEqual(people[("Synthetic NSF Investigator", "principal investigator")]["identifiers"], {"nsf_pi_id": "PI-123"})
            self.assertIn(("Example Co Investigator", "co-principal investigator"), people)


if __name__ == "__main__":
    unittest.main()
