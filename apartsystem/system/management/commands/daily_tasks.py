from django.core.management.base import BaseCommand
from django.utils import timezone
from system.views import (
    send_payment_reminders,
    run_smart_features_daily,
    generate_monthly_bills
)
from datetime import datetime

class Command(BaseCommand):
    help = 'Run daily automated tasks: reminders, smart features, bill generation'

    def add_arguments(self, parser):
        parser.add_argument(
            '--test',
            action='store_true',
            help='Run in test mode (no actual emails/saves)',
        )
        parser.add_argument(
            '--days',
            type=int,
            default=3,
            help='Days before due to send reminders (default: 3)',
        )

    def handle(self, *args, **options):
        test_mode = options['test']
        days_before = options['days']
        
        self.stdout.write(self.style.SUCCESS('=' * 60))
        self.stdout.write(self.style.SUCCESS('🚀 DAILY AUTOMATION TASKS'))
        if test_mode:
            self.stdout.write(self.style.WARNING('⚠️ TEST MODE - No actual emails/saves'))
        self.stdout.write(self.style.SUCCESS('=' * 60))
        
        today = timezone.now().date()
        self.stdout.write(f"📅 Today: {today}")
        
        # 1. Send payment reminders
        self.stdout.write("\n" + self.style.SUCCESS('📧 TASK 1: Payment Reminders'))
        # Send reminders for multiple days (1, 2, and 3 days before due)
        reminder_days = [1, 2, 3]
        total_sent = 0
        total_failed = 0
        total_skipped = 0

        for days in reminder_days:
            self.stdout.write(f"\n   → Checking {days} day(s) before due...")
            result = send_payment_reminders(
                days_before_due=days,
                test_mode=test_mode
            )
            total_sent += result['sent']
            total_failed += result['failed']
            total_skipped += result['skipped']

        reminder_result = {
           'sent': total_sent,
           'failed': total_failed,
           'skipped': total_skipped
        }
        
        # 2. Run smart features
        self.stdout.write("\n" + self.style.SUCCESS('🤖 TASK 2: Smart Features'))
        if test_mode:
            self.stdout.write(self.style.WARNING('   Skipping smart features in test mode'))
            smart_result = {'abnormal': 0, 'high_consumption': 0, 'late_payments': 0}
        else:
            smart_result = run_smart_features_daily()
        
        # 3. Check if end of month (for bill generation)
        self.stdout.write("\n" + self.style.SUCCESS('📊 TASK 3: Month-End Check'))
        today_dt = datetime.now()
        last_day = 28  # You might want to calculate this properly
        
        if today_dt.day >= last_day or test_mode:
            if test_mode:
                self.stdout.write(self.style.WARNING('   TEST: Would generate monthly bills'))
                self.stdout.write(f"   Month: {today_dt.strftime('%B %Y')}")
            else:
                self.stdout.write(f"   Generating bills for {today_dt.strftime('%B %Y')}...")
                # Uncomment kapag ready na
                # generate_monthly_bills(year=today_dt.year, month=today_dt.month)
        else:
            self.stdout.write(f"   Not end of month yet (Day {today_dt.day})")
        
        # Summary
        self.stdout.write("\n" + self.style.SUCCESS('=' * 60))
        self.stdout.write(self.style.SUCCESS('📊 DAILY SUMMARY'))
        self.stdout.write('=' * 60)
        self.stdout.write(f"📧 Reminders: Sent: {reminder_result['sent']}, Failed: {reminder_result['failed']}, Skipped: {reminder_result['skipped']}")
        self.stdout.write(f"🤖 Smart Features: Abnormal: {smart_result['abnormal']}, High: {smart_result['high_consumption']}, Late: {smart_result['late_payments']}")
        self.stdout.write('=' * 60)
        
        self.stdout.write(self.style.SUCCESS('✅ Daily tasks complete!'))