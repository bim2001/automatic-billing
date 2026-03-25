from django.core.management.base import BaseCommand
from system.views import run_smart_features_daily

class Command(BaseCommand):
    help = 'Run smart features: abnormal detection, high consumption alerts, late payment penalties'

    def add_arguments(self, parser):
        parser.add_argument(
            '--test',
            action='store_true',
            help='Run in test mode (print only, no alerts)',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('🚀 Starting smart features...'))
        
        if options['test']:
            self.stdout.write(self.style.WARNING('TEST MODE - No alerts will be created'))
            # You can add test mode logic here
        else:
            results = run_smart_features_daily()
            
            self.stdout.write(self.style.SUCCESS(
                f"\n✅ Complete! Created:"
                f"\n   • {results['abnormal']} abnormal usage alerts"
                f"\n   • {results['high_consumption']} high consumption alerts"
                f"\n   • {results['late_payments']} late payment alerts"
            ))