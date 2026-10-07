import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ci'))
import security
import review
import merge

FILES={'sops/example/task/run.py':'# INTERNAL USE ONLY\nimport json\n'}
FINDING={'category':'private','path':'sops/example/task/run.py','line':1,'evidence':'INTERNAL USE ONLY','explanation':'Explicit internal-only restriction.'}

class SecurityDetectionTests(unittest.TestCase):
    def test_private_and_explicit_nonpublic_metadata(self):
        files={'sops/x/y/sop.json':'{"scope":"private"}', 'sops/x/y/run.py':'# DO NOT DISTRIBUTE\n'}
        categories={f['category'] for f in security.static_findings(files)}
        self.assertEqual(categories, {'private','non_public'})

    def test_ordinary_license_and_copyright_not_prohibited(self):
        files={'sops/x/y/run.py':'# Copyright Example Inc. All rights reserved.\n# SPDX-License-Identifier: MIT\n'}
        self.assertEqual(security.static_findings(files), [])

    def test_destructive_root_operation_without_execution(self):
        for code in ['import shutil\nshutil.rmtree("/")', 'from shutil import rmtree as destroy\ndestroy("/")']:
            findings=security.static_findings({'sops/x/y/run.py':code})
            self.assertEqual(findings[0]['category'],'malicious')
        self.assertEqual(security.static_findings({'sops/x/y/run.py':'fake.rmtree("/")'}), [])

    def test_secret_detection_stops_before_external_model(self):
        creds=[{'category':'credentials','path':'sops/x/y/run.py','rule':'github-pat'}]
        with patch.object(security,'scan_secrets',return_value=creds), patch.object(security,'submission') as submission:
            result=security.assess(Path('.'),'base','head','fake')
        self.assertEqual(result['verdict'],'prohibited')
        submission.assert_not_called()

    def test_parser_verifies_source_evidence_and_does_not_return_it(self):
        response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'verdict':'prohibited','findings':[FINDING]})}}]}
        result=security.parse_security(response, FILES)
        self.assertEqual(result['findings'][0]['category'],'private')
        self.assertNotIn('INTERNAL USE ONLY',json.dumps(result))
        bad={**FINDING,'line':2}
        response['choices'][0]['message']['content']=json.dumps({'verdict':'prohibited','findings':[bad]})
        with self.assertRaises(security.ReviewUnavailable):
            security.parse_security(response, FILES)

    def model(self, verdict, findings):
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'verdict':verdict,'findings':findings})}}]}

    def test_one_model_finding_cannot_automatically_close(self):
        answers=iter([self.model('prohibited',[FINDING]),self.model('clean',[])])
        with patch.object(security,'scan_secrets',return_value=[]), patch.object(security,'submission',return_value=FILES), patch.object(security,'historical_findings',return_value=[]), patch.object(security,'static_findings',return_value=[]):
            result=security.assess(Path('.'),'b','h','fake',request=lambda req:next(answers))
        self.assertEqual(result['verdict'],'uncertain')

    def test_corroborated_findings_have_matching_category_file_and_line(self):
        with patch.object(security,'scan_secrets',return_value=[]), patch.object(security,'submission',return_value=FILES), patch.object(security,'historical_findings',return_value=[]), patch.object(security,'static_findings',return_value=[]):
            result=security.assess(Path('.'),'b','h','fake',request=lambda req:self.model('prohibited',[FINDING]))
        self.assertEqual(result['verdict'],'prohibited')
        self.assertEqual(result['source'],'corroborated-model-findings')

    def test_broken_model_blocks_without_closing(self):
        with patch.object(security,'scan_secrets',return_value=[]), patch.object(security,'submission',return_value=FILES), patch.object(security,'historical_findings',return_value=[]), patch.object(security,'static_findings',return_value=[]):
            result=security.assess(Path('.'),'b','h','fake',request=lambda req:{})
        self.assertEqual(result['verdict'],'unavailable')
        self.assertEqual(result['findings'],[])

