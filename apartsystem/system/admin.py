# system/admin.py - CORRECTED FULL VERSION

from django.contrib import admin
from .models import Room, Billing, Alert, UserProfile, SystemSettings

@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ['name', 'usage', 'limit', 'power_status']
    list_filter = ['power_status']
    search_fields = ['name']
    list_editable = ['usage', 'limit', 'power_status']

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
    search_fields = ['user__username', 'user__email']
    raw_id_fields = ['user', 'room']

@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    list_display = ('admin_name', 'admin_email', 'admin_phone')

