from io import BytesIO, StringIO
import os
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from pathlib import Path

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from PIL import Image
from unittest.mock import Mock, patch

from .models import SystemSettings


class R2SmokeTestCommandTests(SimpleTestCase):
    def test_command_refuses_when_r2_is_disabled(self):
        with override_settings(USE_R2_STORAGE=False):
            with self.assertRaisesMessage(CommandError, 'USE_R2_STORAGE is not true'):
                call_command('r2_smoke_test')

    @override_settings(
        USE_R2_STORAGE=True,
        STORAGES={
            'default': {
                'BACKEND': 'storages.backends.s3.S3Storage',
                'OPTIONS': {
                    'access_key': 'test-access-key',
                    'secret_key': 'test-secret-key',
                    'bucket_name': 'apartment-tenant-media',
                    'endpoint_url': 'https://test-account.r2.cloudflarestorage.com',
                    'region_name': 'auto',
                    'signature_version': 's3v4',
                    'addressing_style': 'path',
                    'default_acl': None,
                    'querystring_auth': True,
                    'querystring_expire': 300,
                },
            },
        },
    )
    @patch('system.management.commands.r2_smoke_test.default_storage')
    def test_command_verifies_then_deletes_only_its_temporary_key(self, storage):
        expected = {}

        def save(name, content):
            expected['content'] = content.read()
            return name

        storage.save.side_effect = save
        storage.open.side_effect = lambda name, mode: BytesIO(expected['content'])

        call_command('r2_smoke_test', verbosity=0, stdout=StringIO())

        saved_key = storage.save.call_args.args[0]
        self.assertRegex(saved_key, r'^smoke-tests/[0-9a-f]{32}\.txt$')
        storage.open.assert_called_once_with(saved_key, 'rb')
        storage.delete.assert_called_once_with(saved_key)
        self.assertEqual([call[0] for call in storage.method_calls], ['save', 'open', 'delete'])

    @override_settings(
        USE_R2_STORAGE=True,
        STORAGES={
            'default': {
                'BACKEND': 'storages.backends.s3.S3Storage',
                'OPTIONS': {},
            },
        },
    )
    def test_command_refuses_incomplete_r2_storage_options(self):
        with self.assertRaisesMessage(CommandError, 'incomplete R2 configuration'):
            call_command('r2_smoke_test')