class CleanupTests(unittest.TestCase):
    def run_cleanup(self, owner='owner/repo', branch='sop/test', sha='a'*40, others=None, delete_result=True, verdict='prohibited'):
        calls, deletes=[],[]
        def api(path,data=None,method=None):
            calls.append((path,data,method))
            if path=='pulls/2' and data is None:
                return {'state':'open','head':{'sha':sha,'ref':branch,'repo':{'full_name':owner}},'base':{'ref':'main'}}
            if path.startswith('pulls?'):
                return others or []
        def delete(branch,sha):
            deletes.append((branch,sha))
            return delete_result
        result=security.close_and_cleanup(api,'owner/repo','fake',2,'a'*40,{'verdict':verdict,'findings':[{'category':'private'}]},delete)
        return result,calls,deletes

    def test_close_then_delete_only_proposal_branch(self):
        result,calls,deletes=self.run_cleanup()
        self.assertEqual(result['cleanup'],'proposal-branch-deleted')
        self.assertIn(('pulls/2',{'state':'closed'},'PATCH'),calls)
        self.assertEqual(deletes,[('sop/test','a'*40)])

    def test_fork_branch_is_never_deleted(self):
        result,calls,deletes=self.run_cleanup(owner='outsider/fork')
        self.assertTrue(result['closed'])
        self.assertEqual(result['cleanup'],'fork-owner-must-remove-source')
        self.assertEqual(deletes,[])

    def test_main_and_nonproposal_branches_are_never_deleted(self):
        for branch in ['main','develop','ci/fix']:
            result,calls,deletes=self.run_cleanup(branch=branch)
            self.assertTrue(result['closed'])
            self.assertEqual(deletes,[])

    def test_shared_proposal_branch_is_not_deleted(self):
        result,calls,deletes=self.run_cleanup(others=[{'number':3}])
        self.assertEqual(result['cleanup'],'branch-used-by-another-open-pr')
        self.assertEqual(deletes,[])

    def test_changed_head_skips_close_and_delete(self):
        result,calls,deletes=self.run_cleanup(sha='b'*40)
        self.assertFalse(result['closed'])
        self.assertFalse(any(method=='PATCH' for _,_,method in calls))
        self.assertEqual(deletes,[])

    def test_uncertainty_and_outages_never_close(self):
        for verdict in ['uncertain','unavailable','clean']:
            result,calls,deletes=self.run_cleanup(verdict=verdict)
            self.assertFalse(result['closed'])
            self.assertEqual(deletes,[])

    def test_delete_ref_is_atomically_guarded_by_expected_sha(self):
        fresh={'state':'open','head':{'sha':'a'*40,'ref':'sop/test','repo':{'full_name':'owner/repo'}},'base':{'ref':'main'}}
        def api(path,data=None,method=None):
            return fresh if path=='pulls/2' and data is None else []
        with patch.object(security.subprocess,'run',return_value=subprocess.CompletedProcess([],1)) as run:
            result=security.close_and_cleanup(api,'owner/repo','DO_NOT_PRINT_TOKEN',2,'a'*40,{'verdict':'prohibited','findings':[{'category':'private'}]})
        cmd=run.call_args.args[0]
        self.assertIn('--force-with-lease=refs/heads/sop/test:'+'a'*40,cmd)
        self.assertNotIn('DO_NOT_PRINT_TOKEN',' '.join(cmd))
        self.assertEqual(result['cleanup'],'branch-changed-or-delete-denied')

class HistoryAndReportingTests(unittest.TestCase):
    def test_private_data_in_an_intermediate_commit_is_detected(self):
        def git(repo,*args):
            return b'c1\n' if args[0]=='rev-list' else b'sops/x/y/sop.json\0'
        with patch.object(security,'git',side_effect=git), patch.object(security,'blob',return_value=b'{"scope":"private"}'):
            found=security.historical_findings(Path('.'),'base','head')
        self.assertEqual(found[0]['category'],'private')
        self.assertEqual(found[0]['commit'],'c1')

    def test_security_rejection_report_never_echoes_sensitive_failures(self):
        result={'ok':False,'auto_merge':False,'head':'a'*40,'human':[],'sops':[], 'fail':['DO_NOT_ECHO_SENSITIVE_VALUE'],
                'security':{'verdict':'prohibited','findings':[{'category':'credentials','path':'DO_NOT_ECHO_SENSITIVE_VALUE'}]},
                'remediation':{'closed':True,'cleanup':'proposal-branch-deleted'}}
        rendered=review.markdown(result)
        self.assertNotIn('DO_NOT_ECHO_SENSITIVE_VALUE',rendered)
        self.assertIn('proposal-branch-deleted',rendered)

class StaticHardeningTests(unittest.TestCase):
    def test_aliased_sys_process_access_is_held(self):
        code="import json\nimport sys as runtime\njson.dump({'value':runtime.modules},runtime.stdout)"
        fail,human=review.source_checks(code,{'compute'})
        self.assertIn('introspection or access to process internals',human)

    def test_indirect_dynamic_builtin_is_held(self):
        fail,human=review.source_checks('import json,sys\nf = eval\njson.dump(f("1"),sys.stdout)',{'compute'})
        self.assertTrue(human)

    def test_pathlib_read_requires_declared_permission(self):
        fail,human=review.source_checks('import json,sys\nfrom pathlib import Path\njson.dump({"text":Path("input.txt").read_text()},sys.stdout)',{'compute'})
        self.assertTrue(any('fs:read' in f for f in fail))

    def test_sys_stdin_stdout_remain_supported(self):
        fail,human=review.source_checks('import json\nimport sys as runtime\na=json.load(runtime.stdin)\njson.dump(a,runtime.stdout)',{'compute'})
        self.assertEqual(fail,[])
        self.assertEqual(human,[])

if __name__=='__main__':
    unittest.main()
