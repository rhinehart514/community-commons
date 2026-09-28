import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


COLLECTOR = Path(__file__).resolve().parents[1] / "scripts/work_discovery/research_work.py"
SPEC = importlib.util.spec_from_file_location("research_work_collector", COLLECTOR)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


class ResearchWorkCollectorTest(unittest.TestCase):
    def test_cached_response_without_original_observation_date_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'cache/researchers/raw'
            cache.mkdir(parents=True)
            (cache / 'works-responses.jsonl').write_text(json.dumps({'response': {'results': []}}) + '\n')
            sources, errors = [], []
            collector.cached_openalex(root / 'output', {}, root / 'cache', {}, {}, {}, sources, errors)
            self.assertEqual(sources[0]['records'], 0)
            self.assertIn('original timezone-aware fetched_at', errors[0])
            self.assertTrue((root / 'output/raw/cached-openalex-page-001.jsonl').exists())

    def test_existing_output_is_preserved_and_requires_a_new_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / 'preserved.txt'
            existing.write_text('Existing evidence')
            with patch('sys.argv', ['research_work.py', '--output', directory]):
                with self.assertRaisesRegex(SystemExit, 'not empty'):
                    collector.main()
            self.assertEqual(existing.read_text(), 'Existing evidence')

    def test_roster_headings_are_not_mistaken_for_people(self):
        for heading in (
            "Graduate Students",
            "Clinical Specialist",
            "Research Project Results",
            "Filter by Research Faculty Member",
        ):
            self.assertFalse(collector.name_like(heading), heading)
        self.assertEqual(
            collector.heading_names("Synthetic Alpha | Example Beta | she/her"),
            ["Synthetic Alpha", "Example Beta"],
        )

    def test_lab_roster_keeps_explicit_current_and_former_roles(self):
        blocks = [
            ("h1", "Team"),
            ("h2", "Current Members"),
            ("h3", "Synthetic Alpha"),
            ("h2", "Lab Alumni"),
            ("h3", "Example Beta"),
            ("h2", "Research Projects"),
            ("h3", "Synthetic Project"),
        ]
        contributions = {}
        count = collector.lab_contributions(blocks, "lab:test", "https://example.test/lab", "test_scope", contributions)
        self.assertEqual(count, 2)
        self.assertEqual(contributions[("lab:test", "Synthetic Alpha")]["role"], "current lab member")
        self.assertEqual(contributions[("lab:test", "Example Beta")]["role"], "former lab member")
        self.assertNotIn(("lab:test", "Synthetic Project"), contributions)

    def test_only_the_author_with_explicit_regional_authorship_is_projected(self):
        institutions = {
            "I_LOCAL_TEST": {"name": "Example Regional University", "scope": "buffalo_western_new_york"},
        }
        work = {
            "id": "https://openalex.org/WTEST",
            "display_name": "Synthetic research output",
            "publication_year": 2025,
            "authorships": [
                {
                    "author": {"id": "https://openalex.org/ATEST1", "display_name": "Synthetic Alpha"},
                    "author_position": "first",
                    "institutions": [{"id": "https://openalex.org/I_LOCAL_TEST"}],
                },
                {
                    "author": {"id": "https://openalex.org/ATEST2", "display_name": "Example Beta"},
                    "author_position": "middle",
                    "institutions": [{"id": "https://openalex.org/I_OTHER_TEST"}],
                },
            ],
        }
        works = {}
        contributions = {}
        unresolved = {}
        matched, _ = collector.openalex_record(
            work, institutions, ("raw/synthetic.json", "0" * 64), "2026-01-01T00:00:00Z",
            "live", works, contributions, unresolved,
        )
        self.assertTrue(matched)
        self.assertEqual([item["name"] for item in contributions.values()], ["Synthetic Alpha"])

    def test_organization_author_is_unresolved_not_a_person(self):
        institutions = {
            "I_LOCAL_TEST": {"name": "Example Regional University", "scope": "buffalo_western_new_york"},
        }
        work = {
            "id": "https://openalex.org/WORGTEST",
            "display_name": "Synthetic institutional output",
            "publication_year": 2025,
            "authorships": [{
                "author": {"id": "https://openalex.org/AORGTEST", "display_name": "Example Regional University"},
                "institutions": [{"id": "https://openalex.org/I_LOCAL_TEST"}],
            }],
        }
        works = {}
        contributions = {}
        unresolved = {}
        matched, added = collector.openalex_record(
            work, institutions, ("raw/synthetic.json", "0" * 64), "2026-01-01T00:00:00Z",
            "live", works, contributions, unresolved,
        )
        self.assertTrue(matched)
        self.assertEqual(added, 0)
        self.assertEqual(contributions, {})
        self.assertIn(("openalex:WORGTEST", "Example Regional University"), unresolved)


if __name__ == "__main__":
    unittest.main()
