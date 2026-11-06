
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
django.setup()

from system.models import UserProfile

print("Checking tenant document references...")
print("")

for profile in UserProfile.objects.all().iterator():
    user_id = getattr(profile, "user_id", None)

    valid_id = getattr(profile, "valid_id", None)
    selfie = getattr(profile, "selfie", None)

    print(f"User ID: {user_id}")
    print("  Valid ID reference:", str(valid_id) if valid_id else "EMPTY")
    print("  Selfie reference:", str(selfie) if selfie else "EMPTY")

print("")
print("READ-ONLY CHECK COMPLETE")
