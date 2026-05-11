# system/admin.py - UPDATED (walang usage field)

from django.contrib import admin
from .models import Room, Billing, Alert, UserProfile, SystemSettings, EnergyUsage
from .models import Payment

@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ['name', 'get_current_usage', 'limit', 'power_status']
    list_filter = ['power_status']
    search_fields = ['name']
    
    def get_current_usage(self, obj):
        return obj.get_current_usage()
    get_current_usage.short_description = 'Current Usage (kWh)'

@admin.register(Billing)
class BillingAdmin(admin.ModelAdmin):
    list_display = ['room', 'billing_month', 'kwh', 'cost', 'is_paid', 'created_at']
    list_filter = ['billing_month', 'is_paid', 'room', 'created_at']
    search_fields = ['room__name', 'billing_month']
    list_editable = ['is_paid']
    date_hierarchy = 'created_at'

@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ['alert_type', 'room', 'message', 'is_read', 'created_at']
    list_filter = ['alert_type', 'is_read', 'created_at']
    search_fields = ['message', 'room__name']
    list_editable = ['is_read']
    date_hierarchy = 'created_at'

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'user_type', 'room']
    list_filter = ['user_type']
    search_fields = ['user__username', 'user__email', 'phone_number']
    raw_id_fields = ['user', 'room']

@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    list_display = ('admin_name', 'admin_email', 'admin_phone')

@admin.register(EnergyUsage)
class EnergyUsageAdmin(admin.ModelAdmin):
    list_display = ['room', 'kwh', 'voltage', 'current', 'power', 'timestamp']
    list_filter = ['room', 'timestamp']
    search_fields = ['room__name']
    ordering = ['-timestamp']

@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['bill', 'tenant', 'amount', 'payment_method', 'status', 'reference_number', 'created_at']
    list_filter = ['payment_method', 'status']
    search_fields = ['reference_number', 'bill__room__name', 'tenant__user__username']
    readonly_fields = ['reference_number', 'transaction_id', 'webhook_data', 'created_at']