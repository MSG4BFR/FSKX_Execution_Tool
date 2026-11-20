"""Neutral pre-visualization interchange regressions for Python and actual R."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

APP=Path(__file__).resolve().parents[1]/'app'
sys.path.insert(0,str(APP))
import interchange


class SnapshotTests(unittest.TestCase):
    def run_model(self,language,mutate=True):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);work=root/'work';out=root/'out';work.mkdir()
        specs=[{'id':'signal','dataType':'DOUBLE','classification':'OUTPUT'},
               {'id':'series','dataType':'VECTOROFNUMBERS','classification':'OUTPUT'}]
        (work/'_run_plan.json').write_text(json.dumps({'visualization_script':'visualization.py' if language=='python' else 'visualization.r',
            'serialize_params':specs,'model_id':'neutral-snapshot-fixture'}))
        if language=='python':
            (work/'params.py').write_text('x=2\n')
            (work/'model.py').write_text('signal=2*x\nseries=list(range(1005))\n')
            (work/'visualization.py').write_text("signal=999\nseries[:]=[999]*1005\n" if mutate else 'pass\n')
            command=[sys.executable,str(APP/'run_python_model.py')]
        else:
            rscript=os.environ.get('FSKX_TEST_RSCRIPT')
            if not rscript:self.skipTest('Set FSKX_TEST_RSCRIPT to run the real R regressions')
            (work/'params.R').write_text('x <- 2\n')
            (work/'model.r').write_text('signal <- 2*x\nseries <- 0:1004\n')
            (work/'visualization.r').write_text('signal <- 999\nseries[] <- 999\n' if mutate else 'invisible(NULL)\n')
            command=[rscript,'--vanilla',str(APP/'run_r_model.R')]
        process=subprocess.run(command+[str(work),str(out)],capture_output=True,text=True,timeout=60)
        self.assertEqual(process.returncode,0,process.stderr)
        bundle=json.loads((out/'outputs.json').read_text());parameters={p['metadata']['id']:p for p in bundle['parameters']}
        self.assertEqual(parameters['signal']['data']['value'],4)
        self.assertEqual(list(interchange.read_value(parameters['series']['data'],str(out))),list(range(1005)))
        self.assertEqual(bundle['generatedBy']['modelId'],'neutral-snapshot-fixture')
        self.assertEqual(bundle['generatedBy']['runId'],'out')

    def test_python_mutated_scalar_and_sidecar_remain_original(self):self.run_model('python')
    def test_python_unchanged_values_remain_compatible(self):self.run_model('python',False)
    def test_r_mutated_scalar_and_sidecar_remain_original(self):self.run_model('r')
    def test_r_unchanged_values_remain_compatible(self):self.run_model('r',False)


if __name__=='__main__':unittest.main(verbosity=2)
