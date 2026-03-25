# your_app/management/commands/send_payment_reminders.py
from django.core.management.base import BaseCommand
from django.core.mail import send_mail
from django.utils import timezone
from datetime import timedelta
from system.models import Billing, UserProfile, Alert

class Command(BaseCommand):
    help = 'Send payment reminders for bills due in X days'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=3, 
                          help='Days before due date to send reminder')

    def handle(self, *args, **options):
        days_before = options['days']
        today = timezone.now().date()
        reminder_date = today + timedelta(days=days_before)
        
        bills = Billing.objects.filter(
            due_date=reminder_date, 
            is_paid=False, 
            reminder_sent=False
        )
        
        self.stdout.write(f"📊 Found {bills.count()} unpaid bill(s) due on {reminder_date}")
        
        for bill in bills:
            try:
                tenant_profile = UserProfile.objects.get(
                    room=bill.room, 
                    user_type='tenant'
                )
                tenant = tenant_profile.user
                
                if not tenant.email:
                    self.stdout.write(f"⚠️ No email for {tenant.username}")
                    continue
                
                send_mail(
                    subject=f"🧾 Reminder: Bill due in {days_before} days",
                    message=f"Hi {tenant.username},\n\nYour bill for {bill.billing_month} is due on {bill.due_date}. Amount: ₱{bill.cost}",
                    from_email='Divinamolina2018@gmail.com',
                    recipient_list=[tenant.email],
                    fail_silently=False
                )
                
                bill.reminder_sent = True
                bill.save()
                
                Alert.objects.create(
                    room=bill.room,
                    alert_type='billing',
                    message=f"Reminder sent for {bill.billing_month}"
                )
                
                self.stdout.write(f"✅ Sent to {tenant.username}")
                
            except UserProfile.DoesNotExist:
                self.stdout.write(f"❌ No tenant for room {bill.room.name}")
            except Exception as e:
                self.stdout.write(f"❌ Error: {e}")
        
        self.stdout.write("✅ Done!")