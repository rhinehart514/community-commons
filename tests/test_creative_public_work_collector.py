import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


COLLECTOR = Path(__file__).resolve().parents[1] / "scripts/work_discovery/creative_public_work.py"
SPEC = importlib.util.spec_from_file_location("creative_public_work_collector", COLLECTOR)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


class CreativePublicWorkCollectorTest(unittest.TestCase):
    def test_caption_prefix_and_trailing_navigation_are_removed(self):
        self.assertEqual(collector.normalize_person_name('Facebook Casey Example'), 'Casey Example')
        self.assertEqual(collector.normalize_person_name('Executive Director Casey Example. See'), 'Casey Example')

    def test_group_show_notes_do_not_establish_an_individual_guest(self):
        self.assertTrue(collector.podcast_credit_uncertainty('Example Ensemble',
            'Example Ensemble is a band with three members.', 'Interview with Example Ensemble'))

    def test_offline_cache_miss_never_requests_network(self):
        with tempfile.TemporaryDirectory() as directory:
            c = collector.Collector(Path(directory), 10, 60, offline=True)
            with patch.object(collector, 'urlopen') as network:
                self.assertIsNone(c.fetch('https://example.test/missing', 'missing'))
                network.assert_not_called()
            self.assertEqual(c.requests, 0)

    def test_credit_headings_and_organizations_are_not_people(self):
        self.assertFalse(collector.is_person_name("Exhibition Artists"))
        self.assertFalse(collector.is_person_name("Fictional Regional University"))
        self.assertFalse(collector.is_person_name("A Retrospective"))
        self.assertTrue(collector.is_person_name("Fictional Name"))

    def test_event_title_is_not_mistaken_for_filmmaker_credit(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            c = collector.Collector(output, max_requests=10, max_seconds=60)
            url = "https://example.test/events/synthetic-program"
            body = b"""<html><head><title>Synthetic Film Program | 2025 Archive</title></head>
            <body><h1>View Lineup</h1><p>\"A Useful Short\" Fictional Director (Dir), 12 minutes.</p>
            <p>Panelists: Director Fictional Director, Producer Imaginary Producer, and Documentary Participant Fictional Subject</p>
            <p>Moderator: Fictional Moderator</p></body></html>"""
            raw = output / "raw" / "synthetic.html"
            raw.write_bytes(body)
            page = c.parse_html(body)
            collector.parse_biff_page(c, url, body, raw, page, "2025")
            film_work = next(row for row in c.works if row["kind"] == "film")
            self.assertEqual(film_work["title"], "A Useful Short")
            credits = {(row["name"], row["role"]) for row in c.contributions}
            self.assertIn(("Fictional Director", "director"), credits)
            self.assertNotIn(("Synthetic Film Program", "director"), credits)
            self.assertIn(("Imaginary Producer", "producer"), credits)
            self.assertIn(("Fictional Subject", "documentary participant"), credits)
            self.assertIn(("Fictional Moderator", "moderator"), credits)

    def test_group_credit_is_unresolved_not_projected_as_person(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            c = collector.Collector(output, max_requests=10, max_seconds=60)
            url = "https://example.test/events/synthetic-gallery"
            body = b"<html><head><title>Synthetic Exhibition</title></head><body><p>Curated by Fictional Community Association. Artist: Fictional Person</p></body></html>"
            raw = output / "raw" / "synthetic.html"
            raw.write_bytes(body)
            page = c.parse_html(body)
            collector.extract_short_credit_text(
                page.text, "Synthetic Exhibition", "exhibition", url, page, c,
                organization="Synthetic Gallery", regional="The listing names Buffalo, New York.", raw=raw,
            )
            self.assertEqual([row["name"] for row in c.contributions], ["Fictional Person"])
            self.assertTrue(any("Fictional Community Association" in row["name"] for row in c.unresolved.values()))


if __name__ == "__main__":
    unittest.main()
