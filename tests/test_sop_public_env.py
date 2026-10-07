"""Public SOPs must not receive ambient secrets or a different import mode from CI."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from rameness.sops import Executor, SOP

class PublicEnvironmentTests(unittest.TestCase):
    def test_public_environment_and_python_mode(self):
        sop = SOP('data.example', Path('/tmp/example'), 'Example', scope='public')
        executor = Executor(SimpleNamespace(), [], cwd=Path('/tmp'))
        ambient={'PATH':'/usr/bin','LANG':'C.UTF-8','PROVIDER_API_KEY':'not-a-real-key','AWS_SECRET_ACCESS_KEY':'test-only','PYTHONPATH':'/tmp/untrusted','RAMENESS_SOP_DIR':'/tmp/wrong'}
        response=subprocess.CompletedProcess([],0,stdout='{}',stderr='')
        with patch.dict(os.environ,ambient,clear=True), patch('rameness.sops.subprocess.run',return_value=response) as run:
            self.assertEqual(executor._script(sop,{}),{})
        env=run.call_args.kwargs['env']
        self.assertEqual(set(env),{'PATH','LANG','RAMENESS_SOP_DIR'})
        self.assertEqual(env['RAMENESS_SOP_DIR'],str(sop.path))
        self.assertIn('-I',run.call_args.args[0])

    def test_private_environment_remains_available(self):
        sop=SOP('data.example',Path('/tmp/example'),'Example',scope='private')
        executor=Executor(SimpleNamespace(),[],cwd=Path('/tmp'))
        response=subprocess.CompletedProcess([],0,stdout='{}',stderr='')
        with patch.dict(os.environ,{'CUSTOM_PRIVATE_SETTING':'example'},clear=True),patch('rameness.sops.subprocess.run',return_value=response) as run:
            executor._script(sop,{})
        self.assertEqual(run.call_args.kwargs['env']['CUSTOM_PRIVATE_SETTING'],'example')
        self.assertNotIn('-I',run.call_args.args[0])

    def test_public_python_ignores_sibling_module_shadowing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'json.py').write_text('raise RuntimeError("sibling module must not load")\n')
            (root/'run.py').write_text('import json\nprint(json.dumps({"ok":True}))\n')
            sop=SOP('data.example',root,'Example',scope='public')
            executor=Executor(SimpleNamespace(),[],cwd=root)
            self.assertEqual(executor._script(sop,{}),{'ok':True})

    def test_public_remote_command_drops_ambient_environment(self):
        commands=[]
        def remote_run(cmd,*args,**kwargs):
            commands.append(cmd)
            return SimpleNamespace(code=0,out='{}',err='')
        env=SimpleNamespace(kind='remote',run=remote_run,put_dir=lambda *a:None)
        executor=Executor(SimpleNamespace(),[],cwd=Path('/tmp'),env=env)
        executor._remote_root='/tmp/sop staging'
        sop=SOP('data.example',Path('/tmp/example'),'Example',scope='public')
        self.assertEqual(executor._script_remote(sop,{}),{})
        self.assertTrue(commands[0].startswith('env -i '))
        self.assertIn('python3 -I -B',commands[0])
        self.assertIn("'/tmp/sop staging/",commands[0])

if __name__=='__main__':
    unittest.main()
