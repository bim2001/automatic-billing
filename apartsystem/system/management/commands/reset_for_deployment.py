from django.core.management.base import BaseCommand
from django.db import transaction

from system.models import (
    ActivityLog,
    Alert,
    Billing,
    EnergyUsage,
    Payment,
    TenantAssignment,
    UserProfile,
)


class Command(BaseCommand):
    help = "Review and optionally clear test transactional data before production deployment."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without changing any data.",
        )

    def handle(self, *args, **options):
        tenant_profiles = list(
            UserProfile.objects.filter(
                user_type="tenant"
            ).select_related("user", "room")
        )

        counts = {
            "EnergyUsage": EnergyUsage.objects.count(),
            "Billing": Billing.objects.count(),
            "Payment": Payment.objects.count(),
            "Alert": Alert.objects.count(),
            "ActivityLog": ActivityLog.objects.count(),
            "TenantAssignment": TenantAssignment.objects.count(),
            "test tenant UserProfile": len(tenant_profiles),
            "test tenant User": len(tenant_profiles),
        }

        self.stdout.write("\nRecords that would be removed:")

        for model, count in counts.items():
            self.stdout.write(f"  {model}: {count}")

        self.stdout.write("\nTenant accounts that would be removed:")

        if tenant_profiles:
            for profile in tenant_profiles:
                self.stdout.write(
                    f"  {profile.user.username} "
                    f"(profile #{profile.pk}, "
                    f"room={profile.room.name if profile.room else 'None'})"
                )
        else:
            self.stdout.write("  No tenant accounts found.")

        self.stdout.write(
            "\nThe following are PRESERVED:"
        )
        self.stdout.write("  Owner accounts")
        self.stdout.write("  Rooms")
        self.stdout.write("  SystemSettings")
        self.stdout.write("  Django configuration")
        self.stdout.write("  PayMongo configuration")
        self.stdout.write("  API configuration")
        self.stdout.write("  Application code and migrations")

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    "\nDry run only; no records were changed."
                )
            )
            return

        self.stdout.write(
            self.style.WARNING(
                "\nWARNING: This will permanently remove all "
                "tenant test data listed above."
            )
        )

        confirmation = input(
            'Type "RESET PRODUCTION" to continue: '
        )

        if confirmation != "RESET PRODUCTION":
            self.stdout.write(
                self.style.WARNING(
                    "Confirmation did not match; no records were changed."
                )
            )
            return

        with transaction.atomic():
            # Remove transactional/test data first.
            EnergyUsage.objects.all().delete()
            Billing.objects.all().delete()
            Payment.objects.all().delete()
            Alert.objects.all().delete()
            ActivityLog.objects.all().delete()

            # Remove tenant-room assignment history.
            TenantAssignment.objects.all().delete()

            # Remove all tenant test accounts.
            for profile in tenant_profiles:
                profile.user.delete()

        self.stdout.write(
            self.style.SUCCESS(
                "\nProduction cleanup completed successfully."
            )
        )
        self.stdout.write(
            "All tenant test accounts and transactional test data were removed."
        )