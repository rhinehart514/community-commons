"""Synthetic attribution regressions for the public software-work collector."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/work_discovery/software_work.py"
SPEC = importlib.util.spec_from_file_location("software_work_collector", MODULE_PATH)
collector = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(collector)


class AttributionTests(unittest.TestCase):
    def test_stars_watches_and_forks_do_not_credit_authorship(self) -> None:
        for event_type in ("WatchEvent", "ForkEvent", "PublicEvent"):
            with self.subTest(event_type=event_type):
                event = {"type": event_type, "actor": {"login": "regional-example"}, "payload": {}}
                self.assertIsNone(collector.event_credit(event, "regional-example"))

    def test_push_credits_the_push_actor_without_calling_them_commit_author(self) -> None:
        event = {
            "id": "synthetic-push",
            "type": "PushEvent",
            "actor": {"login": "regional-example"},
            "repo": {"name": "sample/project"},
            "created_at": "2026-09-01T12:00:00Z",
            "payload": {"head": "abc123", "ref": "refs/heads/main"},
        }
        credit = collector.event_credit(event, "regional-example")
        self.assertIsNotNone(credit)
        self.assertEqual(credit["role"], "push actor; commit authorship is not established by this event")
        self.assertEqual(credit["source_url"], "https://github.com/sample/project/commit/abc123")

    def test_repository_owner_is_not_inferred_without_contributor_response(self) -> None:
        regional_people = {"regional-example": {"login": "regional-example"}}
        response_contributors = [{"login": "another-account", "contributions": 1}]
        self.assertEqual(collector.regional_contributors(response_contributors, regional_people), [])

    def test_bots_and_organizations_are_omitted_from_person_contributions(self) -> None:
        regional_people = {"regional-example": {"login": "regional-example", "name": "Regional Example"}}
        bot = {"login": "automation-bot", "type": "Bot", "contributions": 3}
        organization = {"login": "sample-org", "type": "Organization", "contributions": 4}
        self.assertIsNone(collector.contributor_account_credit(bot, regional_people))
        self.assertIsNone(collector.contributor_account_credit(organization, regional_people))

    def test_unknown_human_type_stays_labeled_as_a_github_account(self) -> None:
        credit = collector.contributor_account_credit(
            {"login": "unresolved-example", "type": "User", "contributions": 2},
            {},
        )
        self.assertIsNotNone(credit)
        self.assertIn("GitHub account", credit[0])
        self.assertIn("identity unverified", credit[1])

    def test_npm_maintainer_handle_alone_does_not_create_regional_repository_link(self) -> None:
        self.assertEqual(
            collector.verified_package_repository(
                "https://github.com/regional-example/project",
                "regional-example/project",
                {},
            ),
            "",
        )
        self.assertEqual(
            collector.verified_package_repository(
                "https://github.com/regional-example/project",
                "regional-example/project",
                {"regional-example/project": [{"login": "regional-example"}]},
            ),
            "regional-example/project",
        )

    def test_devpost_dependency_link_is_not_a_team_credit(self) -> None:
        page = collector.TeamProfileLinkParser()
        page.feed(
            '<div class="project-details"><a href="https://github.com/dependency/project">dependency</a></div>'
            '<section class="team-members"><a href="https://github.com/regional-example">member</a></section>'
        )
        self.assertEqual(page.links, ["https://github.com/regional-example"])

    def test_huggingface_profile_link_does_not_claim_ownership(self) -> None:
        credit = collector.huggingface_account_credit("work", "sample-modeler", "https://huggingface.co/sample-modeler/model")
        self.assertEqual(credit["person_url"], "")
        self.assertIn("ownership unverified", credit["role"])
        self.assertEqual(credit["identifiers"], {"huggingface": "sample-modeler"})

    def test_matching_pypi_display_name_remains_unlinked(self) -> None:
        credit = collector.pypi_author_credit("work", "Regional Example", "https://pypi.org/pypi/sample/json")
        self.assertEqual(credit["person_url"], "")
        self.assertEqual(credit["identifiers"], {})
        self.assertIn("identity unlinked", credit["role"])


if __name__ == "__main__":
    unittest.main()
