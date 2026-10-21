import uuid
from urllib.parse import urlparse

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Upload, read, and remove a temporary object in the configured private R2 bucket.'

    def handle(self, *args, **options):
        if not getattr(settings, 'USE_R2_STORAGE', False):
            raise CommandError('R2 smoke test refused: USE_R2_STORAGE is not true.')

        storage_config = settings.STORAGES.get('default', {})
        if storage_config.get('BACKEND') != 'storages.backends.s3.S3Storage':
            raise CommandError('R2 smoke test refused: the default storage is not the R2 S3 backend.')

        config = storage_config.get('OPTIONS', {})
        required_options = ('access_key', 'secret_key', 'bucket_name', 'endpoint_url')
        missing_options = [name for name in required_options if not str(config.get(name) or '').strip()]
        if missing_options:
            raise CommandError(
                'R2 smoke test refused: incomplete R2 configuration ('
                + ', '.join(missing_options)
                + ').'
            )

        endpoint = urlparse(config['endpoint_url'])
        if endpoint.scheme != 'https' or not endpoint.hostname or not endpoint.hostname.endswith('.r2.cloudflarestorage.com'):
            raise CommandError('R2 smoke test refused: the configured R2 endpoint is not a Cloudflare HTTPS endpoint.')
        if config['bucket_name'] != 'apartment-tenant-media':
            raise CommandError('R2 smoke test refused: the configured bucket is not the expected private test bucket.')
        if (
            config.get('region_name') != 'auto'
            or config.get('signature_version') != 's3v4'
            or config.get('addressing_style') != 'path'
        ):
            raise CommandError('R2 smoke test refused: the required R2 signing or addressing options are not configured.')
        if config.get('default_acl') is not None or config.get('querystring_auth') is not True:
            raise CommandError('R2 smoke test refused: private-object and signed-URL settings are not configured.')
        try:
            signed_url_expire = int(config.get('querystring_expire', 0))
        except (TypeError, ValueError):
            signed_url_expire = 0
        if signed_url_expire <= 0:
            raise CommandError('R2 smoke test refused: the signed-URL expiration setting is invalid.')

        object_name = f'smoke-tests/{uuid.uuid4().hex}.txt'
        expected_content = f'Apartment energy monitoring R2 smoke test {uuid.uuid4().hex}\n'.encode('utf-8')

        try:
            saved_name = default_storage.save(object_name, ContentFile(expected_content))
        except Exception:
            raise CommandError('R2 smoke test failed while uploading its temporary object.') from None

        if saved_name != object_name:
            raise CommandError(
                'R2 smoke test stopped because storage changed the generated object key. '
                f'Temporary object may remain at: {saved_name}'
            )

        try:
            with default_storage.open(saved_name, 'rb') as stored_file:
                actual_content = stored_file.read()
        except Exception:
            raise CommandError(
                'R2 smoke test could not read back its temporary object. '
                f'Temporary object may remain at: {saved_name}'
            ) from None

        if actual_content != expected_content:
            raise CommandError(
                'R2 smoke test content verification failed. '
                f'Temporary object was left in place at: {saved_name}'
            )

        try:
            default_storage.delete(saved_name)
        except Exception:
            raise CommandError(
                'R2 smoke test verified the object but could not delete it. '
                f'Remove this exact temporary object manually: {saved_name}'
            ) from None

        self.stdout.write(self.style.SUCCESS('R2 smoke test passed: temporary object uploaded, verified, and deleted.'))
