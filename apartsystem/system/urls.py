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
    path('dashboard', views.dashboard, name='dashboard'),
    path('tenant/', views.tenant_dashboard, name='tenant_dashboard'),
    path('tenant/notifications/', views.tenant_notifications, name='tenant_notifications'),
    path('edit-profile/', views.edit_profile, name='edit_profile'),
    path('tenant/delete/<int:tenant_id>/', views.delete_tenant, name='delete_tenant'),
    
    # ==================== ROOM MANAGEMENT ====================
    path('room/add/', views.add_room, name='add_room'),
    path('rooms/', views.rooms_page, name='rooms_page'),
    path('room/edit/<int:room_id>/', views.edit_room, name='edit_room'),
    path('room/delete/<int:room_id>/', views.delete_room, name='delete_room'),
    path('toggle_power/<int:room_id>/', views.toggle_power, name='toggle_power'),
    path('toggle/<int:room_id>/', views.toggle_power, name='toggle_power'),
    
    # ==================== TENANT MANAGEMENT ====================
    path('room/<int:room_id>/assign-tenant/', views.assign_tenant, name='assign_tenant'),
    path('room/<int:room_id>/remove-tenant/', views.remove_tenant, name='remove_tenant'),
    path('tenants/', views.tenant_list, name='tenant_list'),
    path('tenant/billing-history/', views.tenant_billing_history, name='tenant_billing_history'),
    path('tenant/billing-history/download/<int:bill_id>/', views.tenant_download_bill, name='tenant_download_bill'),

     # (for approve and assign room)
    path('api/tenant-details/<int:tenant_id>/', api.tenant_details_api, name='tenant_details_api'),
    path('approve-tenant/<int:tenant_id>/', views.approve_tenant, name='approve_tenant'),
    path('assign-room/<int:tenant_id>/', views.assign_room_to_tenant, name='assign_room_to_tenant'),
    
    # ==================== BILLING ====================
    path('billing/', views.billing_view, name='billing_view'),
    path('billing/history/', views.billing_history, name='billing_history'),
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
    path('activity-log/', views.activity_log, name='activity_log'),
    
    # ==================== API ENDPOINTS ====================
    path('api/meter-reading/', api.meter_reading, name='meter_reading'),
    path('api/device-info/', api.device_info, name='device_info'),
    path('api/room-usage/', views.get_room_usage_data, name='room_usage_api'),
    path('api/building-stats/', api.get_building_stats, name='building_stats_api'),  # ✅ GAMITIN ANG API
    path('api/run-smart-features/', views.run_smart_features_api, name='run_smart_features_api'),
    path('api/system-health/', views.system_health, name='system_health'),
    path('api/paymongo-webhook/', api.paymongo_webhook, name='paymongo_webhook'),
    path('api/room-status/<str:room_name>/', api.room_status, name='room_status'),
    path('api/admin/bill-details/<int:bill_id>/', views.bill_details_api, name='admin_bill_details_api'),
    
    path('api/bill-details/<int:bill_id>/', api.bill_details, name='bill_details_api'),

    path('api/tenant-details/<int:tenant_id>/', api.tenant_details_api, name='tenant_details_api'),
    path('approve-tenant/<int:tenant_id>/', views.approve_tenant, name='approve_tenant'),

    # ==================== PAYMENT URLs ====================
    path('payment/gcash/<int:bill_id>/', views.create_gcash_payment, name='create_gcash_payment'),
    path('payment/success/<str:reference_number>/', views.payment_success, name='payment_success'),
    path('payment/cash/<int:bill_id>/', views.manual_paid_confirmation, name='manual_paid_confirmation'),
    path('payment/cash/verify/<int:payment_id>/', views.verify_cash_payment, name='verify_cash_payment'),
    path('payment/', views.payment_method, name='payment_method'),
    path('payment/checkout/<str:reference>/', views.payment_checkout_simulation, name='payment_checkout_simulation'),
]
