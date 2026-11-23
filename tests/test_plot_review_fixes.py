"""Review follow-ups for the closed-figure capture PR (neutral fixtures, Matplotlib required).

* `plt.close(fig=...)` is a normal call form and must keep working in model and visualization code.
* Symlinked work/output paths (macOS /var -> /private/var, symlinked mounts) must not switch
  the export preference off.
* The internal receipt is not offered as a download.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from engine import classify_outputs

HEADER = 'import matplotlib.pyplot as plt\n'


class PlotReviewFixTests(unittest.TestCase):
    def setUp(self):
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            self.skipTest('Matplotlib is required')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def run_wrapper(self, model, visualization, symlinked_paths=False):
        work, out = self.root / 'work', self.root / 'out'
        work.mkdir()
        (work / 'params.py').write_text('x = 2\n')
        (work / 'model.py').write_text(model)
        (work / 'visualization.py').write_text(HEADER + visualization)
        (work / '_run_plan.json').write_text(json.dumps({'visualization_script': 'visualization.py'}))
        work_arg, out_arg = work, out
        if symlinked_paths:
            if not hasattr(os, 'symlink'):
                self.skipTest('needs symbolic links')
            (self.root / 'link_work').symlink_to(work, target_is_directory=True)
            (self.root / 'link_out').symlink_to(self.root, target_is_directory=True)
            work_arg, out_arg = self.root / 'link_work', self.root / 'link_out' / 'out'
        process = subprocess.run([sys.executable, str(ROOT / 'app/run_python_model.py'),
                                  str(work_arg), str(out_arg)],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(process.returncode, 0, process.stderr)
        status = json.loads((out / 'status.json').read_text())
        self.assertTrue(status['ok'], status)
        return out, status

    # -- plt.close keyword forms ---------------------------------------------------------

    def test_keyword_close_in_the_model_script_does_not_fail_the_run(self):
        out, status = self.run_wrapper(
            HEADER + 'signal = 2 * x\nfig, ax = plt.subplots()\nax.plot([0, 1])\nplt.close(fig=fig)\n', 'pass\n')
        self.assertEqual(json.loads((out / 'results.json').read_text())['signal'], 4)
        self.assertEqual(status['plots'], ['automatic/plot_1.png'])   # closed, still captured

    def test_keyword_close_in_visualization_keeps_the_single_export_preview(self):
        out, status = self.run_wrapper(
            'signal = 2 * x\n',
            'fig, ax = plt.subplots()\nax.plot([0, 1])\nfig.savefig("plot.png")\nplt.close(fig=fig)\n')
        self.assertEqual(status['warnings'], [])
        self.assertEqual(status['plots'], ['plot.png'])
        plots, files = classify_outputs(str(out))
        self.assertEqual(plots, ['plot.png'])
        self.assertIn('automatic/plot_1.png', files)

    def test_keyword_close_all_and_positional_forms_still_work(self):
        out, status = self.run_wrapper(
            'signal = 2 * x\n',
            'for i in range(2):\n    plt.figure()\n    plt.plot([0, i])\nplt.close(fig="all")\n'
            'plt.figure()\nplt.close(1)\n')
        self.assertEqual(status['warnings'], [])
        self.assertEqual(len(status['plots']), 3)

    # -- symlinked paths -----------------------------------------------------------------

    def test_symlinked_work_and_output_paths_still_prefer_the_model_export(self):
        out, status = self.run_wrapper(
            'signal = 2 * x\n',
            'fig, ax = plt.subplots()\nax.plot([0, 1])\nfig.savefig("plot.png")\nplt.close(fig)\n',
            symlinked_paths=True)
        self.assertEqual(status['plots'], ['plot.png'])
        plots, files = classify_outputs(str(out))
        self.assertEqual(plots, ['plot.png'])
        self.assertIn('automatic/plot_1.png', files)

    # -- receipt is bookkeeping ----------------------------------------------------------

    def test_receipt_is_kept_on_disk_but_not_listed_as_a_download(self):
        out, _status = self.run_wrapper(
            'signal = 2 * x\n', 'fig, ax = plt.subplots()\nax.plot([0, 1])\nfig.savefig("plot.png")\n')
        self.assertTrue((out / 'plot-selection.json').is_file())
        _plots, files = classify_outputs(str(out))
        self.assertNotIn('plot-selection.json', files)
        self.assertIn('results.json', files)


if __name__ == '__main__':
    unittest.main(verbosity=2)
