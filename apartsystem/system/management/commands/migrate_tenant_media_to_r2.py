import hashlib
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from django.conf import settings
from django.core.files import File
from django.core.files.storage import FileSystemStorage, default_storage
from django.core.management.base import BaseCommand, CommandError

from system.models import UserProfile


CHUNK_SIZE = 1024 * 1024
EXPECTED_BUCKET = 'apartment-tenant-media'


def _hash_file(file_obj):
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: file_obj.read(CHUNK_SIZE), b''):
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


def _normalize_profile_key(value):
    normalized = str(value or '').replace('\\', '/')
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or path.as_posix() != normalized
        or len(path.parts) != 2
        or path.parts[0] not in ('ids', 'selfies')
        or any(part in ('', '.', '..') for part in path.parts)
    ):
        return None
    return normalized


class Command(BaseCommand):
    help = 'Copy local tenant media to the private R2 bucket without changing profile records.'

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            '--dry-run',
            action='store_true',
            help='Inspect local files and existing R2 objects without uploading anything (default).',
        )
        mode.add_argument(
            '--execute',
            action='store_true',
            help='Copy files after a complete preflight; local originals are retained.',
        )

    def handle(self, *args, **options):
        self._require_r2_configuration()
        dry_run = not options['execute']

        local_storage = FileSystemStorage(location=settings.MEDIA_ROOT)
        root = Path(local_storage.location).resolve()
        references, invalid_references = self._profile_references()
        files, scan_failures = self._local_files(root)

        for key in sorted(invalid_references):
            self.stderr.write(self.style.ERROR(f'Invalid profile storage key: {key}'))
        for key in sorted(scan_failures):
            self.stderr.write(self.style.ERROR(f'Could not safely inspect local file: {key}'))

        local_keys = {key for key, _path in files}
        missing_local = sorted(references - local_keys)
        unreferenced = sorted(local_keys - references)
        for key in missing_local:
            self.stderr.write(self.style.ERROR(f'Missing local file referenced by a profile: {key}'))
        for key in unreferenced:
            self.stdout.write(f'Unreferenced local file (will be included): {key}')

        if invalid_references or scan_failures:
            raise CommandError('Migration stopped before contacting storage because local keys/files need review.')

        source_metadata = {}
        local_read_failures = []
        for key, path in files:
            try:
                with path.open('rb') as source:
                    source_metadata[key] = _hash_file(source)
            except OSError:
                local_read_failures.append(key)
                self.stderr.write(self.style.ERROR(f'Could not read local file: {key}'))

        if local_read_failures:
            raise CommandError('Migration stopped before contacting storage because local files could not be read.')

        plan = []
        conflicts = []
        remote_failures = []
        for key, _path in files:
            try:
                exists = default_storage.exists(key)
                if not exists:
                    plan.append((key, 'copy'))
                    continue

                with default_storage.open(key, 'rb') as existing:
                    remote_metadata = _hash_file(existing)
            except Exception:
                remote_failures.append(key)
                self.stderr.write(self.style.ERROR(f'Could not inspect R2 object: {key}'))
                continue

            if remote_metadata == source_metadata[key]:
                plan.append((key, 'identical'))
            else:
                conflicts.append(key)
                self.stderr.write(self.style.ERROR(f'Conflict: R2 object differs from local file: {key}'))

        copy_keys = [key for key, status in plan if status == 'copy']
        identical_keys = [key for key, status in plan if status == 'identical']
        self.stdout.write(f'Local files found: {len(files)}')
        self.stdout.write(f'Profile references missing locally: {len(missing_local)}')
        self.stdout.write(f'Unreferenced local files: {len(unreferenced)}')
        self.stdout.write(f'Already identical in R2: {len(identical_keys)}')
        self.stdout.write(f'Files to copy: {len(copy_keys)}')

        if missing_local or conflicts or remote_failures:
            raise CommandError('Migration stopped during preflight; no local files were uploaded.')

        if dry_run:
            for key in copy_keys:
                self.stdout.write(f'Would copy: {key}')
            self.stdout.write('Dry run complete; R2 and local files were not modified.')
            return

        copied = []
        for key in copy_keys:
            local_path = dict(files)[key]
            try:
                with local_path.open('rb') as source:
                    saved_key = default_storage.save(key, File(source, name=local_path.name))
                if saved_key != key:
                    self.stderr.write(self.style.ERROR(
                        f'Storage selected an unexpected key for {key}; returned key: {saved_key}'
                    ))
                    raise CommandError('Migration stopped because storage did not preserve the relative key.')

                with default_storage.open(key, 'rb') as uploaded:
                    uploaded_metadata = _hash_file(uploaded)
                if uploaded_metadata != source_metadata[key]:
                    self.stderr.write(self.style.ERROR(f'Uploaded content verification failed: {key}'))
                    raise CommandError('Migration stopped because a copied object did not match its local source.')
                copied.append(key)
            except CommandError:
                raise
            except Exception:
                self.stderr.write(self.style.ERROR(f'Copy or verification failed: {key}'))
                raise CommandError(
                    f'Migration stopped after {len(copied)} verified copies; local originals were retained.'
                ) from None

        self.stdout.write(self.style.SUCCESS(
            f'Migration complete: {len(copied)} copied, {len(identical_keys)} already identical. '
            'Local originals and database records were not changed.'
        ))

    @staticmethod
    def _require_r2_configuration():
        if not getattr(settings, 'USE_R2_STORAGE', False):
            raise CommandError('Migration refused: USE_R2_STORAGE is not true.')

        storage_config = settings.STORAGES.get('default', {})
        if storage_config.get('BACKEND') != 'storages.backends.s3.S3Storage':
            raise CommandError('Migration refused: default storage is not the R2 S3 backend.')

        config = storage_config.get('OPTIONS', {})
        required = ('access_key', 'secret_key', 'bucket_name', 'endpoint_url')
        missing = [name for name in required if not str(config.get(name) or '').strip()]
        if missing:
            raise CommandError('Migration refused: incomplete R2 configuration (' + ', '.join(missing) + ').')

        endpoint = urlparse(config['endpoint_url'])
        if endpoint.scheme != 'https' or not endpoint.hostname or not endpoint.hostname.endswith('.r2.cloudflarestorage.com'):
            raise CommandError('Migration refused: endpoint must be a Cloudflare R2 HTTPS endpoint.')
        if config['bucket_name'] != EXPECTED_BUCKET:
            raise CommandError('Migration refused: configured bucket is not the expected tenant media bucket.')
        if (
            config.get('region_name') != 'auto'
            or config.get('signature_version') != 's3v4'
            or config.get('addressing_style') != 'path'
            or config.get('default_acl') is not None
            or config.get('querystring_auth') is not True
        ):
            raise CommandError('Migration refused: R2 signing, private-object, or addressing settings are invalid.')
        try:
            expiration = int(config.get('querystring_expire', 0))
        except (TypeError, ValueError):
            expiration = 0
        if expiration <= 0:
            raise CommandError('Migration refused: signed URL expiration setting is invalid.')

    @staticmethod
    def _profile_references():
        references = set()
        invalid = set()
        fields = ('valid_id_file', 'selfie_verification_file')
        for field in fields:
            values = UserProfile.objects.exclude(**{field: ''}).exclude(
                **{f'{field}__isnull': True}
            ).values_list(field, flat=True)
            for value in values:
                key = _normalize_profile_key(value)
                if key:
                    expected_directory = 'ids' if field == 'valid_id_file' else 'selfies'
                    if PurePosixPath(key).parts[0] == expected_directory:
                        references.add(key)
                    else:
                        invalid.add(str(value))
                else:
                    invalid.add(str(value))
        return references, invalid

    @staticmethod
    def _local_files(root):
        files = []
        failures = []
        for directory in ('ids', 'selfies'):
            folder = root / directory
            if not folder.exists():
                continue
            if folder.is_symlink() or not folder.is_dir():
                failures.append(directory)
                continue
            for path in folder.rglob('*'):
                if path.is_dir() and not path.is_symlink():
                    continue
                relative = path.relative_to(root).as_posix()
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(root)
                    if path.is_symlink() or not resolved.is_file():
                        raise ValueError('not a regular local file')
                except (OSError, ValueError):
                    failures.append(relative)
                    continue
                files.append((relative, path))
        return sorted(files), sorted(failures)
