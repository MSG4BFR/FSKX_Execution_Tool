"""Archive regression fixtures use only temporary neutral files, never real models."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import pipeline_store as store


class ArchiveBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.old=(store.PIPELINES_DIR,store.MODELS_DIR,store.RESULTS_ROOT)
        store.PIPELINES_DIR=str(self.root/'pipelines');store.MODELS_DIR=str(self.root/'models');store.RESULTS_ROOT=str(self.root/'results')
        Path(store.MODELS_DIR).mkdir()
        self.addCleanup(self.restore)

    def restore(self):store.PIPELINES_DIR,store.MODELS_DIR,store.RESULTS_ROOT=self.old

    def record(self,name):return store.save({'nodes':[{'id':'n1','fskx':name}]},'neutral fixture')

    def test_external_model_reference_is_rejected(self):
        sentinel=self.root/'outside.fskx';sentinel.write_bytes(b'neutral outside sentinel')
        record=self.record(str(sentinel))
        with self.assertRaises(ValueError):store.build_archive(record['id'])

    def test_non_archive_and_portable_traversal_references_are_rejected(self):
        for name in ('../outside.fskx','C:\\outside.fskx','/tmp/outside.fskx','nested/model.fskx','not-a-model.txt'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):store.build_archive(self.record(name)['id'])

    def test_external_result_reference_is_rejected(self):
        (Path(store.MODELS_DIR)/'a.fskx').write_bytes(b'neutral')
        record=store.save({'nodes':[{'id':'n1','fskx':'a.fskx'}]},'neutral',node_state={'n1':{'run_id':'../../outside'}})
        with self.assertRaises(ValueError):store.build_archive(record['id'],include_results=True)

    def test_model_symlink_cannot_export_external_file(self):
        sentinel=self.root/'outside.fskx';sentinel.write_bytes(b'neutral sentinel')
        (Path(store.MODELS_DIR)/'a.fskx').symlink_to(sentinel)
        with self.assertRaises(ValueError):store.build_archive(self.record('a.fskx')['id'])

    def test_link_entry_is_rejected_before_any_member_is_restored(self):
        path=self.root/'input.fskxp'
        with zipfile.ZipFile(path,'w') as archive:
            archive.writestr('pipeline.json',json.dumps({'pipeline':{'nodes':[]}}))
            entry=zipfile.ZipInfo('models/link.fskx');entry.external_attr=0o120777<<16
            archive.writestr(entry,'../outside')
        with self.assertRaises(ValueError):store.import_archive(path)
        self.assertEqual(list(Path(store.MODELS_DIR).iterdir()),[])

    def test_duplicate_entry_is_rejected(self):
        path=self.root/'input.fskxp'
        with zipfile.ZipFile(path,'w') as archive:
            archive.writestr('pipeline.json',json.dumps({'pipeline':{'nodes':[]}}))
            archive.writestr('models/a.fskx','first');archive.writestr('models/a.fskx','second')
        with self.assertRaises(ValueError):store.import_archive(path)

    def test_regular_archive_roundtrip_preserves_existing_local_model(self):
        model=Path(store.MODELS_DIR)/'a.fskx';model.write_bytes(b'neutral local model')
        path,_=store.build_archive(self.record('a.fskx')['id']);self.addCleanup(Path(path).unlink)
        record=store.import_archive(path)
        self.assertEqual(record['pipeline']['nodes'][0]['fskx'],'a.fskx')
        self.assertEqual(model.read_bytes(),b'neutral local model')


if __name__=='__main__':unittest.main(verbosity=2)
