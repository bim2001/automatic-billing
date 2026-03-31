import logging
from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from datetime import date

logger = logging.getLogger(__name__)

class Room(models.Model):
    name = models.CharField(max_length=50)
    usage = models.FloatField(default=0)
    limit = models.FloatField(default=50)
    power_status = models.BooleanField(default=True)
    
    def __str__(self):
        tenant_name = self.get_tenant_name()
        if tenant_name:
            return f"{self.name} - {tenant_name}"
        return self.name
    
    def get_tenant_name(self):
        """Get the name of the tenant assigned to this room"""
        try:
            profile = UserProfile.objects.get(room=self, user_type='tenant')
            return profile.user.get_full_name() or profile.user.username
        except UserProfile.DoesNotExist:
            return None
    
    def is_occupied(self):
        """Check if room has a tenant assigned"""
        return UserProfile.objects.filter(room=self, user_type='tenant').exists()
    
    class Meta:
        ordering = ['name']


class UserProfile(models.Model):
    USER_TYPES = [
        ('owner', 'Owner'),
        ('tenant', 'Tenant')
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    user_type = models.CharField(max_length=20, choices=USER_TYPES, default='tenant')
    room = models.ForeignKey(Room, on_delete=models.SET_NULL, null=True, blank=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True) 
   
    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} - {self.user_type}"


# ============ TENANT ASSIGNMENT (UNAHIN BAGO BILLING) ============
class TenantAssignment(models.Model):
    """Tracks tenant move-in and move-out dates for prorated billing"""
    tenant = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name='assignments')
    room = models.ForeignKey(Room, on_delete=models.CASCADE)
    move_in_date = models.DateField()
    move_out_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        status = "Active" if self.is_active else "Inactive"
        return f"{self.tenant.user.username} - {self.room.name} ({status})"
    
    def days_occupied_in_month(self, year, month):
        """Calculate how many days tenant occupied the room in a given month"""
        from datetime import date, timedelta
        
        month_start = date(year, month, 1)
        if month == 12:
            month_end = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(year, month + 1, 1) - timedelta(days=1)
        
        # Tenant's stay during this month
        stay_start = max(self.move_in_date, month_start)
        stay_end = self.move_out_date if self.move_out_date else month_end
        stay_end = min(stay_end, month_end)
        
        if stay_start > stay_end:
            return 0
        
        return (stay_end - stay_start).days + 1
    
    class Meta:
        ordering = ['-move_in_date']


# ============ BILLING (SUMUNOD) ============
class Billing(models.Model):
    room = models.ForeignKey(Room, on_delete=models.CASCADE)
    billing_month = models.CharField(max_length=50)
    kwh = models.FloatField(default=0)
    cost = models.FloatField(default=0)
    is_paid = models.BooleanField(default=False)
    due_date = models.DateField(default=date.today) 
    reminder_sent = models.BooleanField(default=False)  
    created_at = models.DateTimeField(auto_now_add=True)
    tenant_assignment = models.ForeignKey(
        TenantAssignment, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        help_text="Which tenant assignment this bill is for (for prorated bills)"
    )
    days_occupied = models.IntegerField(default=0, help_text="Number of days tenant occupied the room this month")
    
    def __str__(self):
        return f"{self.room.name} - {self.billing_month}"
    
    class Meta:
        ordering = ['-billing_month', 'room__name']


class EnergyUsage(models.Model):
    room = models.ForeignKey(Room, on_delete=models.CASCADE)
    kwh = models.FloatField(help_text="Kilowatt-hours consumed")
    timestamp = models.DateTimeField(auto_now_add=True)
    date = models.DateField(auto_now_add=True)

    def __str__(self):
        return f"{self.room.name} - {self.kwh}kWh - {self.timestamp}"

    class Meta:
        ordering = ['-timestamp']


