"""Review follow-ups for the interchange snapshot PR (neutral fixtures, no real models).

* R: data-frame (OBJECT) outputs must survive on every R version, including 3.6.x.
* R: missing name/unit/model id must not leak `null` / `{}` into the bundle (the schema
  declares strings).
* Python: a runner folder without the helper module degrades to a warning, not a crash.

The R tests need a real interpreter:  FSKX_TEST_RSCRIPT=/path/to/Rscript  (jsonlite required).
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

APP = Path(__file__).resolve().parents[1] / 'app'
sys.path.insert(0, str(APP))
import interchange

R_MODEL = ("signal <- 2 * x\n"
           "tbl <- data.frame(a = 1:3, b = c('u', 'v', 'w'))\n"
           "label <- 'abc'\n")
SPECS = [
    {'id': 'signal', 'dataType': 'DOUBLE', 'classification': 'OUTPUT', 'name': 'Signal', 'unit': 'g'},
    {'id': 'tbl', 'dataType': 'OBJECT', 'classification': 'OUTPUT', 'name': None, 'unit': None},
    {'id': 'label', 'dataType': 'STRING', 'classification': 'OUTPUT', 'name': None, 'unit': None},
]


class TemporaryCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.work = self.root / 'work'
        self.out = self.root / 'out'
        self.work.mkdir()

    def write_plan(self, **plan):
        (self.work / '_run_plan.json').write_text(json.dumps(plan))


class RSerializationTests(TemporaryCase):
    def run_r(self, plan):
        rscript = os.environ.get('FSKX_TEST_RSCRIPT')
        if not rscript:
            self.skipTest('Set FSKX_TEST_RSCRIPT to run the real R regressions')
        self.write_plan(visualization_script='visualization.r', **plan)
        (self.work / 'params.R').write_text('x <- 2\n')
        (self.work / 'model.r').write_text(R_MODEL)
        (self.work / 'visualization.r').write_text('invisible(NULL)\n')
        process = subprocess.run([rscript, '--vanilla', str(APP / 'run_r_model.R'),
                                  str(self.work), str(self.out)],
                                 capture_output=True, text=True, timeout=120)
        self.assertEqual(process.returncode, 0, process.stderr)
        status = json.loads((self.out / 'status.json').read_text())
        bundle = json.loads((self.out / 'outputs.json').read_text())
        return status, bundle, {p['metadata']['id']: p for p in bundle['parameters']}

    def test_data_frame_output_is_serialized(self):
        status, _bundle, parameters = self.run_r({'serialize_params': SPECS, 'model_id': 'neutral'})
        self.assertEqual(sorted(parameters), ['label', 'signal', 'tbl'], status.get('warnings'))
        table = interchange.read_value(parameters['tbl']['data'], str(self.out))
        self.assertEqual(len(table), 3)
        self.assertFalse(status.get('warnings'), status.get('warnings'))

    def test_missing_name_unit_and_model_id_are_omitted_not_null(self):
        _status, bundle, parameters = self.run_r({'serialize_params': SPECS, 'model_id': None})
        self.assertEqual(parameters['signal']['metadata']['name'], 'Signal')
        self.assertEqual(parameters['signal']['metadata']['unit'], 'g')
        for pid in ('tbl', 'label'):
            self.assertNotIn('name', parameters[pid]['metadata'])
            self.assertNotIn('unit', parameters[pid]['metadata'])
        self.assertEqual(bundle['generatedBy']['modelId'], '')
        self.assertTrue(all(p['modelId'] == '' for p in bundle['parameters']))


class PythonMissingHelperTests(TemporaryCase):
    def test_missing_snapshot_module_is_a_warning_not_a_failed_run(self):
        runner = self.root / 'runner'
        runner.mkdir()
        for name in ('run_python_model.py', 'interchange.py'):    # interchange_snapshot.py absent
            shutil.copy(APP / name, runner / name)
        self.write_plan(visualization_script='visualization.py', model_id='neutral',
                        serialize_params=[SPECS[0]])
        (self.work / 'params.py').write_text('x = 2\n')
        (self.work / 'model.py').write_text('signal = 2 * x\n')
        (self.work / 'visualization.py').write_text('pass\n')
        process = subprocess.run([sys.executable, str(runner / 'run_python_model.py'),
                                  str(self.work), str(self.out)],
                                 capture_output=True, text=True, timeout=120)
        self.assertEqual(process.returncode, 0, process.stderr)
        status = json.loads((self.out / 'status.json').read_text())
        self.assertTrue(status['ok'])
        self.assertTrue(any('Interchange unavailable' in w for w in status['warnings']),
                        status['warnings'])
        self.assertEqual(json.loads((self.out / 'results.json').read_text())['signal'], 4)


if __name__ == '__main__':
    unittest.main(verbosity=2)
