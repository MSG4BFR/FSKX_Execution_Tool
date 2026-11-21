"""Neutral export/fallback regressions; run with Python and Matplotlib installed."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
from engine import classify_outputs
from plot_preferences import preferred_plot_exports


class ExportPreferenceTests(unittest.TestCase):
    def run_model(self,visual):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=Path(temporary.name);work=root/'work';out=root/'out';work.mkdir()
        (work/'params.py').write_text('x=2\n')
        (work/'model.py').write_text('signal=2*x\n')
        (work/'visualization.py').write_text('import matplotlib.pyplot as plt\nfrom pathlib import Path\n'+visual)
        (work/'_run_plan.json').write_text(json.dumps({'visualization_script':'visualization.py'}))
        result=subprocess.run([sys.executable,str(ROOT/'app/run_python_model.py'),str(work),str(out)],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        status=json.loads((out/'status.json').read_text());self.assertTrue(status['ok'])
        self.assertEqual(json.loads((out/'results.json').read_text())['signal'],4)
        return out,status

    def test_model_export_is_the_only_preview_with_original_resolution(self):
        out,status=self.run_model('fig,ax=plt.subplots()\nax.plot([0,1],[0,signal])\nfig.savefig("plot.png",dpi=150,bbox_inches="tight")\nplt.close(fig)\n')
        self.assertEqual(status['plots'],['plot.png'])
        plots,files=classify_outputs(str(out));self.assertEqual(plots,['plot.png'])
        self.assertIn('automatic/plot_1.png',files)
        width=struct.unpack('!II',(out/'plot.png').read_bytes()[16:24])[0]
        backup_width=struct.unpack('!II',(out/'automatic/plot_1.png').read_bytes()[16:24])[0]
        self.assertGreater(width,backup_width)
        self.assertEqual(preferred_plot_exports(out),{'automatic/plot_1.png':'plot.png'})

    def test_no_export_retains_one_automatic_preview(self):
        out,status=self.run_model('plt.plot([0,1],[0,signal])\nplt.close()\n')
        self.assertEqual(status['plots'],['automatic/plot_1.png'])
        self.assertEqual(classify_outputs(str(out))[0],status['plots'])

    def test_one_export_does_not_hide_another_figure_or_reused_number(self):
        out,status=self.run_model('fig,ax=plt.subplots()\nax.plot([0,1],[0,signal])\nfig.savefig("own.png")\nplt.close(fig)\nother,axis=plt.subplots()\naxis.plot([0,1],[3,7])\nplt.close(other)\n')
        self.assertEqual(set(classify_outputs(str(out))[0]),{'own.png','automatic/plot_2.png'})

    def test_later_figure_change_retains_distinct_automatic_state(self):
        out,status=self.run_model('fig,ax=plt.subplots()\nax.plot([0,1],[0,signal])\nfig.savefig("own.png")\nax.set_title("Changed after export")\nplt.close(fig)\n')
        self.assertEqual(set(classify_outputs(str(out))[0]),{'own.png','automatic/plot_1.png'})

    def test_deleted_or_changed_export_cannot_suppress_capture(self):
        for action in ['Path("own.png").unlink()','Path("own.png").write_bytes(b"changed")']:
            with self.subTest(action=action):
                out,status=self.run_model('fig,ax=plt.subplots()\nax.plot([0,1],[0,signal])\nfig.savefig("own.png")\nplt.close(fig)\n'+action+'\n')
                self.assertIn('automatic/plot_1.png',classify_outputs(str(out))[0])
                self.assertEqual(preferred_plot_exports(out),{})

    def test_second_figure_overwriting_export_keeps_first_capture(self):
        out,status=self.run_model('fig,ax=plt.subplots()\nax.plot([0,1],[0,signal])\nfig.savefig("own.png")\nplt.close(fig)\nother,axis=plt.subplots()\naxis.plot([0,1],[7,9])\nother.savefig("own.png")\nplt.close(other)\n')
        self.assertEqual(set(classify_outputs(str(out))[0]),{'own.png','automatic/plot_1.png'})

    def test_model_filename_does_not_overwrite_capture(self):
        out,status=self.run_model('fig,ax=plt.subplots()\nax.plot([0,1],[0,signal])\nfig.savefig("plot_1.png",dpi=150)\nplt.close(fig)\n')
        self.assertEqual(classify_outputs(str(out))[0],['plot_1.png'])
        self.assertTrue((out/'automatic/plot_1.png').is_file())

    def test_keyword_path_and_inferred_extension(self):
        out,status=self.run_model('Path("nested").mkdir()\nfig,ax=plt.subplots()\nax.plot([0,1],[0,signal])\nfig.savefig(fname=Path("nested/own"))\nplt.close(fig)\n')
        self.assertEqual(classify_outputs(str(out))[0],['nested/own.png'])

    def test_failed_outside_or_stream_export_preserves_fallback(self):
        for action in ['try:\n fig.savefig("missing/own.png")\nexcept OSError:\n pass','fig.savefig("../outside.png")','import io\nfig.savefig(io.BytesIO(),format="png")']:
            with self.subTest(action=action):
                out,status=self.run_model('fig,ax=plt.subplots()\nax.plot([0,1],[0,signal])\n'+action+'\nplt.close(fig)\n')
                self.assertEqual(classify_outputs(str(out))[0],['automatic/plot_1.png'])

    def test_direct_result_export(self):
        out,status=self.run_model('import sys\nfig,ax=plt.subplots()\nax.plot([0,1],[0,signal])\nfig.savefig(str(Path(sys.argv[2])/"own.png"))\nplt.close(fig)\n')
        self.assertEqual(classify_outputs(str(out))[0],['own.png'])

    def test_unavailable_optional_backup_keeps_good_export(self):
        out,status=self.run_model('import sys\nfig,ax=plt.subplots()\nax.plot([0,1],[0,signal])\nfig.savefig("own.png")\nPath(sys.argv[2],"automatic").write_text("blocked")\nplt.close(fig)\n')
        self.assertEqual(classify_outputs(str(out))[0],['own.png'])
        self.assertTrue(any('backup unavailable' in w for w in status['warnings']))

    def test_lost_tracking_does_not_hide_capture(self):
        out,status=self.run_model('fig,ax=plt.subplots()\nax.plot([0,1],[0,signal])\nfig.savefig("own.png")\nfig.stale_callback=None\nplt.close(fig)\n')
        self.assertEqual(set(classify_outputs(str(out))[0]),{'own.png','automatic/plot_1.png'})

    def test_svg_pdf_and_intentional_multiple_exports(self):
        for extension in ['svg','pdf']:
            with self.subTest(extension=extension):
                out,status=self.run_model('fig,ax=plt.subplots()\nax.plot([0,1],[0,signal])\nfig.savefig("own.'+extension+'")\nplt.close(fig)\n')
                self.assertEqual(classify_outputs(str(out))[0],['own.'+extension])
        out,status=self.run_model('fig,ax=plt.subplots()\nax.plot([0,1],[0,signal])\nfig.savefig("own.png")\nfig.savefig("own.svg")\nplt.close(fig)\n')
        self.assertEqual(set(classify_outputs(str(out))[0]),{'own.png','own.svg'})

    def test_corrupted_receipt_and_changed_files_restore_capture(self):
        out,status=self.run_model('fig,ax=plt.subplots()\nax.plot([0,1],[0,signal])\nfig.savefig("own.png")\nplt.close(fig)\n')
        (out/'own.png').write_bytes(b'changed later')
        self.assertEqual(set(classify_outputs(str(out))[0]),{'own.png','automatic/plot_1.png'})
        (out/'plot-selection.json').write_text('{invalid JSON')
        self.assertEqual(preferred_plot_exports(out),{})

    def test_receipt_traversal_links_and_unproven_old_files_never_hide_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'automatic').mkdir()
            auto=root/'automatic/plot_1.png';auto.write_bytes(b'neutral automatic')
            own=root/'own.png';own.write_bytes(b'neutral export')
            self.assertEqual(preferred_plot_exports(root),{})

            pair={'automatic':'automatic/plot_1.png','export':'own.png','automatic_sha256':hashlib.sha256(auto.read_bytes()).hexdigest(),'export_sha256':hashlib.sha256(own.read_bytes()).hexdigest()}
            for name in ['../own.png','/own.png','sub\\own.png']:
                (root/'plot-selection.json').write_text(json.dumps({'schema_version':1,'preference':'model_export','pairs':[{**pair,'export':name}]}))
                self.assertEqual(preferred_plot_exports(root),{})
            own.unlink();own.symlink_to(auto)
            (root/'plot-selection.json').write_text(json.dumps({'schema_version':1,'preference':'model_export','pairs':[pair]}))
            self.assertEqual(preferred_plot_exports(root),{})


    def test_model_authored_receipt_filename_is_retained(self):
        out,status=self.run_model('Path("plot-selection.json").write_text("model-authored neutral metadata")\nfig,ax=plt.subplots()\nax.plot([0,1],[0,signal])\nfig.savefig("own.png")\nplt.close(fig)\n')
        self.assertEqual((out/'model-files/plot-selection.json').read_text(),'model-authored neutral metadata')
        self.assertEqual(classify_outputs(str(out))[0],['own.png'])


if __name__=='__main__':unittest.main(verbosity=2)