class Alert(models.Model):
    ALERT_TYPES = [
        ('over_limit', 'Usage Over Limit'),
        ('power_off', 'Power Auto-OFF'),
        ('power_on', 'Power Restored'),
        ('billing', 'Billing Alert'),
        ('tenant_assigned', 'Tenant Assigned'),
        ('tenant_removed', 'Tenant Removed'),
        ('abnormal_usage', 'Abnormal Usage Detected'),  
        ('late_payment', 'Late Payment Penalty'),       
        ('high_consumption', 'High Consumption Alert'), 
    ]
    
    room = models.ForeignKey(Room, on_delete=models.CASCADE, null=True, blank=True)
    alert_type = models.CharField(max_length=20, choices=ALERT_TYPES)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.get_alert_type_display()} - {self.room.name if self.room else 'System'}"
    
    class Meta:
        ordering = ['-created_at']


class SystemSettings(models.Model):
    """Global system settings and configuration"""
    
    # System Information
    system_name = models.CharField(max_length=100, default="Smart Energy Monitor")
    
    # Admin Contact
    admin_name = models.CharField(max_length=100)
    admin_email = models.EmailField()
    admin_phone = models.CharField(max_length=20)
    
    # Billing Configuration
    electricity_rate = models.FloatField(default=23.0, help_text="PHP per kWh")
    late_penalty_amount = models.FloatField(default=50.0, help_text="PHP")
    
    # Reminder Settings
    reminder_days_before = models.IntegerField(default=3, help_text="Send reminder X days before due")
    
    # Smart Features
    abnormal_threshold = models.FloatField(default=2.0, help_text="Standard deviations for abnormal detection")
    
    # Automatic Bill Generation
    auto_generate_bills = models.BooleanField(default=True, help_text="Automatically generate bills at end of month")
    
    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = "System Settings"
        verbose_name_plural = "System Settings"
    
    def __str__(self):
        return f"⚙️ {self.system_name} Configuration"
    
    @classmethod
    def get_settings(cls):
        """Get or create default settings"""
        settings, created = cls.objects.get_or_create(
            id=1,
            defaults={
                'system_name': 'Smart Energy Monitor',
                'admin_name': 'Administrator',
                'admin_email': 'admin@example.com',
                'admin_phone': '+63 XXX XXX XXXX',
                'electricity_rate': 23.0,
                'late_penalty_amount': 50.0,
                'reminder_days_before': 3,
                'abnormal_threshold': 2.0,
                'auto_generate_bills': True,
            }
        )
        
        if created:
            print("✅ Created default system settings")
        
        return settings
    
    def get_admin_contact(self):
        return {
            'name': self.admin_name,
            'email': self.admin_email,
            'phone': self.admin_phone
        }
    
    def get_billing_config(self):
        return {
            'rate': self.electricity_rate,
            'penalty': self.late_penalty_amount,
            'reminder_days': self.reminder_days_before
        }


# ============ SIGNALS ============
@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)

@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    if hasattr(instance, 'userprofile'):
        instance.userprofile.save()

class APIToken(models.Model):
    name = models.CharField(max_length=100)
    token = models.CharField(max_length=64, unique=True)
    room = models.ForeignKey(Room, on_delete=models.CASCADE, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.name} - {self.room.name if self.room else 'All'}"

class Payment(models.Model):
    PAYMENT_METHODS = [
        ('gcash', 'GCash'),
        ('cash', 'Cash'),
    ]
    
    STATUS = [
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('failed', 'Failed'),
    ]
    
    bill = models.ForeignKey(Billing, on_delete=models.CASCADE, related_name='payments')
    tenant = models.ForeignKey(UserProfile, on_delete=models.CASCADE)
    amount = models.FloatField()
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHODS)
    reference_number = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=20, choices=STATUS, default='pending')
    transaction_id = models.CharField(max_length=100, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    
    def __str__(self):
        return f"{self.bill.room.name} - {self.billing_month} - {self.status}"
    
    class Meta:
        ordering = ['-created_at']