from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.forms import PasswordResetForm
from django.core.exceptions import ImproperlyConfigured


class AppBaseURLPasswordResetForm(PasswordResetForm):
    """Build reset links from APP_BASE_URL instead of the request host."""

    def save(self, **kwargs):
        base_url = urlsplit((settings.APP_BASE_URL or '').strip())
        try:
            hostname = base_url.hostname
            port = base_url.port
        except ValueError:
            hostname = None
            port = None

        if (
            base_url.scheme not in ('http', 'https')
            or not hostname
            or base_url.username
            or base_url.password
        ):
            raise ImproperlyConfigured('APP_BASE_URL must be an absolute HTTP or HTTPS application URL.')
        if not settings.DEBUG and base_url.scheme != 'https':
            raise ImproperlyConfigured('APP_BASE_URL must use HTTPS when DEBUG is false.')
        if not settings.DEBUG and hostname.lower() in ('localhost', '127.0.0.1', '::1'):
            raise ImproperlyConfigured('APP_BASE_URL must use the deployed application host when DEBUG is false.')

        domain = f'[{hostname}]' if ':' in hostname else hostname
        if port:
            domain = f'{domain}:{port}'

        kwargs['domain_override'] = domain
        kwargs['use_https'] = base_url.scheme == 'https'
        return super().save(**kwargs)
