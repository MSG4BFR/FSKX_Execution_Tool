"""Real Flask multipart requests against temporary neutral archive fixtures."""
import io
from html.parser import HTMLParser
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import server
from test_pipeline_import_conflicts import archive_bytes, RESULT


class ImportApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for module, name, value in [(server.engine, 'MODELS_DIR', str(self.root / 'models')),
                                    (server.engine, 'RESULTS_ROOT', str(self.root / 'results')),
                                    (server.pipeline_store, 'PIPELINES_DIR', str(self.root / 'pipelines'))]:
            previous = getattr(module, name)
            setattr(module, name, value)
            self.addCleanup(setattr, module, name, previous)
        self.client = server.app.test_client()

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return path

    def post(self, policy=None):
        data = {'file': (io.BytesIO(archive_bytes()), 'neutral.fskxp')}
        if policy is not None:
            data['conflict_policy'] = policy
        return self.client.post('/api/pipelines/import', data=data)

    def test_omitted_and_explicit_default_return_existing_success_shape(self):
        for policy in (None, 'keep_local'):
            with self.subTest(policy=policy):
                model = self.write('models/A.fskx', b'neutral local model')
                result = self.write(RESULT, b'{"signal":4}')
                response = self.post(policy)
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(set(response.get_json()), {'id', 'name'})
                self.assertEqual(model.read_bytes(), b'neutral local model')
                self.assertEqual(result.read_bytes(), b'{"signal":4}')
                self.assertTrue((self.root / 'models/B.fskx').is_file())

    def test_reject_conflict_returns_409_paths_and_no_partial_import(self):
        result = self.write(RESULT, b'{"signal":4}')
        response = self.post('reject')
        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertEqual(payload['conflicts'], [RESULT])
        self.assertIn(RESULT, payload['error'])
        self.assertNotIn(str(self.root), payload['error'])
        self.assertEqual(result.read_bytes(), b'{"signal":4}')
        self.assertFalse((self.root / 'models').exists())
        self.assertFalse((self.root / 'pipelines').exists())
        self.assertFalse((self.root / 'results/A/run-1/new.txt').exists())

    def test_reject_accepts_empty_destination_and_identical_reimport(self):
        first = self.post('reject')
        second = self.post('reject')
        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertNotEqual(first.get_json()['id'], second.get_json()['id'])
        self.assertEqual((self.root / RESULT).read_bytes(), b'{"signal":9}')

    def test_invalid_policy_returns_400_without_importing(self):
        for policy in ('', 'true', 'false', 'overwrite', 'Reject', ' reject '):
            with self.subTest(policy=policy):
                response = self.post(policy)
                self.assertEqual(response.status_code, 400)
                self.assertIn('conflict_policy', response.get_json()['error'])
                self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_file_retains_400_response(self):
        response = self.client.post('/api/pipelines/import', data={'conflict_policy': 'reject'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {'error': 'No file uploaded.'})

    def test_temporary_upload_is_cleaned_after_success_conflict_and_malformed_archive(self):
        real_mkstemp = tempfile.mkstemp
        for action in ('success', 'conflict', 'malformed'):
            with self.subTest(action=action):
                if action == 'conflict':
                    self.write(RESULT, b'{"signal":4}')
                paths = []
                def track(*args, **kwargs):
                    descriptor, path = real_mkstemp(*args, **kwargs)
                    paths.append(Path(path))
                    return descriptor, path
                with patch.object(server.tempfile, 'mkstemp', side_effect=track):
                    if action == 'malformed':
                        response = self.client.post('/api/pipelines/import', data={
                            'file': (io.BytesIO(b'not an archive'), 'bad.fskxp'),
                            'conflict_policy': 'reject'})
                    else:
                        response = self.post('reject')
                    self.assertEqual(response.status_code, {'success': 200, 'conflict': 409, 'malformed': 400}[action])
                self.assertEqual(len(paths), 1)
                self.assertTrue(all(not path.exists() for path in paths))

    def test_join_page_offers_an_unchecked_conflict_option(self):
        class Inputs(HTMLParser):
            def __init__(self):
                super().__init__()
                self.matches = []
            def handle_starttag(self, tag, attrs):
                values = dict(attrs)
                if tag == 'input' and values.get('id') == 'rejectImportConflicts':
                    self.matches.append(values)
        # Only the model listing is stubbed: render the real Jinja page and context.
        with patch.object(server.engine, 'list_models', return_value=[]):
            response = self.client.get('/join')
        self.assertEqual(response.status_code, 200)
        parser = Inputs()
        parser.feed(response.get_data(as_text=True))
        self.assertEqual(len(parser.matches), 1)
        self.assertEqual(parser.matches[0]['type'], 'checkbox')
        self.assertNotIn('checked', parser.matches[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
