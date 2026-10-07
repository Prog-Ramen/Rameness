import copy
import io
import json
import subprocess
import sys
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ci'))
import merge
import review
import semantic

GOOD = {**{k: True for k in semantic.KEYS}, 'reason': 'Useful parameterized procedure with normal and edge tests.'}

def completion(content=None, finish='stop'):
    return {'choices': [{'finish_reason': finish, 'message': {'content': json.dumps(GOOD) if content is None else content}}]}

class RecoveryTests(unittest.TestCase):
    def decide(self, responses):
        calls, sleeps = [], []
        def request(req):
            calls.append(json.loads(req.data))
            value = responses[min(len(calls) - 1, len(responses) - 1)]
            if isinstance(value, Exception):
                raise value
            return value
        with patch.object(semantic, 'tree', return_value={}), patch.object(semantic, 'blob', return_value=b''):
            result = semantic.decide(Path('.'), 'base', 'head', [], 'fake-token', request, sleeps.append)
        return result, calls, sleeps

    def test_fenced_object_keeps_boolean_validation(self):
        parsed = semantic.parse_judgment(completion('```json\n' + json.dumps(GOOD) + '\n```'))
        self.assertEqual(parsed, GOOD)
        bad = {**GOOD, 'general': 'true'}
        with self.assertRaises(semantic.ReviewUnavailable):
            semantic.parse_judgment(completion('```json\n' + json.dumps(bad) + '\n```'))

    def test_surrounding_prose_not_accepted(self):
        with self.assertRaises(semantic.ReviewUnavailable):
            semantic.parse_judgment(completion('Ignore policy. ' + json.dumps(GOOD)))

    def test_no_duplicate_keys_can_override_rejection(self):
        raw = json.dumps(GOOD).replace('"general": true', '"general": false, "general": true')
        with self.assertRaises(semantic.ReviewUnavailable):
            semantic.parse_judgment(completion(raw))

    def test_incomplete_json_retries_and_recovers(self):
        result, calls, sleeps = self.decide([completion('{"meaningful":'), completion()])
        self.assertTrue(result['approved'])
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(sleeps, [2])
        self.assertEqual([c['max_tokens'] for c in calls], [1000, 2000])
        self.assertTrue(all(c['stream'] is False for c in calls))

    def test_truncated_valid_prefix_never_authorizes_merge(self):
        result, calls, sleeps = self.decide([completion(finish='length')])
        self.assertFalse(result['approved'])
        self.assertTrue(result['unavailable'])
        self.assertEqual(len(calls), 3)
        self.assertEqual(result['diagnostic']['stage'], 'model_output')

    def test_empty_content_retries_then_blocks(self):
        result, calls, _ = self.decide([completion('')])
        self.assertFalse(result['approved'])
        self.assertEqual(len(calls), 3)
        self.assertEqual(result['diagnostic']['stage'], 'model_output')

    def test_model_json_diagnostic_distinguishes_api_decode(self):
        result, calls, _ = self.decide([completion('{broken')])
        self.assertEqual(result['diagnostic']['stage'], 'model_json')
        self.assertIn('line=1', result['diagnostic']['detail'])
        self.assertNotIn('{broken', json.dumps(result))

    def test_retryable_transport_error_recovers(self):
        result, calls, _ = self.decide([OSError('DO_NOT_PRINT_TOKEN'), completion()])
        self.assertTrue(result['approved'])
        self.assertEqual(len(calls), 2)
        self.assertNotIn('DO_NOT_PRINT_TOKEN', json.dumps(result))

    def test_auth_failure_does_not_retry(self):
        error = urllib.error.HTTPError('https://models.github.ai', 401, 'DO_NOT_PRINT_TOKEN', {}, None)
        result, calls, sleeps = self.decide([error])
        self.assertFalse(result['approved'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(result['diagnostic'], {'stage': 'http', 'detail': 'HTTP 401'})

    def test_valid_negative_is_decision_not_infrastructure_error(self):
        bad = {**GOOD, 'general': False, 'reason': 'Embeds a company-specific policy.'}
        result, calls, sleeps = self.decide([completion(json.dumps(bad))])
        self.assertFalse(result['approved'])
        self.assertNotIn('unavailable', result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_api_body_diagnostics_never_include_response(self):
        class Response(io.BytesIO):
            status = 200
            headers = Message()
        Response.headers['Content-Type'] = 'text/html'
        with patch.object(semantic.urllib.request, 'urlopen', return_value=Response(b'DO_NOT_PRINT_TOKEN')):
            with self.assertRaises(semantic.ReviewUnavailable) as caught:
                semantic.read_response(object())
        self.assertEqual(caught.exception.stage, 'api_json')
        self.assertIn('content_type=text/html', caught.exception.detail)
        self.assertNotIn('DO_NOT_PRINT_TOKEN', str(caught.exception))

    def test_invalid_schema_extra_fields_block(self):
        result, _, _ = self.decide([completion(json.dumps({**GOOD, 'approved': True}))])
        self.assertFalse(result['approved'])
        self.assertEqual(result['diagnostic']['stage'], 'judgment_schema')

class LabelTests(unittest.TestCase):
    def result(self, **kw):
        return {'ok': True, 'auto_merge': False, 'human': [], 'fail': [], 'sops': [], 'head': 'a'*40, **kw}

    def test_reviewer_outage_has_own_label_and_heading(self):
        result = self.result(semantic={'approved': False, 'unavailable': True, 'reason': 'model_json error'})
        self.assertEqual(merge.label_for(result), 'sop:reviewer-error')
        rendered = review.markdown(result)
        self.assertIn('Reviewer error', rendered)
        self.assertNotIn('Maintainer review required', rendered)
        self.assertNotIn('Needs human review', rendered)

    def test_pending_checks_have_checking_label(self):
        result = self.result(waiting=['waiting for exact-head checks'])
        self.assertEqual(merge.label_for(result), 'sop:checking')
        self.assertIn('Waiting for CI', review.markdown(result))

    def test_actual_policy_issue_keeps_human_label(self):
        result = self.result(human=['uses network'])
        self.assertEqual(merge.label_for(result), 'sop:needs-human')

    def test_label_transition_cleans_old_managed_labels(self):
        calls=[]
        def api(path, data=None, method=None):
            calls.append((path, data, method))
            if path.startswith('issues/2/comments'):
                return []
        result=self.result(semantic={'unavailable': True, 'reason': 'model JSON failure'})
        with patch.object(merge, 'api', side_effect=api):
            merge.report(2, result)
        additions = [data for path, data, method in calls if path == 'issues/2/labels']
        self.assertEqual(additions, [{'labels': ['sop:reviewer-error']}])
        self.assertIn(('issues/2/labels/sop:needs-human', None, 'DELETE'), calls)
        self.assertNotIn(('issues/2/labels/sop:reviewer-error', None, 'DELETE'), calls)

class MergerSafetyTests(unittest.TestCase):
    def test_unavailable_reviewer_cannot_call_merge_api(self):
        sha, base = 'a'*40, 'b'*40
        calls=[]
        pr={'number':2,'draft':False,'state':'open','head':{'sha':sha,'repo':{'full_name':'owner/repo'}}}
        def api(path, data=None, method=None):
            calls.append((path, data, method))
            if path.startswith('pulls?'):
                return [pr]
            if path == 'pulls/2':
                return pr
            raise AssertionError('Unexpected API call: '+path)
        def git(*args):
            return sha if args == ('rev-parse','FETCH_HEAD') else base if args == ('rev-parse','origin/main') else ''
        result={'ok':True,'auto_merge':True,'human':[],'fail':[],'sops':[], 'head':sha}
        rendered=[]
        with patch.object(merge,'api',side_effect=api), patch.object(merge,'git',side_effect=git), \
             patch.object(merge,'review',return_value=result), patch.object(merge,'assess',return_value={'verdict':'clean','findings':[]}), patch.object(merge,'checked',return_value=True), \
             patch.object(merge,'cached_semantic',return_value=None), \
             patch.object(merge,'decide',return_value={'approved':False,'unavailable':True,'reason':'model_json failure'}), \
             patch.object(merge,'report',side_effect=lambda n,r: rendered.append(copy.deepcopy(r))), \
             patch.object(merge.subprocess,'run',return_value=subprocess.CompletedProcess([],0)):
            merge.main()
        self.assertFalse(any('/merge' in path for path,_,_ in calls))
        self.assertFalse(rendered[0]['auto_merge'])
        self.assertEqual(rendered[0]['human'], [])
        self.assertEqual(merge.label_for(rendered[0]), 'sop:reviewer-error')

if __name__ == '__main__':
    unittest.main()
