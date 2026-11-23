"""Observable import-policy checks using only temporary neutral file contents."""
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

RECORD = {'name': 'Neutral import fixture',
          'pipeline': {'nodes': [{'id': 'n1', 'fskx': 'A.fskx'}], 'edges': []},
          'node_state': {'n1': {'run_id': 'run-1', 'ok': True}}}
RESULT = 'results/A/run-1/outputs.json'
ENTRIES = [('models/B.fskx', b'neutral new model'),
           ('models/A.fskx', b'neutral incoming model'),
           (RESULT, b'{"signal":9}'),
           ('results/A/run-1/new.txt', b'neutral new artifact')]


def archive_bytes(entries=ENTRIES):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('pipeline.json', json.dumps(RECORD))
        for name, contents in entries:
            archive.writestr(name, contents)
    return stream.getvalue()


class ImportConflictTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / 'target'
        self.target.mkdir()
        self.models = self.target / 'models'
        self.results = self.target / 'results'
        self.saved = self.target / 'pipelines'
        self.previous = store.PIPELINES_DIR
        store.PIPELINES_DIR = str(self.saved)
        self.addCleanup(setattr, store, 'PIPELINES_DIR', self.previous)

    def write(self, relative, contents):
        path = self.target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def snapshot(self):
        return {p.relative_to(self.target).as_posix():
                (p.read_bytes(), p.stat().st_mtime_ns) if p.is_file() else None
                for p in self.target.rglob('*')}

    def run_import(self, entries=ENTRIES, **kwargs):
        path = self.root / 'input.fskxp'
        path.write_bytes(archive_bytes(entries))
        return store.import_archive(path, str(self.models), str(self.results), **kwargs)

    def test_default_and_explicit_keep_local_preserve_different_files_and_restore_missing(self):
        for kwargs in ({}, {'conflict_policy': 'keep_local'}):
            with self.subTest(kwargs=kwargs):
                model = self.write('models/A.fskx', b'neutral local model')
                result = self.write(RESULT, b'{"signal":4}')
                stamps = (model.stat().st_mtime_ns, result.stat().st_mtime_ns)
                record = self.run_import(**kwargs)
                self.assertEqual(model.read_bytes(), b'neutral local model')
                self.assertEqual(result.read_bytes(), b'{"signal":4}')
                self.assertEqual(stamps, (model.stat().st_mtime_ns, result.stat().st_mtime_ns))
                self.assertEqual((self.models / 'B.fskx').read_bytes(), b'neutral new model')
                self.assertEqual((self.results / 'A/run-1/new.txt').read_bytes(), b'neutral new artifact')
                self.assertEqual(record['node_state'], RECORD['node_state'])
                self.assertIsNotNone(store.load(record['id']))

    def test_reject_imports_into_empty_destinations(self):
        record = self.run_import(conflict_policy='reject')
        for name, contents in ENTRIES:
            self.assertEqual((self.target / name).read_bytes(), contents)
        self.assertEqual(record['pipeline'], RECORD['pipeline'])

    def test_reject_accepts_identical_files_without_rewriting_or_reusing_pipeline_id(self):
        first = self.run_import(conflict_policy='reject')
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                  for folder in (self.models, self.results) for p in folder.rglob('*') if p.is_file()}
        # Timestamps are not archive identity; identical bytes must still be accepted.
        for path in before:
            os.utime(path, (1000000000, 1000000000))
        second = self.run_import(conflict_policy='reject')
        self.assertNotEqual(first['id'], second['id'])
        for path, (contents, _) in before.items():
            self.assertEqual(path.read_bytes(), contents)
            self.assertEqual(int(path.stat().st_mtime), 1000000000)

    def test_reject_model_conflict_prevents_earlier_new_files_and_pipeline_save(self):
        self.write('models/A.fskx', b'neutral local model')
        before = self.snapshot()
        with self.assertRaises(store.ArchiveConflictError) as caught:
            self.run_import(conflict_policy='reject')
        self.assertEqual(caught.exception.paths, ['models/A.fskx'])
        self.assertEqual(self.snapshot(), before)

    def test_reject_result_conflict_prevents_earlier_new_files_and_pipeline_save(self):
        self.write(RESULT, b'{"signal":4}')
        before = self.snapshot()
        with self.assertRaises(store.ArchiveConflictError) as caught:
            self.run_import(conflict_policy='reject')
        self.assertEqual(caught.exception.paths, [RESULT])
        self.assertEqual(self.snapshot(), before)

    def test_reject_reports_all_conflicts_without_absolute_host_paths(self):
        self.write('models/A.fskx', b'neutral local model')
        self.write(RESULT, b'{"signal":4}')
        with self.assertRaises(ValueError) as caught:
            self.run_import(conflict_policy='reject')
        self.assertEqual(caught.exception.paths, ['models/A.fskx', RESULT])
        self.assertNotIn(str(self.target), str(caught.exception))

    def test_reject_compares_equal_sizes_empty_files_and_late_chunk_differences(self):
        for local, incoming in [(b'AC', b'AB'), (b'', b'A'),
                                (b'x' * (512 * 1024) + b'1', b'x' * (512 * 1024) + b'2')]:
            with self.subTest(size=len(local)):
                self.write('models/A.fskx', local)
                before = self.snapshot()
                with self.assertRaises(store.ArchiveConflictError):
                    self.run_import([('models/A.fskx', incoming)], conflict_policy='reject')
                self.assertEqual(self.snapshot(), before)
        record = self.run_import([('models/A.fskx', local)], conflict_policy='reject')
        self.assertIsNotNone(store.load(record['id']))

    def test_reject_handles_directory_and_parent_file_collisions_without_writes(self):
        (self.models / 'A.fskx').mkdir(parents=True)
        before = self.snapshot()
        with self.assertRaises(store.ArchiveConflictError):
            self.run_import(conflict_policy='reject')
        self.assertEqual(self.snapshot(), before)
        (self.models / 'A.fskx').rmdir()
        self.write('results/A', b'neutral blocking parent')
        before = self.snapshot()
        with self.assertRaises(store.ArchiveConflictError):
            self.run_import(conflict_policy='reject')
        self.assertEqual(self.snapshot(), before)

    def test_invalid_policy_is_rejected_before_opening_archive_or_creating_directories(self):
        for value in (None, True, False, 1, '', 'overwrite', 'REJECT', [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'conflict_policy'):
                    store.import_archive(self.root / 'missing.fskxp', str(self.models),
                                         str(self.results), conflict_policy=value)
                self.assertEqual(self.snapshot(), {})

    def test_normalized_target_alias_preserves_default_but_is_rejected_in_strict_mode(self):
        entries = [(RESULT, b'first neutral file'),
                   ('results/A/run-1//outputs.json', b'second neutral file')]
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'same import target'):
            self.run_import(entries, conflict_policy='reject')
        self.assertEqual(self.snapshot(), before)
        self.run_import(entries)
        self.assertEqual((self.target / RESULT).read_bytes(), b'first neutral file')

    def test_both_modes_retain_link_and_duplicate_entry_rejection(self):
        for policy in ('keep_local', 'reject'):
            for kind in ('duplicate', 'link'):
                with self.subTest(policy=policy, kind=kind):
                    entries = [('models/B.fskx', b'neutral new model')]
                    if kind == 'duplicate':
                        entries += [('models/B.fskx', b'neutral duplicate')]
                    else:
                        link = zipfile.ZipInfo('models/link.fskx')
                        link.external_attr = 0o120777 << 16
                        entries += [(link, '../outside')]
                    before = self.snapshot()
                    with self.assertRaisesRegex(ValueError, 'duplicate/link'):
                        self.run_import(entries, conflict_policy=policy)
                    self.assertEqual(self.snapshot(), before)

    def test_reject_detects_aliases_through_existing_internal_directory_symlinks(self):
        folder = self.results / 'A/storage'
        folder.mkdir(parents=True)
        (self.results / 'A/alias').symlink_to(folder, target_is_directory=True)
        entries = [('results/A/storage/outputs.json', b'first neutral file'),
                   ('results/A/alias/outputs.json', b'second neutral file')]
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'same import target'):
            self.run_import(entries, conflict_policy='reject')
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
