import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from bp_commons.external import ror_lookup, openalex_institution
from bp_commons.datasets import load_dataset


class ExternalTests(unittest.TestCase):
    def response(self, value):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(value).encode()
        return response

    def test_ror_preserves_response_and_does_not_assert_identity(self):
        org = {'id':'https://ror.org/example', 'names':[{'value':'Example University','types':['ror_display']}]}
        with tempfile.TemporaryDirectory() as directory, patch('bp_commons.external.urlopen', return_value=self.response({'items':[{'chosen':True,'organization':org}]})):
            output = Path(directory)/'ror.json'
            result = ror_lookup('Example University', output)
            data = load_dataset(output)
            self.assertFalse(data['coverage']['complete'])
            self.assertTrue(data['records'][0]['provider_suggested'])
            self.assertTrue(Path(result['raw_response']).is_file())
            with self.assertRaises(FileExistsError):
                ror_lookup('Example University', output)

    def test_openalex_preserves_ror_and_validates_id(self):
        org = {'id':'https://openalex.org/I1','display_name':'Example','ror':'https://ror.org/example'}
        with tempfile.TemporaryDirectory() as directory, patch('bp_commons.external.urlopen', return_value=self.response(org)):
            output = Path(directory)/'oa.json'
            openalex_institution('I1',output)
            self.assertEqual(load_dataset(output)['records'][0]['identifiers']['ror'],[org['ror']])
            with self.assertRaises(ValueError):
                openalex_institution('../other',output)

    def test_failed_fetch_does_not_create_dataset(self):
        with tempfile.TemporaryDirectory() as directory, patch('bp_commons.external.urlopen', side_effect=TimeoutError):
            output = Path(directory)/'ror.json'
            with self.assertRaises(TimeoutError):
                ror_lookup('Example',output)
            self.assertFalse(output.exists())
