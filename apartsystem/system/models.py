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

class Billing(models.Model):
    room = models.ForeignKey(Room, on_delete=models.CASCADE)
    billing_month = models.CharField(max_length=50)
    kwh = models.FloatField(default=0)
    cost = models.FloatField(default=0)
    is_paid = models.BooleanField(default=False)
    due_date = models.DateField(default=date.today) 
    reminder_sent = models.BooleanField(default=False)  
    created_at = models.DateTimeField(auto_now_add=True)
    
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

class UserProfile(models.Model):
    USER_TYPES = [
        ('owner', 'Owner'),
        ('tenant', 'Tenant')
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    user_type = models.CharField(max_length=20, choices=USER_TYPES, default='tenant')
    room = models.ForeignKey(Room, on_delete=models.SET_NULL, null=True, blank=True)
   
    
    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} - {self.user_type}"

# Signal to create UserProfile automatically when a User is created
@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)

@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    if hasattr(instance, 'userprofile'):
        instance.userprofile.save()

class SystemSettings(models.Model):
    """Global system settings and configuration"""
    
    # System Information
    system_name = models.CharField(max_length=100, default="Smart Energy Monitor")
    
    # Admin Contact (existing fields)
    admin_name = models.CharField(max_length=100)
    admin_email = models.EmailField()
    admin_phone = models.CharField(max_length=20)
    
    # Billing Configuration
    electricity_rate = models.FloatField(default=23.0, help_text="PHP per kWh")
    late_penalty_amount = models.FloatField(default=50.0, help_text="PHP")
    
    # Reminder Settings
    reminder_days_before = models.IntegerField(default=3, help_text="Send reminder X days before due")
    
    # Smart Features
    abnormal_threshold = models.FloatField(
        default=2.0, 
        help_text="Standard deviations for abnormal detection (higher = less sensitive)"
    )
    
    # Automatic Bill Generation
    auto_generate_bills = models.BooleanField(
        default=True,
        help_text="Automatically generate bills at end of month"
    )
    
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
        """
        Get or create default settings.
        Use this everywhere instead of SystemSettings.objects.first()
        """
        settings, created = cls.objects.get_or_create(
            id=1,  # Always use ID 1 para iisa lang
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
        """Return formatted admin contact info"""
        return {
            'name': self.admin_name,
            'email': self.admin_email,
            'phone': self.admin_phone
        }
    
    def get_billing_config(self):
        """Return billing configuration"""
        return {
            'rate': self.electricity_rate,
            'penalty': self.late_penalty_amount,
            'reminder_days': self.reminder_days_before
        }