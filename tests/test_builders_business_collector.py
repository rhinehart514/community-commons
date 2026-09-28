"""Offline regression fixtures for builders_business credit extraction."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts" / "work_discovery"))

from builders_business import Collector


class BuildersBusinessCollectorTests(unittest.TestCase):
    def test_rit_comma_joined_team_becomes_individual_credits(self):
        markup = "Team Members<br/><span>Riley Fiction,Jordan Sample,eag8276</span>"

        self.assertEqual(
            Collector.get_rit_team(markup),
            ["Riley Fiction", "Jordan Sample"],
        )

    def test_rochester_department_page_is_not_a_project_or_person_credit(self):
        department_url = "https://www.hajim.rochester.edu/senior-design-day/me"
        project_url = "https://www.hajim.rochester.edu/senior-design-day/mock-tow-tank"
        categories = {department_url}
        markup = "<h1>Mechanical Engineering</h1><p>Team Members</p><p>Mechanical Engineering Department</p>"

        self.assertFalse(Collector.is_ur_project_url(department_url, categories))
        self.assertEqual(Collector.extract_ur_project_team(department_url, markup, categories), [])
        self.assertTrue(Collector.is_ur_project_url(project_url, categories))

    def test_project_page_team_heading_yields_only_listed_members(self):
        url = "https://www.hajim.rochester.edu/senior-design-day/mock-tow-tank"
        markup = "<h1>Mock Tow Tank</h1><h2>Team Members</h2><ul><li>Riley Fiction</li><li>Jordan Sample</li></ul><h2>Abstract</h2><p>Build a teaching prototype.</p>"

        self.assertEqual(Collector.extract_ur_project_team(url, markup, set()), ["Riley Fiction", "Jordan Sample"])

    def test_rochester_team_list_excludes_artifact_and_nonmember_sections(self):
        markup = """
        <h2>Team Members</h2><p>Riley Fiction<br />Jordan Sample</p>
        <h2>Final Rendered Spaceframe</h2><p>Not a person credit</p>
        <h2>Supervisors</h2><ul><li>Professor Fiction</li></ul>
        <h2>Customers</h2><p>Casey Example</p>
        """

        self.assertEqual(Collector.get_ur_team(markup), ["Riley Fiction", "Jordan Sample"])

    def test_aia_parser_uses_named_project_credit_not_collective_heading(self):
        markup = """<p>Riley Fiction and Jordan Sample received the 2024 Student Project of the Year award for “MOCK,” a housing concept for a community design studio.</p>
        <h2>Architecture Students</h2>
        <p>Architecture Students at Example University attended the annual award event.</p>
        """

        projects = Collector.extract_aia_projects(markup)

        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["title"], "MOCK")
        self.assertEqual(projects[0]["people"], ["Riley Fiction", "Jordan Sample"])
        self.assertNotIn("Architecture Students", projects[0]["people"])


if __name__ == "__main__":
    unittest.main()
