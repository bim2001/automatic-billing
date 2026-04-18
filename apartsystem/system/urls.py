from django.urls import path
from . import views
from . import api

urlpatterns = [
    # ==================== AUTHENTICATION ====================
    path('', views.login_view, name='login_view'),
    path('login/', views.login_view, name='login_view'),
    path('logout/', views.logout_view, name='logout_view'),
    path('register/', views.register_tenant, name='register_tenant'),
    
    # ==================== DASHBOARDS ====================
    path('dashboard/', views.dashboard, name='dashboard'),
    path('dashboard', views.dashboard, name='dashboard'),  # Without slash (backward compat)
    path('tenant/', views.tenant_dashboard, name='tenant_dashboard'),
    path('tenant/notifications/', views.tenant_notifications, name='tenant_notifications'),
    path('edit-profile/', views.edit_profile, name='edit_profile'),
    
    # ==================== ROOM MANAGEMENT ====================
    path('room/add/', views.add_room, name='add_room'),
    path('room/edit/<int:room_id>/', views.edit_room, name='edit_room'),
    path('room/delete/<int:room_id>/', views.delete_room, name='delete_room'),
    path('toggle_power/<int:room_id>/', views.toggle_power, name='toggle_power'),
    path('toggle/<int:room_id>/', views.toggle_power, name='toggle_power'),  # Old pattern
    
    # ==================== TENANT MANAGEMENT ====================
    path('room/<int:room_id>/assign-tenant/', views.assign_tenant, name='assign_tenant'),
    path('room/<int:room_id>/remove-tenant/', views.remove_tenant, name='remove_tenant'),
    path('tenants/', views.tenant_list, name='tenant_list'),
    
    # ==================== BILLING ====================
    path('billing/', views.billing_view, name='billing_view'),
    path('billing/history/', views.billing_history, name='billing_history'),
    path('billing/mark-paid/<int:bill_id>/', views.mark_as_paid, name='mark_as_paid'),
    path('billing/<int:bill_id>/mark-paid/', views.mark_as_paid, name='mark_as_paid'),  # Alternative
    path('billing/export/', views.export_billing_csv, name='export_billing_csv'),
    path('billing/report/', views.billing_report_html, name='billing_report_html'),
    
    # ==================== ALERTS ====================
    path('alerts/', views.alerts_view, name='alerts_view'),
    path('alerts/mark-read/<int:alert_id>/', views.mark_alert_read, name='mark_alert_read'),
    path('alerts/clear-all/', views.clear_all_alerts, name='clear_all_alerts'),
    
    # ==================== MONITORING & SETTINGS ====================
    path('monitoring/', views.monitoring_dashboard, name='monitoring_dashboard'),
    path('settings/', views.system_settings, name='system_settings'),
    path('system-health/', views.health_dashboard, name='health_dashboard'),
    path('health/', views.health_dashboard, name='system_health_dashboard'),
    
    # ==================== API ENDPOINTS ====================
    path('api/meter-reading/', api.meter_reading, name='meter_reading'),
    path('api/device-info/', api.device_info, name='device_info'),
    path('api/room-usage/', views.get_room_usage_data, name='room_usage_api'),
    path('api/building-stats/', views.get_building_stats, name='building_stats_api'),
    path('api/run-smart-features/', views.run_smart_features_api, name='run_smart_features_api'),
    path('api/system-health/', views.system_health, name='system_health'),
    path('api/paymongo-webhook/', views.paymongo_webhook, name='paymongo_webhook'),  # Remove trailing slash?

    # Payment URLs
    path('payment/gcash/<int:bill_id>/', views.create_gcash_payment, name='create_gcash_payment'),
    path('payment/success/<str:reference_number>/', views.payment_success, name='payment_success'),
    path('payment/cash/<int:bill_id>/', views.manual_paid_confirmation, name='manual_paid_confirmation'),
    path('payment/', views.payment_method, name='payment_method'),
 

    # Payment Checkout Simulation (for PayMongo)
    path('payment/checkout/<str:reference>/', views.payment_checkout_simulation, name='payment_checkout_simulation'),

    path('activity-log/', views.activity_log, name='activity_log'),
]