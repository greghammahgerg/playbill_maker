import io
import os
import sys
import tempfile
import types
import unittest
from unittest import mock


# Provide lightweight stubs for optional Google Drive dependencies so the app
# can be imported in a test environment without credentials.
if 'googleapiclient.discovery' not in sys.modules:
    discovery_module = types.ModuleType('googleapiclient.discovery')
    discovery_module.build = lambda *args, **kwargs: None
    sys.modules['googleapiclient.discovery'] = discovery_module

if 'googleapiclient.http' not in sys.modules:
    http_module = types.ModuleType('googleapiclient.http')
    http_module.MediaIoBaseUpload = object
    sys.modules['googleapiclient.http'] = http_module

if 'google.oauth2' not in sys.modules:
    google_oauth2_module = types.ModuleType('google.oauth2')
    service_account_module = types.ModuleType('google.oauth2.service_account')
    service_account_module.Credentials = type('Credentials', (), {
        'from_service_account_file': staticmethod(lambda *args, **kwargs: None)
    })
    google_oauth2_module.service_account = service_account_module
    sys.modules['google.oauth2'] = google_oauth2_module
    sys.modules['google.oauth2.service_account'] = service_account_module

import playbill_maker


class SecurityRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

        self.original_data_file = playbill_maker.DATA_FILE
        self.original_season_file = playbill_maker.SEASON_FILE
        self.original_upload_folder = playbill_maker.UPLOAD_FOLDER
        self.original_admin_emails = playbill_maker.ADMIN_EMAILS

        playbill_maker.DATA_FILE = os.path.join(self.tempdir.name, 'submissions.json')
        playbill_maker.SEASON_FILE = os.path.join(self.tempdir.name, 'seasonal_program.json')
        playbill_maker.UPLOAD_FOLDER = os.path.join(self.tempdir.name, 'uploads')
        playbill_maker.ADMIN_EMAILS = {'admin@example.com'}
        os.makedirs(playbill_maker.UPLOAD_FOLDER, exist_ok=True)

        playbill_maker.app.config['UPLOAD_FOLDER'] = playbill_maker.UPLOAD_FOLDER
        playbill_maker.app.config['TESTING'] = True
        self.client = playbill_maker.app.test_client()

    def tearDown(self):
        playbill_maker.DATA_FILE = self.original_data_file
        playbill_maker.SEASON_FILE = self.original_season_file
        playbill_maker.UPLOAD_FOLDER = self.original_upload_folder
        playbill_maker.ADMIN_EMAILS = self.original_admin_emails
        playbill_maker.app.config['UPLOAD_FOLDER'] = self.original_upload_folder

    def test_honeypot_field_rejects_public_submission(self):
        valid_bio = ' '.join(['word'] * 60)
        valid_image = b'0' * (1024 * 1024 + 1)

        with self.client.session_transaction() as session:
            session['user_email'] = 'artist@example.com'
            session['email_verified'] = True

        with mock.patch('playbill_maker.handle_artist_submission') as handle_submission:
            response = self.client.post(
                '/form',
                data={
                    'prefix': '',
                    'first-name': 'Ada',
                    'middle-name': '',
                    'last-name': 'Lovelace',
                    'suffix': '',
                    'bio': valid_bio,
                    'headshot': (io.BytesIO(valid_image), 'avatar.jpg'),
                    'website': 'spam-bot',
                },
                content_type='multipart/form-data',
            )

        self.assertEqual(response.status_code, 400)
        handle_submission.assert_not_called()
        self.assertEqual(playbill_maker.load_submissions(), {})

    def test_form_requires_a_verified_email_session(self):
        response = self.client.get('/form')

        self.assertEqual(response.status_code, 302)
        self.assertIn('/form/login', response.headers['Location'])

    def test_verified_email_can_create_an_artist_session(self):
        with mock.patch(
            'playbill_maker.verify_firebase_id_token',
            return_value=({'uid': 'artist-id'}, 'artist@example.com'),
        ):
            response = self.client.post(
                '/auth/session',
                json={'idToken': 'verified-token', 'next': '/form'},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['redirect'], '/form')
        with self.client.session_transaction() as session:
            self.assertEqual(session['user_email'], 'artist@example.com')
            self.assertTrue(session['email_verified'])
            self.assertFalse(session['is_admin'])

    def test_only_allowlisted_email_can_create_an_admin_session(self):
        with mock.patch(
            'playbill_maker.verify_firebase_id_token',
            return_value=({'uid': 'artist-id'}, 'artist@example.com'),
        ):
            response = self.client.post(
                '/auth/session',
                json={'idToken': 'verified-token', 'next': '/admin'},
            )

        self.assertEqual(response.status_code, 403)

    def test_admin_post_without_csrf_token_is_rejected(self):
        with self.client.session_transaction() as session:
            session['is_admin'] = True
            session['csrf_token'] = 'valid-token'

        response = self.client.post('/admin', data={'submission_ids': ['sample-id']})

        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
