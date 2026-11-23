"""Neutral wrapper regressions; run: python tests/test_closed_python_figures.py."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

WRAPPER=Path(__file__).resolve().parents[1]/'app/run_python_model.py'


class FigureCaptureTests(unittest.TestCase):
    def run_model(self,visualization):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);work=root/'work';out=root/'out';work.mkdir()
        (work/'params.py').write_text('x=2\n',encoding='utf-8')
        (work/'model.py').write_text('signal=2*x\n',encoding='utf-8')
        (work/'visualization.py').write_text('import matplotlib.pyplot as plt\n'+visualization,encoding='utf-8')
        (work/'_run_plan.json').write_text(json.dumps({'visualization_script':'visualization.py'}))
        process=subprocess.run([sys.executable,str(WRAPPER),str(work),str(out)],capture_output=True,text=True)
        self.assertEqual(process.returncode,0,process.stderr)
        status=json.loads((out/'status.json').read_text())
        self.assertEqual(json.loads((out/'results.json').read_text())['signal'],4)
        return out,status

    def assert_png(self,path):
        raw=path.read_bytes();self.assertEqual(raw[:8],b'\x89PNG\r\n\x1a\n')
        width,height=struct.unpack('>II',raw[16:24]);self.assertGreater(width,50);self.assertGreater(height,50)

    def test_closed_figures_and_reused_numbers_retain_both_actual_renderings(self):
        out,status=self.run_model("""fig,ax=plt.subplots()
ax.plot([0,1,2],[0,signal,2*signal]);ax.set_xlabel('x');ax.set_ylabel('neutral signal')
fig.savefig('expected_first.png',dpi=120,bbox_inches='tight')
plt.close(fig)
fig,ax=plt.subplots();ax.plot([0,1],[2,4])
fig.savefig('expected_second.png',dpi=120,bbox_inches='tight')
plt.close('all')
""")
        self.assertEqual(len(status['plots']),2)
        self.assertEqual(set(status['plots']),{'expected_first.png','expected_second.png'})
        for capture,expected in zip(('automatic/plot_1.png','automatic/plot_2.png'),('expected_first.png','expected_second.png')):
            self.assert_png(out/capture);self.assert_png(out/expected)
            self.assertEqual(hashlib.sha256((out/capture).read_bytes()).digest(),hashlib.sha256((out/expected).read_bytes()).digest())

    def test_open_figure_still_captured_once(self):
        out,status=self.run_model("plt.plot([0,1],[0,signal])\n")
        self.assertEqual(len(status['plots']),1);self.assert_png(out/status['plots'][0])

    def test_current_number_and_label_close_forms(self):
        out,status=self.run_model("""plt.figure('first');plt.plot([0,1],[0,signal]);plt.close()
fig=plt.figure();plt.plot([0,1],[0,signal]);plt.close(fig.number)
plt.figure('third');plt.plot([0,1],[0,signal]);plt.close('third')
""")
        self.assertEqual(len(status['plots']),3)
        for name in status['plots']:self.assert_png(out/name)

    def test_explicit_image_with_capture_filename_is_preserved_separately(self):
        out,status=self.run_model("""fig,ax=plt.subplots();ax.plot([0,1],[0,signal]);plt.close(fig)
with open('plot_1.png','wb') as stream:stream.write(b'explicit neutral model file')
""")
        self.assert_png(out/'automatic/plot_1.png')
        self.assertEqual((out/'plot_1.png').read_bytes(),b'explicit neutral model file')
        self.assertIn('plot_1.png',status['files'])

    def test_partial_figure_survives_visualization_error(self):
        out,status=self.run_model("plt.plot([0,1],[0,signal]);plt.close();raise ValueError('neutral failure')\n")
        self.assertEqual(len(status['plots']),1);self.assertTrue(status['warnings'])
        self.assert_png(out/status['plots'][0])


if __name__=='__main__':unittest.main(verbosity=2)
