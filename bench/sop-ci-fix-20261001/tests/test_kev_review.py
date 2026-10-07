import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ci'))
import kev_review
import semantic

class KevReviewTests(unittest.TestCase):
    def review(self, scores):
        with patch.dict(os.environ, {'SOP_KEV_URL': 'http://127.0.0.1:8008/v1/systemone'}):
            return kev_review.decide('untrusted SOP', lambda req: {'answers': {k: {'noul': v} for k, v in scores.items()}})

    def test_all_criteria_required(self):
        scores = dict.fromkeys(kev_review.CRITERIA, .99)
        self.assertTrue(self.review(scores)['approved'])
        scores['general'] = .94
        self.assertFalse(self.review(scores)['approved'])

    def test_malformed_scores_block(self):
        for invalid in (True, float('nan'), float('inf'), -1, 1.01, '.99', None):
            scores = dict.fromkeys(kev_review.CRITERIA, .99)
            scores['general'] = invalid
            with self.subTest(invalid=invalid):
                result = self.review(scores)
                self.assertFalse(result['approved'])
                self.assertTrue(result['unavailable'])
        self.assertTrue(self.review({})['unavailable'])

    def test_outage_and_remote_endpoint_block(self):
        with patch.dict(os.environ, {'SOP_KEV_URL': 'https://example.com/v1/systemone'}):
            result = kev_review.decide('private', lambda req: self.fail('remote request'))
        self.assertTrue(result['unavailable'])
        with patch.dict(os.environ, {'SOP_KEV_URL': 'http://127.0.0.1:8008/v1/systemone'}):
            result = kev_review.decide('private', lambda req: (_ for _ in ()).throw(TimeoutError('secret')))
        self.assertNotIn('secret', result['reason'])
        self.assertTrue(result['unavailable'])

    def test_backend_and_policy_cache(self):
        before = semantic.policy_id()
        with patch.dict(os.environ, {'SOP_REVIEW_BACKEND': 'kev'}), patch.object(semantic, 'tree', return_value={}):
            self.assertNotEqual(before, semantic.policy_id())
            result = semantic.decide(Path('.'), 'base', 'head', [], 'token', lambda req: {'answers': {k: {'noul': .99} for k in kev_review.CRITERIA}})
        self.assertTrue(result['approved'])
        self.assertEqual(result['backend'], 'kev')
