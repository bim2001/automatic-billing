from django.contrib import admin 
from django.urls import path
from system import views
from system import api 

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.login_view, name='login_view'),
    path('register/', views.register_tenant, name='register_tenant'),
    path('logout/', views.logout_view, name='logout_view'),
    path('dashboard', views.dashboard, name='dashboard'),
    path('tenant/', views.tenant_dashboard, name='tenant_dashboard'),    
    path('toggle/<int:room_id>/', views.toggle_power, name='toggle_power'),
    path('api/meter-reading/', api.meter_reading, name='meter_reading'),
    
    # Billing URLs
    path('billing/', views.billing_view, name='billing_view'),
    path('billing/history/', views.billing_history, name='billing_history'),
    path('billing/mark-paid/<int:bill_id>/', views.mark_as_paid, name='mark_as_paid'),

 # Tenant management
    path('room/<int:room_id>/assign-tenant/', views.assign_tenant, name='assign_tenant'),
    path('room/<int:room_id>/remove-tenant/', views.remove_tenant, name='remove_tenant'),
    path('tenants/', views.tenant_list, name='tenant_list'),
    
     # Alert URLs
    path('alerts/', views.alerts_view, name='alerts_view'),
    path('alerts/mark-read/<int:alert_id>/', views.mark_alert_read, name='mark_alert_read'),
    path('alerts/clear-all/', views.clear_all_alerts, name='clear_all_alerts'),

     # Room CRUD URLs
    path('room/add/', views.add_room, name='add_room'),
    path('room/edit/<int:room_id>/', views.edit_room, name='edit_room'),
    path('room/delete/<int:room_id>/', views.delete_room, name='delete_room'),

     # Toggle Power
    path('toggle_power/<int:room_id>/', views.toggle_power, name='toggle_power'),
     # Tenant
    path('tenant/', views.tenant_dashboard, name='tenant_dashboard'),
    path('tenant/notifications/', views.tenant_notifications, name='tenant_notifications'),

    path('api/meter-reading/', api.meter_reading, name='meter_reading'),
    path('api/device-info/', api.device_info, name='device_info'),
    path('api/room-usage/', views.get_room_usage_data, name='room_usage_api'),
    path('api/building-stats/', views.get_building_stats, name='building_stats_api'),
    path('api/run-smart-features/', views.run_smart_features_api, name='run_smart_features_api'),
    path('api/system-health/', views.system_health, name='system_health'),

    path('system-health/', views.health_dashboard, name='system_health_dashboard'),

    path('monitoring/', views.monitoring_dashboard, name='monitoring_dashboard'),

    path('settings/', views.system_settings, name='system_settings'),
]