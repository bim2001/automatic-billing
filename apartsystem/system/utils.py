# system/utils.py
from datetime import date, timedelta
import calendar
from django.utils import timezone
from django.core.mail import send_mail
from .models import Room, Billing, Alert

ELECTRICITY_RATE = 23

def generate_monthly_bills():
    """Compatibility wrapper for the shared tenant-cycle bill generator."""
    from .views import generate_monthly_bills as generate_cycle_bills
    return generate_cycle_bills()

def send_payment_reminders():
    today = timezone.now().date()
    reminder_date = today + timedelta(days=3)

    bills = Billing.objects.filter(due_date=reminder_date, is_paid=False)

    for bill in bills:
        tenant = bill.room.userprofile.user
        tenant_email = tenant.email

        send_mail(
            'Electricity Bill Reminder',
            f'Your electricity bill for {bill.billing_month} is due in 3 days.\nTotal: ₱{bill.cost}',
            'system@email.com',
            [tenant_email],
        )

        Alert.objects.create(
            room=bill.room,
            alert_type='billing',
            message=f"Reminder: Your electricity bill for {bill.billing_month} is due in 3 days."
        )