import importlib.util
import os
import pathlib
import unittest
from unittest.mock import patch

path = pathlib.Path(__file__).resolve().parents[1] / 'src' / 'report.py'
spec = importlib.util.spec_from_file_location('report_under_test', path)
report = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, {'META_ACCESS_TOKEN': 'test', 'META_AD_ACCOUNT_ID': 'act_test'}):
    spec.loader.exec_module(report)


class PaginationTests(unittest.TestCase):
    def test_campaigns_include_second_page(self):
        pages = [
            {'data': [{'id': '1'}], 'paging': {'next': 'next-page', 'cursors': {'after': 'a'}}},
            {'data': [{'id': '2'}]},
        ]
        with patch.object(report, 'meta_get', side_effect=pages):
            self.assertEqual(set(report.fetch_meta_campaigns()), {'1', '2'})

    def test_insights_include_second_page(self):
        pages = [
            {'data': [{'adset_id': '1'}], 'paging': {'next': 'next-page', 'cursors': {'after': 'a'}}},
            {'data': [{'adset_id': '2'}]},
        ]
        with patch.object(report, 'meta_get', side_effect=pages):
            self.assertEqual(len(report.fetch_meta_adset_insights()), 2)


if __name__ == '__main__':
    unittest.main()
