# system/admin.py - UPDATED (walang usage field)

from django.contrib import admin
from .models import Room, Billing, Alert, UserProfile, SystemSettings, EnergyUsage, Payment


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ['name', 'get_current_usage', 'limit', 'power_status']
    list_filter = ['power_status']
    search_fields = ['name']
    
    def get_current_usage(self, obj):
        return f"{obj.get_current_usage():.2f} kWh"
    get_current_usage.short_description = 'Current Usage (kWh)'


@admin.register(Billing)
class BillingAdmin(admin.ModelAdmin):
    list_display = ['room', 'billing_month', 'kwh', 'formatted_cost', 'is_paid', 'due_date', 'created_at']
    list_filter = ['billing_month', 'is_paid', 'room', 'created_at']
    search_fields = ['room__name', 'billing_month']
    list_editable = ['is_paid']
    date_hierarchy = 'created_at'
    
    def formatted_cost(self, obj):
        """Display cost with 2 decimal places"""
        return f"₱{obj.cost:.2f}"
    formatted_cost.short_description = 'Cost'
    formatted_cost.admin_order_field = 'cost'


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
    list_display = ['room', 'formatted_kwh', 'voltage', 'current', 'power', 'timestamp']
    list_filter = ['room', 'timestamp']
    search_fields = ['room__name']
    ordering = ['-timestamp']
    
    def formatted_kwh(self, obj):
        """Display kWh with 2 decimal places"""
        return f"{obj.kwh:.2f} kWh"
    formatted_kwh.short_description = 'kWh'
    formatted_kwh.admin_order_field = 'kwh'


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['bill', 'tenant', 'formatted_amount', 'payment_method', 'status', 'reference_number', 'created_at']
    list_filter = ['payment_method', 'status']
    search_fields = ['reference_number', 'bill__room__name', 'tenant__user__username']
    readonly_fields = ['reference_number', 'transaction_id', 'webhook_data', 'created_at']
    
    def formatted_amount(self, obj):
        """Display amount with 2 decimal places"""
        return f"₱{obj.amount:.2f}"
    formatted_amount.short_description = 'Amount'
    formatted_amount.admin_order_field = 'amount'