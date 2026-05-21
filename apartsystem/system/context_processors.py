from .models import SystemSettings


def admin_contact(request):
    settings = SystemSettings.get_settings()
    return {
        'admin_contact': {
            'name': settings.admin_name,
            'phone': settings.admin_phone,
            'email': settings.admin_email,
        }
    }
