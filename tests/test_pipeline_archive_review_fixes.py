"""Review follow-ups for the pipeline archive PR (neutral temporary fixtures only).

1. A failed import must not leave a half-restored destination, in either conflict policy.
2. Export and import share their limits: whatever export writes can be imported again,
   and an archive over the limits fails at export with a clear message.
3. A failed export leaves no temporary file behind and the endpoint answers 400, not 500.
"""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import pipeline_store as store

PIPELINE = {'nodes': [{'id': 'n1', 'fskx': 'A.fskx'}], 'edges': []}
STATE = {'n1': {'run_id': 'run-1', 'ok': True}}


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.models = self.root / 'models'
        self.results = self.root / 'results'
        self.scratch = self.root / 'scratch'   # isolated tempdir: lets tests spot leaked files
        self.scratch.mkdir()
        self.models.mkdir()
        self.patch(store, 'PIPELINES_DIR', str(self.root / 'pipelines'))
        self.patch(tempfile, 'tempdir', str(self.scratch))

    def patch(self, module, name, value):
        previous = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, previous)

    def saved_pipeline(self):
        (self.models / 'A.fskx').write_bytes(b'neutral model')
        return store.save(PIPELINE, 'neutral', node_state=STATE)

    def add_results(self, count, size=3):
        run = self.results / 'A' / 'run-1'
        run.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            (run / 'f{}.txt'.format(i)).write_bytes(b'x' * size)

    def export(self, **kwargs):
        record = self.saved_pipeline()
        return store.build_archive(record['id'], str(self.models), include_results=True,
                                   results_root=str(self.results), **kwargs)

    def leftover_temp_files(self):
        return sorted(p.name for p in self.scratch.iterdir())

    # -- 1. import is all-or-nothing in the default policy too --------------------------

    @unittest.skipUnless(hasattr(os, 'symlink'), 'needs symbolic links')
    def test_default_import_failure_midway_restores_nothing(self):
        outside = self.root / 'outside.fskx'
        outside.write_bytes(b'neutral local file')
        os.symlink(outside, self.models / 'A.fskx')       # existing destination is a link
        archive = self.root / 'in.fskxp'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('pipeline.json', json.dumps({'name': 'n', 'pipeline': PIPELINE}))
            z.writestr('models/B.fskx', b'neutral new model')   # would be restored first
            z.writestr('models/A.fskx', b'neutral incoming model')
        for policy in ('keep_local', 'reject'):
            with self.subTest(policy=policy):
                with self.assertRaises(ValueError):
                    store.import_archive(archive, str(self.models), str(self.results),
                                         conflict_policy=policy)
                self.assertEqual(sorted(p.name for p in self.models.iterdir()), ['A.fskx'])
                self.assertFalse(os.path.exists(store.PIPELINES_DIR)
                                 and os.listdir(store.PIPELINES_DIR))

    # -- 2. export and import limits match -----------------------------------------------

    def test_export_over_entry_limit_fails_clearly_without_leaking_a_file(self):
        self.patch(store, 'MAX_ARCHIVE_ENTRIES', 10)
        self.add_results(20)
        with self.assertRaisesRegex(ValueError, 'size limits'):
            self.export()
        self.assertEqual(self.leftover_temp_files(), [])

    def test_export_over_file_size_limit_fails_clearly(self):
        self.patch(store, 'MAX_ARCHIVE_ENTRY_BYTES', 100)
        self.add_results(1, size=200)
        with self.assertRaisesRegex(ValueError, 'size limits'):
            self.export()
        self.assertEqual(self.leftover_temp_files(), [])

    def test_export_over_total_size_limit_fails_clearly(self):
        self.patch(store, 'MAX_ARCHIVE_TOTAL_BYTES', store._GENERATED_BYTES + 2000)
        self.add_results(30, size=100)
        with self.assertRaisesRegex(ValueError, 'size limits'):
            self.export()
        self.assertEqual(self.leftover_temp_files(), [])

    def test_archive_at_the_entry_limit_exports_and_imports_again(self):
        # entries = pipeline.json + 1 model + N results + 3 generated documents
        self.patch(store, 'MAX_ARCHIVE_ENTRIES', 12)
        self.add_results(7)
        path, _ = self.export()
        self.addCleanup(os.remove, path)
        self.assertEqual(len(zipfile.ZipFile(path).namelist()), 12)
        record = store.import_archive(path, str(self.root / 'models2'), str(self.root / 'results2'))
        self.assertIsNotNone(store.load(record['id']))

    def test_one_entry_more_than_the_limit_is_refused_at_export(self):
        self.patch(store, 'MAX_ARCHIVE_ENTRIES', 12)
        self.add_results(8)
        with self.assertRaisesRegex(ValueError, 'size limits'):
            self.export()

    # -- 3. failed export: no leaked temp file, endpoint answers 400 ---------------------

    def test_invalid_model_reference_leaves_no_temporary_file(self):
        record = store.save({'nodes': [{'id': 'n1', 'fskx': '../outside.fskx'}]}, 'bad')
        with self.assertRaises(ValueError):
            store.build_archive(record['id'], str(self.models))
        self.assertEqual(self.leftover_temp_files(), [])

    def test_containment_failure_midway_leaves_no_temporary_file(self):
        outside = self.root / 'outside.fskx'
        outside.write_bytes(b'neutral outside file')
        if not hasattr(os, 'symlink'):
            self.skipTest('needs symbolic links')
        os.symlink(outside, self.models / 'A.fskx')
        record = store.save(PIPELINE, 'linked')
        with self.assertRaises(ValueError):
            store.build_archive(record['id'], str(self.models))
        self.assertEqual(self.leftover_temp_files(), [])

    def test_export_endpoint_returns_json_400_for_an_invalid_reference(self):
        try:
            import server
        except ImportError as exc:                       # Flask etc. not installed
            self.skipTest('server dependencies missing: {}'.format(exc))
        self.patch(server.engine, 'MODELS_DIR', str(self.models))
        self.patch(server.engine, 'RESULTS_ROOT', str(self.results))
        self.patch(server.pipeline_store, 'PIPELINES_DIR', store.PIPELINES_DIR)
        record = store.save({'nodes': [{'id': 'n1', 'fskx': 'sub/A.fskx'}]}, 'bad')
        response = server.app.test_client().get('/api/pipelines/{}/export'.format(record['id']))
        self.assertEqual(response.status_code, 400)
        self.assertIn('Could not export', response.get_json()['error'])
        self.assertEqual(self.leftover_temp_files(), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
