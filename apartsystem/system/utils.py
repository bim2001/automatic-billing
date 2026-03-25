# system/utils.py
from datetime import date, timedelta
import calendar
from django.utils import timezone
from django.core.mail import send_mail
from .models import Room, Billing, Alert

ELECTRICITY_RATE = 23

def generate_monthly_bills():
    today = date.today()
    current_month_str = today.strftime("%B %Y")
    last_day = calendar.monthrange(today.year, today.month)[1]

    for room in Room.objects.all():
        if not Billing.objects.filter(room=room, billing_month=current_month_str).exists():
            cost = room.usage * ELECTRICITY_RATE
            due_date = date(today.year, today.month, last_day)
            Billing.objects.create(
                room=room,
                billing_month=current_month_str,
                kwh=room.usage,
                cost=cost,
                due_date=due_date
            )

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