class TenantMediaMigrationCommandTests(SimpleTestCase):
    r2_storages = {
        'default': {
            'BACKEND': 'storages.backends.s3.S3Storage',
            'OPTIONS': {
                'access_key': 'test-only-access',
                'secret_key': 'test-only-secret',
                'bucket_name': 'apartment-tenant-media',
                'endpoint_url': 'https://test-account.r2.cloudflarestorage.com',
                'region_name': 'auto',
                'signature_version': 's3v4',
                'addressing_style': 'path',
                'default_acl': None,
                'querystring_auth': True,
                'querystring_expire': 300,
            },
        },
    }

    def run_migration(self, media_root, references, storage, *arguments):
        with override_settings(
            USE_R2_STORAGE=True,
            STORAGES=self.r2_storages,
            MEDIA_ROOT=str(media_root),
        ), patch(
            'system.management.commands.migrate_tenant_media_to_r2.Command._profile_references',
            return_value=(set(references), set()),
        ), patch(
            'system.management.commands.migrate_tenant_media_to_r2.default_storage', storage,
        ):
            output = StringIO()
            errors = StringIO()
            call_command('migrate_tenant_media_to_r2', *arguments, stdout=output, stderr=errors)
            return output.getvalue(), errors.getvalue()

    def test_dry_run_reports_files_without_writing_to_r2(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'ids').mkdir()
            (root / 'ids' / 'referenced.txt').write_bytes(b'private id placeholder')
            (root / 'selfies').mkdir()
            (root / 'selfies' / 'orphan.txt').write_bytes(b'private selfie placeholder')
            storage = Mock()
            storage.exists.return_value = False

            output, _errors = self.run_migration(
                root,
                {'ids/referenced.txt'},
                storage,
                '--dry-run',
            )

            self.assertIn('Files to copy: 2', output)
            self.assertIn('Unreferenced local file (will be included): selfies/orphan.txt', output)
            self.assertIn('Dry run complete', output)
            storage.save.assert_not_called()
            storage.delete.assert_not_called()

    def test_execute_copies_and_verifies_objects_without_removing_local_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'ids').mkdir()
            (root / 'ids' / 'valid-id.bin').write_bytes(b'valid id placeholder')
            (root / 'selfies').mkdir()
            (root / 'selfies' / 'selfie.bin').write_bytes(b'selfie placeholder')
            remote = {}
            storage = Mock()
            storage.exists.side_effect = lambda key: key in remote
            storage.open.side_effect = lambda key, mode: BytesIO(remote[key])

            def save(key, uploaded):
                remote[key] = uploaded.read()
                return key

            storage.save.side_effect = save
            self.run_migration(
                root,
                {'ids/valid-id.bin', 'selfies/selfie.bin'},
                storage,
                '--execute',
            )

            self.assertEqual(set(remote), {'ids/valid-id.bin', 'selfies/selfie.bin'})
            self.assertEqual((root / 'ids' / 'valid-id.bin').read_bytes(), b'valid id placeholder')
            self.assertEqual((root / 'selfies' / 'selfie.bin').read_bytes(), b'selfie placeholder')
            self.assertEqual(storage.save.call_count, 2)
            storage.delete.assert_not_called()

    def test_conflicting_remote_object_stops_before_any_upload(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'ids').mkdir()
            (root / 'ids' / 'conflict.bin').write_bytes(b'local content')
            storage = Mock()
            storage.exists.return_value = True
            storage.open.return_value = BytesIO(b'different remote content')

            with self.assertRaisesMessage(CommandError, 'stopped during preflight'):
                self.run_migration(
                    root,
                    {'ids/conflict.bin'},
                    storage,
                    '--execute',
                )

            storage.save.assert_not_called()

    def test_missing_referenced_file_stops_before_any_upload(self):
        with TemporaryDirectory() as directory:
            storage = Mock()
            with self.assertRaisesMessage(CommandError, 'stopped during preflight'):
                self.run_migration(
                    Path(directory),
                    {'ids/missing.bin'},
                    storage,
                    '--execute',
                )

            storage.save.assert_not_called()
            storage.exists.assert_not_called()


class RegistrationFrontendValidationTests(SimpleTestCase):
    def test_invalid_selection_blocks_next_and_valid_replacement_clears_error(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is unavailable for the registration JavaScript harness')

        template = Path(__file__).resolve().parents[1] / 'templates' / 'system' / 'login.html'
        html = template.read_text(encoding='utf-8')
        scripts = re.findall(r'<script>(.*?)</script>', html, re.DOTALL)
        self.assertTrue(scripts, 'Registration template must contain its validation script')

        harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {File} = require('node:buffer');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    const classes = new Set();
    elements.set(id, {
      id, value: '', files: [], dataset: {}, style: {}, textContent: '', listeners: {},
      classList: {
        add: (...names) => names.forEach(name => classes.add(name)),
        remove: (...names) => names.forEach(name => classes.delete(name)),
        contains: name => classes.has(name)
      },
      addEventListener: (name, callback) => { elements.get(id).listeners[name] = callback; },
      setCustomValidity: value => { elements.get(id).validationMessage = value; },
      focus: () => {}
    });
  }
  return elements.get(id);
}
const document = {
  getElementById: element,
  querySelector: selector => {
    const field = element(selector);
    field.value = 'filled';
    return field;
  }
};
element('reg_email').value = 'tenant@example.com';
element('reg_password').value = 'password123';
element('reg_password2').value = 'password123';
const window = { location: { search: '' } };
"""
        checks = r"""
(async () => {
context.changeStep(1);
context.changeStep(1);
assert.equal(vm.runInContext('currentStep', context), 3);
const id = element('valid_id');
const selfie = element('selfie');
id.files = [new File([new Uint8Array(10 * 1024 * 1024 + 1)], 'large.jpg', {type: 'image/jpeg'})];
id.listeners.change.call(id);
assert.match(id.dataset.validationError, /10 MiB/);
context.changeStep(1);
assert.equal(vm.runInContext('currentStep', context), 3);
id.files = [new File([new Uint8Array([0xff, 0xd8, 0xff, 0xe0])], 'valid.jpg', {type: 'image/jpeg'})];
id.listeners.change.call(id);
assert.equal(id.dataset.validationStatus, 'pending');
context.changeStep(1);
assert.equal(vm.runInContext('currentStep', context), 3);
await new Promise(resolve => setImmediate(resolve));
assert.equal(id.dataset.validationError, undefined);
id.files = [new File([new TextEncoder().encode('not a jpeg')], 'fake.jpg', {type: 'image/jpeg'})];
id.listeners.change.call(id);
await new Promise(resolve => setImmediate(resolve));
assert.match(id.dataset.validationError, /contents do not match/);
context.changeStep(1);
assert.equal(vm.runInContext('currentStep', context), 3);
id.files = [new File([new Uint8Array([0xff, 0xd8, 0xff, 0xe0])], 'valid.jpg', {type: 'image/jpeg'})];
id.listeners.change.call(id);
await new Promise(resolve => setImmediate(resolve));
selfie.files = [new File([new Uint8Array(10 * 1024 * 1024 + 1)], 'large-selfie.jpg', {type: 'image/jpeg'})];
selfie.listeners.change.call(selfie);
assert.match(selfie.dataset.validationError, /10 MiB/);
context.changeStep(1);
assert.equal(vm.runInContext('currentStep', context), 3);
selfie.files = [{name: 'selfie.pdf', size: 128, type: 'application/pdf'}];
selfie.listeners.change.call(selfie);
assert.match(selfie.dataset.validationError, /PDFs are not accepted/);
context.changeStep(1);
assert.equal(vm.runInContext('currentStep', context), 3);
selfie.files = [new File([new Uint8Array([0xff, 0xd8, 0xff, 0xe0])], 'selfie.jpg', {type: 'image/jpeg'})];
selfie.listeners.change.call(selfie);
await new Promise(resolve => setImmediate(resolve));
assert.equal(selfie.dataset.validationError, undefined);
context.changeStep(1);
assert.equal(vm.runInContext('currentStep', context), 4);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        script = harness + '\nconst context = {document, window, URLSearchParams};\nvm.createContext(context);\nvm.runInContext(' + repr(scripts[-1]) + ', context);\n' + checks
        result = subprocess.run([node, '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class TenantRegistrationUploadTests(TestCase):
    def setUp(self):
        self.media_dir = TemporaryDirectory()
        self.addCleanup(self.media_dir.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        SystemSettings.objects.create(id=1, system_name='Registration test')
        self.url = reverse('register_tenant')

    def jpeg_upload(self, name='proof.jpg', content_type='image/jpeg'):
        buffer = BytesIO()
        Image.new('RGB', (12, 12), color='blue').save(buffer, format='JPEG')
        return SimpleUploadedFile(name, buffer.getvalue(), content_type=content_type)

    def pdf_upload(self, name='proof.pdf', content_type='application/pdf'):
        return SimpleUploadedFile(name, b'%PDF-1.4\nvalid test PDF signature\n%%EOF', content_type=content_type)

    def registration_data(self, **files):
        data = {
            'email': 'tenant@example.com',
            'password': 'password123',
            'password2': 'password123',
            'first_name': 'Test',
            'middle_name': '',
            'last_name': 'Tenant',
            'phone_number': '09123456789',
            'emergency_person': 'Emergency Contact',
            'emergency_number': '09987654321',
            'occupants': '1',
            'employment_status': 'full_time',
            'agree_terms': 'on',
            'agree_privacy': 'on',
        }
        data.update(files)
        return data

    def assert_rejected_without_user(self, response, message):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, message)
        self.assertFalse(User.objects.filter(email='tenant@example.com').exists())
        self.assertEqual(
            [filename for _, _, filenames in os.walk(self.media_dir.name) for filename in filenames],
            [],
        )

    def test_missing_valid_id_is_rejected(self):
        response = self.client.post(self.url, self.registration_data(selfie=self.jpeg_upload('selfie.jpg')))
        self.assert_rejected_without_user(response, 'Valid ID upload is required')

    def test_missing_selfie_is_rejected(self):
        response = self.client.post(self.url, self.registration_data(valid_id=self.jpeg_upload('id.jpg')))
        self.assert_rejected_without_user(response, 'Selfie upload is required')

    def test_valid_images_within_limit_register(self):
        response = self.client.post(self.url, self.registration_data(
            valid_id=self.jpeg_upload('id.jpg'), selfie=self.jpeg_upload('selfie.jpg')
        ))
        self.assertRedirects(response, reverse('login_view'))
        user = User.objects.get(email='tenant@example.com')
        self.assertFalse(user.userprofile.is_approved)
        self.assertTrue(user.userprofile.valid_id_file)
        self.assertTrue(user.userprofile.selfie_verification_file)

    def test_file_larger_than_10_mib_is_rejected_before_user_creation(self):
        too_large = SimpleUploadedFile(
            'large.jpg', b'0' * (10 * 1024 * 1024 + 1), content_type='image/jpeg'
        )
        response = self.client.post(self.url, self.registration_data(
            valid_id=too_large, selfie=self.jpeg_upload('selfie.jpg')
        ))
        self.assert_rejected_without_user(response, 'Valid ID must be no larger than 10 MB')

    def test_oversized_selfie_is_rejected_before_user_creation(self):
        too_large = SimpleUploadedFile(
            'large-selfie.jpg', b'0' * (10 * 1024 * 1024 + 1), content_type='image/jpeg'
        )
        response = self.client.post(self.url, self.registration_data(
            valid_id=self.jpeg_upload('id.jpg'), selfie=too_large
        ))
        self.assert_rejected_without_user(response, 'Selfie must be no larger than 10 MB')

    def test_valid_pdf_is_accepted_as_valid_id(self):
        response = self.client.post(self.url, self.registration_data(
            valid_id=self.pdf_upload(), selfie=self.jpeg_upload('selfie.jpg')
        ))
        self.assertRedirects(response, reverse('login_view'))
        user = User.objects.get(email='tenant@example.com')
        self.assertTrue(user.userprofile.valid_id_file.endswith('.pdf'))

    def test_pdf_is_rejected_as_selfie(self):
        response = self.client.post(self.url, self.registration_data(
            valid_id=self.jpeg_upload('id.jpg'), selfie=self.pdf_upload('selfie.pdf')
        ))
        self.assert_rejected_without_user(response, 'Selfie must be')

    def test_misleading_image_extension_is_rejected(self):
        response = self.client.post(self.url, self.registration_data(
            valid_id=self.jpeg_upload('id.png'), selfie=self.jpeg_upload('selfie.jpg')
        ))
        self.assert_rejected_without_user(response, 'Valid ID extension does not match its image format')

    def test_misleading_image_content_type_is_rejected(self):
        response = self.client.post(self.url, self.registration_data(
            valid_id=self.jpeg_upload('id.jpg', content_type='image/png'),
            selfie=self.jpeg_upload('selfie.jpg'),
        ))
        self.assert_rejected_without_user(response, 'Valid ID content type does not match its image format')

    def test_owner_can_preview_pdf_through_private_storage_endpoint(self):
        tenant = User.objects.create_user(username='tenant-doc', email='doc@example.com')
        tenant_profile = tenant.userprofile
        tenant_profile.user_type = 'tenant'
        tenant_profile.valid_id_file = default_storage.save(
            'ids/tenant-doc_id.pdf', ContentFile(b'%PDF-1.4\npreview fixture\n%%EOF')
        )
        tenant_profile.save()
        owner = User.objects.create_user(username='owner-doc', email='owner@example.com')
        owner.userprofile.user_type = 'owner'
        owner.userprofile.save()
        self.client.force_login(owner)

        response = self.client.get(reverse('tenant_document_preview', kwargs={
            'tenant_id': tenant_profile.id, 'document_type': 'valid-id'
        }))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('inline', response['Content-Disposition'])
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response['Content-Security-Policy'], "frame-ancestors 'self'")
        self.assertEqual(b''.join(response.streaming_content), b'%PDF-1.4\npreview fixture\n%%EOF')

    def test_owner_can_preview_image_through_private_storage_endpoint(self):
        tenant = User.objects.create_user(username='tenant-image', email='image@example.com')
        tenant_profile = tenant.userprofile
        tenant_profile.user_type = 'tenant'
        tenant_profile.selfie_verification_file = default_storage.save(
            'selfies/tenant-image_selfie.jpg', ContentFile(self.jpeg_upload().read())
        )
        tenant_profile.save()
        owner = User.objects.create_user(username='owner-image', email='owner-image@example.com')
        owner.userprofile.user_type = 'owner'
        owner.userprofile.save()
        self.client.force_login(owner)

        response = self.client.get(reverse('tenant_document_preview', kwargs={
            'tenant_id': tenant_profile.id, 'document_type': 'selfie'
        }))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/jpeg')
        self.assertTrue(b''.join(response.streaming_content).startswith(b'\xff\xd8'))

    def test_non_owner_cannot_preview_tenant_document(self):
        tenant = User.objects.create_user(username='tenant-private', email='private@example.com')
        tenant_profile = tenant.userprofile
        tenant_profile.user_type = 'tenant'
        tenant_profile.valid_id_file = default_storage.save(
            'ids/tenant-private_id.jpg', ContentFile(self.jpeg_upload().read())
        )
        tenant_profile.save()
        self.client.force_login(tenant)

        response = self.client.get(reverse('tenant_document_preview', kwargs={
            'tenant_id': tenant_profile.id, 'document_type': 'valid-id'
        }))

        self.assertEqual(response.status_code, 404)

    def test_local_media_path_does_not_expose_verification_files(self):
        default_storage.save(
            'ids/private-id.jpg', ContentFile(self.jpeg_upload().read())
        )

        response = self.client.get('/media/ids/private-id.jpg')

        self.assertEqual(response.status_code, 404)

    def test_owner_document_details_api_returns_authorized_preview_url(self):
        tenant = User.objects.create_user(username='tenant-api', email='api@example.com')
        tenant_profile = tenant.userprofile
        tenant_profile.user_type = 'tenant'
        tenant_profile.valid_id_file = 'ids/tenant-api_id.pdf'
        tenant_profile.save()
        owner = User.objects.create_user(username='owner-api', email='owner-api@example.com')
        owner.userprofile.user_type = 'owner'
        owner.userprofile.save()
        self.client.force_login(owner)

        response = self.client.get(reverse('tenant_details_api', kwargs={'tenant_id': tenant_profile.id}))
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data['valid_id_preview_url'], reverse('tenant_document_preview', kwargs={
            'tenant_id': tenant_profile.id, 'document_type': 'valid-id'
        }))
        self.assertEqual(data['valid_id_preview_type'], 'pdf')
        self.assertNotIn('valid_id_file', data)
