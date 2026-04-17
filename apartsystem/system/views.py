from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.timezone import now
from datetime import datetime, date, timedelta
import calendar
from django.db.models import Sum, Q
from django.core.mail import send_mail
from django.contrib import messages
from django.http import JsonResponse
from django.contrib.auth.models import User
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.conf import settings as django_settings
import logging
import json
import secrets
import hashlib
from django.db import models 
from .models import Room, Billing, Alert, UserProfile, SystemSettings, EnergyUsage, Payment, TenantAssignment
from django.http import HttpResponse
import csv
from .models import ActivityLog

# Try to import paymongo (optional - for GCash)
try:
    from .paymongo import get_paymongo
    PAYMONGO_AVAILABLE = True
except ImportError:
    PAYMONGO_AVAILABLE = False
    print("⚠️ paymongo.py not found. GCash payments will be disabled.")

logger = logging.getLogger(__name__)

# ============== HELPER FUNCTIONS ==============
def get_settings():
    return SystemSettings.get_settings()

def get_last_day_of_month(year, month):
    return calendar.monthrange(year, month)[1]

def generate_reference_number(bill):
    """Generate unique reference number for payment"""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    room_code = bill.room.name.replace(' ', '').upper()[:10]
    amount_hash = hashlib.md5(str(bill.cost).encode()).hexdigest()[:6]
    return f"PAY-{room_code}-{timestamp}-{amount_hash}"

def create_payment_record(bill, tenant, payment_method):
    """Create a payment record"""
    reference = generate_reference_number(bill)
    
    payment = Payment.objects.create(
        bill=bill,
        tenant=tenant,
        amount=bill.cost,
        payment_method=payment_method,
        reference_number=reference,
        status='pending'
    )
    
    return payment

def mark_payment_as_paid(payment, transaction_id=None):
    """Mark payment as paid and update bill status"""
    payment.status = 'paid'
    payment.paid_at = timezone.now()
    if transaction_id:
        payment.transaction_id = transaction_id
    payment.save()
    
    # Update the bill status
    bill = payment.bill
    bill.is_paid = True
    bill.save()
    
    # Create alert
    Alert.objects.create(
        room=bill.room,
        alert_type='billing',
        message=f"✅ Payment received for {bill.billing_month} via {payment.get_payment_method_display()}. Reference: {payment.reference_number}"
    )
    
    return payment

def log_activity(user, action, description, ip_address=None):
    """Helper function to log user activities"""
    if not ip_address:
        try:
            import socket
            ip_address = socket.gethostbyname(socket.gethostname())
        except:
            ip_address = '127.0.0.1'
    
    user_type = 'tenant'
    if hasattr(user, 'userprofile'):
        user_type = user.userprofile.user_type
    
    ActivityLog.objects.create(
        user=user,
        user_type=user_type,
        action=action,
        description=description,
        ip_address=ip_address
    )

# ============== PAYMONGO WEBHOOK (NEW - ITO ANG KULANG MO!) ==============
@csrf_exempt
def paymongo_webhook(request):
    """Handle PayMongo webhook callbacks for payment status updates"""
    if request.method != 'POST':
        return JsonResponse({"error": "Method not allowed"}, status=405)
    
    try:
        payload = json.loads(request.body)
        
        print(f"\n{'='*60}")
        print(f"📢 WEBHOOK RECEIVED:")
        print(f"{json.dumps(payload, indent=2)}")
        print(f"{'='*60}\n")
        
        event_type = payload.get('data', {}).get('attributes', {}).get('type', '')
        payment_attrs = payload.get('data', {}).get('attributes', {}).get('data', {}).get('attributes', {})
        payment_id = payload.get('data', {}).get('attributes', {}).get('data', {}).get('id', '')
        status = payment_attrs.get('status', '')
        description = payment_attrs.get('description', '')
        
        print(f"📊 Event: {event_type}")
        print(f"📊 Payment ID: {payment_id}")
        print(f"📊 Status: {status}")
        print(f"📊 Description: {description}")
        
        # ✅ Kunin ang reference_number mula sa description
        reference_number = None
        if description:
            import re
            # Hanapin ang pattern: Ref: PAY-XXXX-XXXX-XXXX
            match = re.search(r'Ref:\s*(PAY-[A-Z0-9]+-\d+-[a-f0-9]+)', description)
            if match:
                reference_number = match.group(1)
                print(f"✅ Extracted reference_number from description: {reference_number}")
        
        if event_type == 'payment.paid' or status == 'paid':
            if not reference_number:
                print("⚠️ No reference_number found in webhook payload")
                return JsonResponse({"status": "ignored", "reason": "no reference_number"}, status=200)
            
            try:
                payment = Payment.objects.get(reference_number=reference_number)
                
                print(f"✅ Found payment: ID={payment.id}, Bill={payment.bill.id}")
                
                payment.status = 'paid'
                payment.paid_at = timezone.now()
                payment.transaction_id = payment_id
                payment.webhook_received = True
                payment.webhook_data = payload
                payment.save()
                
                bill = payment.bill
                bill.is_paid = True
                bill.save()
                
                Alert.objects.create(
                    room=bill.room,
                    alert_type='billing',
                    message=f"✅ Payment of ₱{payment.amount} for {bill.billing_month} has been confirmed via GCash Webhook."
                )
                
                print(f"✅ Payment {reference_number} marked as paid via WEBHOOK!")
                
            except Payment.DoesNotExist:
                print(f"⚠️ Payment not found for reference: {reference_number}")
        
        return JsonResponse({"status": "success", "event": event_type}, status=200)
        
    except Exception as e:
        print(f"❌ Webhook error: {str(e)}")
        import traceback
        traceback.print_exc()
        return JsonResponse({"error": "Internal server error"}, status=500)

def get_building_stats(request):
    """API endpoint para sa building-wide statistics"""
    profile = request.user.userprofile
    
    # Only owners can access
    if profile.user_type != 'owner':
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    # Kunin settings for rate display (optional)
    settings = get_settings()
    
    # Kunin lahat ng rooms
    rooms = Room.objects.all()
    
    # Current month
    today = datetime.now()
    year = today.year
    month = today.month
    
    # Kunin ang usage per room for this month
    room_stats = []
    total_building_usage = 0
    
    for room in rooms:
        total_kwh = EnergyUsage.objects.filter(
            room=room,
            timestamp__year=year,
            timestamp__month=month
        ).aggregate(total=Sum('kwh'))['total'] or 0
        
        room_stats.append({
            'name': room.name,
            'usage': round(total_kwh, 2),
            'limit': room.limit,
            'status': 'ON' if room.power_status else 'OFF',
            'tenant': room.get_tenant_name() or 'Vacant'
        })
        
        total_building_usage += total_kwh
    
    # Sort by usage (highest first)
    room_stats.sort(key=lambda x: x['usage'], reverse=True)
    
    # Kunin ang daily building usage for chart
    daily_building = []
    days = []
    
    for day in range(1, 32):
        day_total = EnergyUsage.objects.filter(
            timestamp__year=year,
            timestamp__month=month,
            timestamp__day=day
        ).aggregate(total=Sum('kwh'))['total'] or 0
        
        if day_total > 0:
            daily_building.append(round(day_total, 2))
            days.append(f"Day {day}")
    
    return JsonResponse({
        'total_rooms': len(rooms),
        'total_usage': round(total_building_usage, 2),
        'avg_per_room': round(total_building_usage / len(rooms), 2) if rooms else 0,
        'room_stats': room_stats[:5],  # Top 5
        'daily_building': daily_building,
        'days': days,
        'month': today.strftime("%B %Y")
    })

@login_required
def get_room_usage_data(request):
    """API endpoint para kunin ang usage data for graphs"""
    profile = request.user.userprofile
    
    # Check if tenant
    if profile.user_type != 'tenant':
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    room = profile.room
    if not room:
        return JsonResponse({'error': 'No room assigned'}, status=404)
    
    # Kunin ang current month
    today = datetime.now()
    year = today.year
    month = today.month
    
    # Kunin ang daily usage for the month
    daily_usage = []
    days_in_month = []
    
    # Get all readings for this month
    readings = EnergyUsage.objects.filter(
        room=room,
        timestamp__year=year,
        timestamp__month=month
    ).order_by('timestamp')
    
    # Group by day
    from collections import defaultdict
    daily_totals = defaultdict(float)
    
    for reading in readings:
        day = reading.timestamp.day
        daily_totals[day] += reading.kwh
    
    # Create arrays for Chart.js
    for day in range(1, 32):  # Max 31 days
        if day in daily_totals:
            daily_usage.append(round(daily_totals[day], 2))
            days_in_month.append(f"Day {day}")
    
    # Kunin din ang previous month for comparison
    if month == 1:
        prev_month = 12
        prev_year = year - 1
    else:
        prev_month = month - 1
        prev_year = year
    
    prev_month_readings = EnergyUsage.objects.filter(
        room=room,
        timestamp__year=prev_year,
        timestamp__month=prev_month
    ).aggregate(total=Sum('kwh'))['total'] or 0
    
    current_month_total = sum(daily_usage)
    
    return JsonResponse({
        'room': room.name,
        'current_month': today.strftime("%B %Y"),
        'daily_usage': daily_usage,
        'days': days_in_month,
        'total_current': round(current_month_total, 2),
        'total_previous': round(prev_month_readings, 2),
        'comparison': round(current_month_total - prev_month_readings, 2)
    })


# ============== BILLING FUNCTIONS ==============
def generate_monthly_bills(year=None, month=None):
    """Generate prorated bills for all tenants based on their move-in/move-out dates"""
    
    from .models import SystemSettings, TenantAssignment, Billing, EnergyUsage
    from django.db.models import Q, Sum
    from datetime import date, datetime
    import calendar
    
    def get_last_day_of_month(year, month):
        """Return the last day of the given month"""
        return calendar.monthrange(year, month)[1]
    
    # Get electricity rate from settings
    settings = SystemSettings.get_settings()
    electricity_rate = settings.electricity_rate
    
    if year is None or month is None:
        today = datetime.now()
        year = today.year
        month = today.month
    
    month_name = datetime(year, month, 1).strftime("%B %Y")
    month_start = date(year, month, 1)
    month_end = date(year, month, get_last_day_of_month(year, month))
    
    print(f"\n📊 Generating prorated bills for {month_name}...")
    print("=" * 60)
    
    # Get all active tenant assignments for this month
    assignments = TenantAssignment.objects.filter(
        move_in_date__lte=month_end,
        is_active=True
    ).filter(
        Q(move_out_date__isnull=True) | Q(move_out_date__gte=month_start)
    )
    
    bills_created = 0
    bills_updated = 0
    
    for assignment in assignments:
        room = assignment.room
        tenant = assignment.tenant
        
        # Calculate days occupied this month
        days_occupied = assignment.days_occupied_in_month(year, month)
        
        if days_occupied == 0:
            continue
        
        # Get total kWh for the room this month
        total_kwh = EnergyUsage.objects.filter(
            room=room,
            timestamp__year=year,
            timestamp__month=month
        ).aggregate(total=Sum('kwh'))['total'] or 0
        
        # Prorate based on days occupied
        total_days_in_month = get_last_day_of_month(year, month)
        
        # Avoid division by zero
        if total_days_in_month > 0:
            prorated_kwh = (total_kwh / total_days_in_month) * days_occupied
        else:
            prorated_kwh = 0
            
        prorated_cost = prorated_kwh * electricity_rate
        due_date = month_end
        
        # Check if bill already exists for this assignment
        bill, created = Billing.objects.update_or_create(
            room=room,
            billing_month=month_name,
            tenant_assignment=assignment,
            defaults={
                'kwh': round(prorated_kwh, 2),
                'cost': round(prorated_cost, 2),
                'is_paid': False,
                'due_date': due_date,
                'reminder_sent': False,
                'days_occupied': days_occupied
            }
        )
        
        if created:
            bills_created += 1
            status = "✅ CREATED"
        else:
            bills_updated += 1
            status = "🔄 UPDATED"
        
        print(f"{status}: {room.name} - {tenant.user.username}")
        print(f"   Days occupied: {days_occupied}/{total_days_in_month} days")
        print(f"   Total kWh: {total_kwh:.2f} → Prorated: {prorated_kwh:.2f} kWh")
        print(f"   Amount: ₱{prorated_cost:.2f}")
        print("-" * 40)
    
    print("=" * 60)
    print(f"✅ Done! Created: {bills_created}, Updated: {bills_updated}")
    
    return bills_created, bills_updated


def send_payment_reminders(days_before_due=3, test_mode=False):
    """
    Send payment reminders for bills due in X days
    Returns: dict with counts of emails sent
    """
    today = timezone.now().date()
    reminder_date = today + timedelta(days=days_before_due)
    
    print(f"\n📧 CHECKING BILLS DUE ON: {reminder_date} (in {days_before_due} days)")
    print("=" * 60)
    
    # Kunin ang bills na due in X days, unpaid, at hindi pa nasendan ng reminder
    bills = Billing.objects.filter(
        due_date=reminder_date,
        is_paid=False,
        reminder_sent=False
    )
    
    if not bills.exists():
        print("❌ No bills to process")
        return {'sent': 0, 'failed': 0, 'skipped': 0}
    
    print(f"📊 Found {bills.count()} bill(s) to process")
    print("-" * 60)
    
    sent_count = 0
    failed_count = 0
    skipped_count = 0
    
    for bill in bills:
        try:
            # Kunin ang tenant
            tenant_profile = UserProfile.objects.get(room=bill.room, user_type='tenant')
            tenant = tenant_profile.user
            tenant_email = tenant.email
            tenant_name = tenant.get_full_name() or tenant.username
            
            # Skip if no email
            if not tenant_email:
                print(f"⚠️ SKIPPED: {bill.room.name} - No email for {tenant_name}")
                skipped_count += 1
                continue
            
            # Compute days remaining
            days_remaining = (bill.due_date - today).days
            
            # Create email content
            subject = f"🧾 Bill Reminder: {bill.billing_month} due in {days_remaining} days"
            
            message = f"""
Hi {tenant_name},

This is a reminder about your electricity bill.

━━━━━━━━━━━━━━━━━━━━━━━━
Room: {bill.room.name}
Billing Month: {bill.billing_month}
Amount Due: ₱{bill.cost}
Due Date: {bill.due_date}
Days Remaining: {days_remaining}
━━━━━━━━━━━━━━━━━━━━━━━━

Please settle your payment before the due date to avoid late payment penalties.

If you have already paid, please ignore this message.

Thank you,
Smart Energy Monitor System
            """
            
            if test_mode:
                print(f"📧 TEST MODE - Would send to: {tenant_email}")
                print(f"   Subject: {subject}")
                print(f"   Message: {message[:100]}...")
                sent_count += 1
            else:
                # ACTUAL SENDING - use django_settings, not the system settings
                send_mail(
                    subject=subject,
                    message=message,
                    from_email=django_settings.EMAIL_HOST_USER,
                    recipient_list=[tenant_email],
                    fail_silently=False,
                )
                
                # Mark as sent
                bill.reminder_sent = True
                bill.save(update_fields=['reminder_sent'])
                
                print(f"✅ SENT: {bill.room.name} - {tenant_email}")
                sent_count += 1
            
        except UserProfile.DoesNotExist:
            print(f"❌ FAILED: {bill.room.name} - No tenant assigned")
            failed_count += 1
        except Exception as e:
            print(f"❌ FAILED: {bill.room.name} - {str(e)}")
            failed_count += 1
    
    print("-" * 60)
    print(f"📊 SUMMARY: Sent: {sent_count}, Failed: {failed_count}, Skipped: {skipped_count}")
    
    return {
        'sent': sent_count,
        'failed': failed_count,
        'skipped': skipped_count
    }


def check_all_upcoming_bills(days_ahead=7):
    """
    Check all bills due in the next X days
    """
    today = timezone.now().date()
    results = {}
    
    for days in range(1, days_ahead + 1):
        check_date = today + timedelta(days=days)
        count = Billing.objects.filter(
            due_date=check_date,
            is_paid=False,
            reminder_sent=False
        ).count()
        
        if count > 0:
            results[str(check_date)] = count
    
    return results


# ============== AUTHENTICATION VIEWS ==============
def register_tenant(request):
    context = {}

    if request.method == "POST":
        username = request.POST.get("username")
        email = request.POST.get("email")
        password = request.POST.get("password")
        password2 = request.POST.get("password2")

        if password != password2:
            context['register_error'] = "Passwords do not match."
        elif User.objects.filter(username=username).exists():
            context['register_error'] = "Username already exists."
        elif User.objects.filter(email=email).exists():
            context['register_error'] = "Email already registered."
        else:
            # Create the user
            user = User.objects.create_user(username=username, email=email, password=password)

            # Only create UserProfile if it doesn't exist yet
            if not hasattr(user, 'userprofile'):
                UserProfile.objects.create(user=user, user_type='tenant')

            context['register_success'] = "Account created successfully! You can now login."
            return redirect('login_view')

    return render(request, "system/login.html", context)


def login_view(request):
    error = None
    
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        
        if not username or not password:
            error = "Username and password are required."
        else:
            user = authenticate(request, username=username, password=password)
            
            if user is not None:
                login(request, user)
                log_activity(user, 'login', f"User {username} logged in")
                
                try:
                    profile = user.userprofile
                except UserProfile.DoesNotExist:
                    user_type = 'owner' if user.is_staff or user.is_superuser else 'tenant'
                    profile = UserProfile.objects.create(user=user, user_type=user_type)
                
                if profile.user_type == 'tenant':
                    return redirect('tenant_dashboard')
                else:
                    return redirect('dashboard')
            else:
                error = "Invalid username or password. Please try again."
    
    return render(request, 'system/login.html', {'error': error, 'login_error': error})

def logout_view(request):
    if request.user.is_authenticated:
        log_activity(request.user, 'logout', f"User {request.user.username} logged out")
    
    logout(request)
    return redirect('login_view')

# ============== OWNER DASHBOARD ==============
@login_required
def dashboard(request):
    profile = request.user.userprofile
    settings = get_settings()

    # Only allow owners/admins
    if profile.user_type != 'owner':
        return redirect('tenant_dashboard')

    rooms = Room.objects.all()
    
    # Get recent alerts
    recent_alerts = Alert.objects.order_by('-created_at')[:10]
    
    for room in rooms:
        # Calculate billing using settings rate
        room.cost = room.usage * settings.electricity_rate
        
        # AUTO-OFF LOGIC with Alert
        if room.usage > room.limit and room.power_status:
            # Automatically turn OFF if over limit
            room.power_status = False
            room.save()
            
            # Create alert for auto power-off
            Alert.objects.create(
                room=room,
                alert_type='power_off',
                message=f"Room {room.name} exceeded limit ({room.usage} > {room.limit} kWh). Power automatically turned OFF."
            )
        
        room.over_limit = room.usage > room.limit
        
        # Create alert for over limit (if not already created today)
        if room.over_limit and not Alert.objects.filter(
            room=room, 
            alert_type='over_limit',
            created_at__date=timezone.now().date()
        ).exists():
            Alert.objects.create(
                room=room,
                alert_type='over_limit',
                message=f"Room {room.name} is over limit! Current: {room.usage} kWh, Limit: {room.limit} kWh"
            )
    
    # Calculate dashboard stats
    total_rooms = rooms.count()
    occupied_rooms = sum(1 for room in rooms if room.is_occupied())
    total_kwh = sum(room.usage for room in rooms)
    total_cost = sum(room.cost for room in rooms)
    over_limit_count = sum(1 for room in rooms if room.over_limit)
    unread_alerts_count = Alert.objects.filter(is_read=False).count()
    
    # Get all tenants without rooms for assignment
    available_rooms = total_rooms - occupied_rooms
    available_tenants = UserProfile.objects.filter(
        user_type='tenant', 
        room__isnull=True
    ).select_related('user')
    
    return render(request, 'system/dashboard.html', {
        'rooms': rooms,
        'username': request.user.username,
        'electricity_rate': settings.electricity_rate,
        'recent_alerts': recent_alerts,
        'unread_alerts_count': unread_alerts_count,
        'available_tenants': available_tenants,
        'stats': {
            'total_rooms': total_rooms,
            'occupied_rooms': occupied_rooms,
            'total_kwh': total_kwh,
            'total_cost': total_cost,
            'over_limit_count': over_limit_count,
            'available_rooms': available_rooms, 
        }
    })


# ============== TENANT DASHBOARD ==============
@login_required
def tenant_dashboard(request):
    settings = get_settings()
    
    # Send reminders at login using settings value
    send_payment_reminders(days_before_due=settings.reminder_days_before)

    profile = request.user.userprofile
    if profile.user_type != 'tenant':
        return redirect('dashboard')

    room = profile.room
    admin_info = SystemSettings.objects.first()

    if not room:
        return render(request, 'user/tenant_dashboard.html', {
            'no_room': True,
            'admin_info': admin_info,
            'username': request.user.username,
        })

    bills = Billing.objects.filter(room=room).order_by('-created_at')
    current_month = timezone.now().strftime("%B %Y")
    current_bill = bills.filter(billing_month=current_month).first()

    # ==================== PAYMENT SECTION ====================
    pending_payment = None
    reference_number = ''
    
    if current_bill:
        # Get existing pending payment
        pending_payment = Payment.objects.filter(
            bill=current_bill,
            status='pending'
        ).first()
        
        # If no pending payment exists and bill is not paid, create one
        if not pending_payment and not current_bill.is_paid:
            pending_payment = create_payment_record(current_bill, profile, 'cash')  # Default to cash
        
        # Generate reference number for display
        if pending_payment:
            reference_number = pending_payment.reference_number
    # ==========================================================

    # Tenant alert types
    tenant_alert_types = ['over_limit', 'power_off', 'power_on', 'billing', 'late_payment', 'abnormal_usage', 'high_consumption']
    
    # Get recent alerts (5 lang)
    recent_alerts = Alert.objects.filter(
        room=room,
        alert_type__in=tenant_alert_types
    ).order_by('-created_at')[:5]
    
    # Get total count ng alerts
    alerts_count = Alert.objects.filter(
        room=room,
        alert_type__in=tenant_alert_types
    ).count()
    
    # Get unread alerts count
    unread_alerts_count = Alert.objects.filter(
        room=room,
        alert_type__in=tenant_alert_types,
        is_read=False
    ).count()

    # Compute totals
    total_kwh = sum(bill.kwh for bill in bills)
    total_paid = sum(bill.cost for bill in bills if bill.is_paid)

    # Average daily usage
    days_in_month = 30
    avg_daily_usage = room.usage / days_in_month if room.usage else 0

    # Due date
    due_date = None
    if current_bill and current_bill.due_date:
        due_date = current_bill.due_date

    return render(request, 'user/tenant_dashboard.html', {
        'room': room,
        'bills': bills,
        'current_bill': current_bill,
        'current_month': current_month,
        'due_date': due_date,
        'electricity_rate': settings.electricity_rate,
        'username': request.user.username,
        'recent_alerts': recent_alerts,
        'alerts_count': alerts_count,
        'unread_alerts_count': unread_alerts_count,
        'admin_info': admin_info,
        'avg_daily_usage': avg_daily_usage,
        'total_kwh': total_kwh,
        'total_paid': total_paid,
        # Payment-related context
        'pending_payment': pending_payment,
        'reference_number': reference_number,
    })

@login_required
def tenant_notifications(request):
    profile = request.user.userprofile
    
    if profile.user_type != 'tenant':
        return redirect('dashboard')
    
    room = profile.room
    
    if not room:
        return redirect('tenant_dashboard')
    
    tenant_alert_types = ['over_limit', 'power_off', 'power_on', 'billing', 'late_payment', 'abnormal_usage', 'high_consumption']
    
    all_alerts = Alert.objects.filter(
        room=room,
        alert_type__in=tenant_alert_types
    ).order_by('-created_at')
    
    from django.core.paginator import Paginator
    paginator = Paginator(all_alerts, 20)
    page_number = request.GET.get('page')
    alerts = paginator.get_page(page_number)
    
    if request.method == 'POST' and 'mark_all_read' in request.POST:
        all_alerts.filter(is_read=False).update(is_read=True)
        return redirect('tenant_notifications')
    
    unread_count = all_alerts.filter(is_read=False).count()
    
    return render(request, 'user/tenant_notifications.html', {
        'alerts': alerts,
        'unread_count': unread_count,
        'room': room,
        'username': request.user.username,
    })


# ============== ROOM MANAGEMENT ==============
@login_required
def toggle_power(request, room_id):
    # Check if user is owner
    if request.user.userprofile.user_type != 'owner':
        messages.error(request, "You don't have permission to do that.")
        return redirect('tenant_dashboard')
    
    room = get_object_or_404(Room, id=room_id)
    old_status = room.power_status
    room.power_status = not room.power_status
    room.save()
    
    # Create alert for manual power change
    if old_status:  # Was ON, now OFF
        alert_type = 'power_off'
        message = f"Room {room.name} power manually turned OFF by owner."
    else:  # Was OFF, now ON
        alert_type = 'power_on'
        message = f"Room {room.name} power manually turned ON by owner."
    
    Alert.objects.create(
        room=room,
        alert_type=alert_type,
        message=message
    )
    
    return redirect('dashboard')


@login_required
def add_room(request):
    if request.user.userprofile.user_type != 'owner':
        return redirect('tenant_dashboard')
    
    if request.method == 'POST':
        name = request.POST.get('name')
        limit = float(request.POST.get('limit', 200))
        usage = float(request.POST.get('usage', 0))
        power_status = request.POST.get('power_status') == 'on'
        
        if not name:
            messages.error(request, "Room name is required.")
            return render(request, 'system/add_room.html')
        
        room = Room.objects.create(
            name=name,
            usage=usage,
            limit=limit,
            power_status=power_status
        )
        
        log_activity(request.user, 'create', f"Created new room: {name}")
        
        Alert.objects.create(
            alert_type='power_on',
            message=f"New room added: {name}",
            room=room
        )
        
        messages.success(request, f"Room '{name}' added successfully!")
        return redirect('dashboard')
    
    unread_alerts_count = Alert.objects.filter(is_read=False).count()
    
    return render(request, 'system/add_room.html', {
        'username': request.user.username,
        'unread_alerts_count': unread_alerts_count
    })


@login_required
def edit_room(request, room_id):
    if request.user.userprofile.user_type != 'owner':
        return redirect('tenant_dashboard')
    
    room = get_object_or_404(Room, id=room_id)
    old_name = room.name
    
    if request.method == 'POST':
        room.name = request.POST.get('name')
        room.limit = float(request.POST.get('limit', 200))
        room.usage = float(request.POST.get('usage', room.usage))
        room.save()
        
        log_activity(request.user, 'update', f"Updated room: {old_name} → {room.name}")
        
        Alert.objects.create(
            alert_type='billing',
            message=f"Room {room.name} updated",
            room=room
        )
        
        messages.success(request, f"Room '{room.name}' updated successfully!")
        return redirect('dashboard')
    
    unread_alerts_count = Alert.objects.filter(is_read=False).count()
    
    return render(request, 'system/edit_room.html', {
        'room': room,
        'username': request.user.username,
        'unread_alerts_count': unread_alerts_count
    })


@login_required
def delete_room(request, room_id):
    if request.user.userprofile.user_type != 'owner':
        return redirect('tenant_dashboard')
    
    room = get_object_or_404(Room, id=room_id)
    room_name = room.name
    
    log_activity(request.user, 'delete', f"Deleted room: {room_name}")
    
    UserProfile.objects.filter(room=room, user_type='tenant').update(room=None)
    
    Alert.objects.create(
        alert_type='power_off',
        message=f"Room deleted: {room_name}",
        room=None
    )
    
    room.delete()
    messages.success(request, f"Room '{room_name}' deleted successfully!")
    return redirect('dashboard')


# ============== TENANT ASSIGNMENT ==============
@login_required
def assign_tenant(request, room_id):
    if request.user.userprofile.user_type != 'owner':
        messages.error(request, "You don't have permission to do that.")
        return redirect('dashboard')
    
    if request.method == 'POST':
        tenant_id = request.POST.get('tenant_id')
        move_in_date = request.POST.get('move_in_date')
        room = get_object_or_404(Room, id=room_id)
        
        if tenant_id:
            tenant_profile = get_object_or_404(UserProfile, id=tenant_id, user_type='tenant')
            
            TenantAssignment.objects.filter(room=room, is_active=True).update(is_active=False)
            
            assignment = TenantAssignment.objects.create(
                tenant=tenant_profile,
                room=room,
                move_in_date=move_in_date or date.today(),
                is_active=True
            )
            
            tenant_profile.room = room
            tenant_profile.save()
            
            log_activity(request.user, 'assign', f"Assigned {tenant_profile.user.username} to room {room.name}")
            
            Alert.objects.create(
                room=room,
                alert_type='tenant_assigned',
                message=f"Tenant {tenant_profile.user.username} assigned to room {room.name} starting {assignment.move_in_date}"
            )
            
            messages.success(request, f"Tenant {tenant_profile.user.username} assigned to {room.name}")
        else:
            TenantAssignment.objects.filter(room=room, is_active=True).update(is_active=False)
            UserProfile.objects.filter(room=room, user_type='tenant').update(room=None)
            
            log_activity(request.user, 'remove', f"Removed tenant from room {room.name}")
            
            messages.success(request, f"Tenant removed from {room.name}.")
    
    return redirect('dashboard')


@login_required
def remove_tenant(request, room_id):
    if request.user.userprofile.user_type != 'owner':
        messages.error(request, "You don't have permission to do that.")
        return redirect('dashboard')
    
    room = get_object_or_404(Room, id=room_id)
    
    tenant_profile = UserProfile.objects.filter(room=room, user_type='tenant').first()
    if tenant_profile:
        tenant_name = tenant_profile.user.get_full_name() or tenant_profile.user.username
        tenant_profile.room = None
        tenant_profile.save()
    
        Alert.objects.create(
            room=room,
            alert_type='tenant_removed',
            message=f"Tenant {tenant_name} removed from room {room.name}"
        )
    
    return redirect('dashboard')


@login_required
def tenant_list(request):
    if request.user.userprofile.user_type != 'owner':
        return redirect('dashboard')
    
    tenants = UserProfile.objects.filter(
        user_type='tenant'
    ).select_related('user', 'room')
    
    total_tenants = tenants.count()
    with_room = tenants.filter(room__isnull=False).count()
    without_room = tenants.filter(room__isnull=True).count()
    
    return render(request, 'system/tenant_list.html', {
        'tenants': tenants,
        'total_tenants': total_tenants,
        'with_room': with_room,
        'without_room': without_room,
        'username': request.user.username,
    })


# ============== BILLING VIEWS ==============
@login_required
def billing_view(request):
    settings = get_settings()
    
    if request.user.userprofile.user_type != 'owner':
        return redirect('tenant_dashboard')
    
    current_month = timezone.now().strftime("%B %Y")
    
    from datetime import date
    import calendar
    today = date.today()
    last_day = calendar.monthrange(today.year, today.month)[1]
    due_date = date(today.year, today.month, last_day)
    
    rooms = Room.objects.all()
    for room in rooms:
        has_tenant = UserProfile.objects.filter(room=room, user_type='tenant').exists()
        
        if has_tenant:
            bill, created = Billing.objects.get_or_create(
                room=room,
                billing_month=current_month,
                defaults={
                    'kwh': room.usage,
                    'cost': room.usage * settings.electricity_rate,
                    'is_paid': False,
                    'due_date': due_date,
                    'reminder_sent': False
                }
            )
            
            if not created:
                bill.kwh = room.usage
                bill.cost = room.usage * settings.electricity_rate
                bill.save()
        else:
            Billing.objects.filter(room=room, billing_month=current_month).delete()
    
    bills = Billing.objects.filter(
        billing_month=current_month,
        room__userprofile__user_type='tenant',
        room__userprofile__isnull=False
    ).select_related('room').distinct()
    
    total_kwh = sum(bill.kwh for bill in bills)
    total_cost = sum(bill.cost for bill in bills)
    paid_count = sum(1 for bill in bills if bill.is_paid)
    
    unread_alerts_count = Alert.objects.filter(is_read=False).count()
    
    return render(request, 'system/billing.html', {
        'bills': bills,
        'current_month': current_month,
        'total_kwh': total_kwh,
        'total_cost': total_cost,
        'paid_count': paid_count,
        'username': request.user.username,
        'electricity_rate': settings.electricity_rate,
        'unread_alerts_count': unread_alerts_count
    })

@login_required
def mark_as_paid(request, bill_id):
    if request.user.userprofile.user_type != 'owner':
        return redirect('tenant_dashboard')
    
    bill = get_object_or_404(Billing, id=bill_id)
    
    has_tenant = UserProfile.objects.filter(room=bill.room, user_type='tenant').exists()
    
    if not has_tenant:
        messages.error(request, f"Cannot mark bill for {bill.room.name} as paid - no tenant assigned to this room.")
        next_url = request.GET.get('next', 'billing_view')
        return redirect(next_url)
    
    bill.is_paid = not bill.is_paid
    bill.save()
    
    messages.success(request, f"Payment status updated for {bill.room.name}.")
    
    next_url = request.GET.get('next', 'billing_view')
    return redirect(next_url)


@login_required
def billing_history(request):
    settings = get_settings()
    
    if request.user.userprofile.user_type != 'owner':
        return redirect('tenant_dashboard')
    
    room_name = request.GET.get('room_name', '').strip()
    month_filter = request.GET.get('month', '')
    status_filter = request.GET.get('status', '')
    start_date = request.GET.get('start_date', '')
    end_date = request.GET.get('end_date', '')
    
    bills_query = Billing.objects.filter(
        room__userprofile__user_type='tenant'
    ).select_related('room').distinct()
    
    if room_name:
        bills_query = bills_query.filter(room__name__icontains=room_name)
    if month_filter:
        bills_query = bills_query.filter(billing_month=month_filter)
    if status_filter == 'paid':
        bills_query = bills_query.filter(is_paid=True)
    elif status_filter == 'unpaid':
        bills_query = bills_query.filter(is_paid=False)
    if start_date:
        try:
            start_datetime = datetime.strptime(start_date, '%Y-%m-%d')
            bills_query = bills_query.filter(created_at__date__gte=start_datetime)
        except ValueError:
            pass
    if end_date:
        try:
            end_datetime = datetime.strptime(end_date, '%Y-%m-%d')
            bills_query = bills_query.filter(created_at__date__lte=end_datetime)
        except ValueError:
            pass
    
    all_bills = bills_query.order_by('-billing_month', 'room__name')
    
    available_months = Billing.objects.filter(
        room__userprofile__user_type='tenant'
    ).values_list('billing_month', flat=True).distinct().order_by('-billing_month')
    
    bills_by_month = {}
    for bill in all_bills:
        month_str = bill.billing_month
        
        if month_str not in bills_by_month:
            bills_by_month[month_str] = {
                'bills': [],
                'total_kwh': 0,
                'total_cost': 0,
                'paid_count': 0,
                'total_bills': 0
            }
        
        bills_by_month[month_str]['bills'].append(bill)
        bills_by_month[month_str]['total_kwh'] += bill.kwh
        bills_by_month[month_str]['total_cost'] += bill.cost
        bills_by_month[month_str]['total_bills'] += 1
        
        if bill.is_paid:
            bills_by_month[month_str]['paid_count'] += 1
    
    def month_sort_key(month_str):
        try:
            return datetime.strptime(month_str, "%B %Y")
        except:
            return datetime.min
    
    sorted_months = dict(sorted(
        bills_by_month.items(), 
        key=lambda x: month_sort_key(x[0]), 
        reverse=True
    ))
    
    search_params = {
        'room_name': room_name,
        'month': month_filter,
        'status': status_filter,
        'start_date': start_date,
        'end_date': end_date,
    }
    
    return render(request, 'system/billing_history.html', {
        'all_bills': all_bills,
        'bills_by_month': sorted_months,
        'available_months': available_months,
        'search_params': search_params,
        'username': request.user.username,
        'electricity_rate': settings.electricity_rate,
        'unread_alerts_count': Alert.objects.filter(is_read=False).count(),
    })


# ============== ALERTS VIEWS ==============
@login_required
def alerts_view(request):
    all_alerts = Alert.objects.order_by('-created_at')

    if request.method == 'POST' and 'mark_all_read' in request.POST:
        Alert.objects.filter(is_read=False).update(is_read=True)
        messages.success(request, "All alerts marked as read.")
        return redirect('alerts_view')

    unread_alerts_count = Alert.objects.filter(is_read=False).count()

    return render(request, 'system/alerts.html', {
        'alerts': all_alerts,
        'username': request.user.username,
        'unread_alerts_count': unread_alerts_count
    })


@login_required
def mark_alert_read(request, alert_id):
    alert = get_object_or_404(Alert, id=alert_id)
    alert.is_read = True
    alert.save()
    return redirect('alerts_view')


@login_required
def clear_all_alerts(request):
    if request.user.userprofile.user_type != 'owner':
        return redirect('dashboard')
    
    if request.method == 'POST':
        Alert.objects.all().delete()
        messages.success(request, "All alerts cleared.")
    return redirect('alerts_view')

@login_required
def monitoring_dashboard(request):
    profile = request.user.userprofile
    if profile.user_type != 'owner':
        return redirect('dashboard')
    
    return render(request, 'system/monitoring_dashboard.html', {
        'username': request.user.username
    })

@login_required
@require_POST
def run_smart_features_api(request):
    """API endpoint to manually run smart features"""
    profile = request.user.userprofile
    
    if profile.user_type != 'owner':
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    results = run_smart_features_daily()
    
    return JsonResponse(results)

@login_required
def system_settings(request):
    profile = request.user.userprofile
    
    if profile.user_type != 'owner':
        return redirect('dashboard')
    
    from .models import SystemSettings
    
    # Get or create settings
    settings = SystemSettings.get_settings()
    
    if request.method == 'POST':
        # Get form data with proper handling
        admin_name = request.POST.get('admin_name', '').strip()
        admin_email = request.POST.get('admin_email', '').strip()
        admin_phone = request.POST.get('admin_phone', '').strip()
        system_name = request.POST.get('system_name', '').strip()
        
        # Validate required fields
        if not admin_name:
            messages.error(request, "⚠️ Administrator Name is required.")
            return render(request, 'system/settings.html', {
                'settings': settings,
                'username': request.user.username,
            })
        
        if not admin_email:
            messages.error(request, "⚠️ Administrator Email is required.")
            return render(request, 'system/settings.html', {
                'settings': settings,
                'username': request.user.username,
            })
        
        # Update settings (use default if empty)
        settings.admin_name = admin_name if admin_name else "System Administrator"
        settings.admin_email = admin_email if admin_email else "admin@example.com"
        settings.admin_phone = admin_phone if admin_phone else "+63 XXX XXX XXXX"
        settings.system_name = system_name if system_name else "Smart Energy Monitor"
        
        # Optional fields
        if request.POST.get('electricity_rate'):
            settings.electricity_rate = float(request.POST.get('electricity_rate'))
        if request.POST.get('late_penalty_amount'):
            settings.late_penalty_amount = float(request.POST.get('late_penalty_amount'))
        if request.POST.get('reminder_days_before'):
            settings.reminder_days_before = int(request.POST.get('reminder_days_before'))
        if request.POST.get('abnormal_threshold'):
            settings.abnormal_threshold = float(request.POST.get('abnormal_threshold'))
        
        settings.save()
        
        messages.success(request, "✅ System settings updated successfully!")
        return redirect('system_settings')
    
    return render(request, 'system/settings.html', {
        'settings': settings,
        'username': request.user.username,
    })



@login_required
def system_health(request):
    """System health check for owner (FIXED: removed duplicate decorator)"""
    profile = request.user.userprofile
    
    if profile.user_type != 'owner':
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    rooms_count = Room.objects.count()
    tenants_count = UserProfile.objects.filter(user_type='tenant').count()
    bills_count = Billing.objects.count()
    readings_count = EnergyUsage.objects.count()
    alerts_count = Alert.objects.filter(is_read=False).count()
    
    today = timezone.now().date()
    upcoming_bills = Billing.objects.filter(
        is_paid=False,
        due_date__gte=today
    ).count()
    
    overdue_bills = Billing.objects.filter(
        is_paid=False,
        due_date__lt=today
    ).count()
    
    last_reading = EnergyUsage.objects.order_by('-timestamp').first()
    last_alert = Alert.objects.order_by('-created_at').first()
    
    return JsonResponse({
        'status': 'healthy',
        'database': {
            'rooms': rooms_count,
            'tenants': tenants_count,
            'bills': bills_count,
            'readings': readings_count,
            'unread_alerts': alerts_count
        },
        'billing': {
            'upcoming': upcoming_bills,
            'overdue': overdue_bills
        },
        'activity': {
            'last_reading': last_reading.timestamp if last_reading else None,
            'last_alert': last_alert.created_at if last_alert else None
        },
        'timestamp': timezone.now()
    })

@login_required
def health_dashboard(request):
    """Health dashboard page with nice UI (for humans)"""
    profile = request.user.userprofile
    
    if profile.user_type != 'owner':
        return redirect('dashboard')
    
    rooms_count = Room.objects.count()
    tenants_count = UserProfile.objects.filter(user_type='tenant').count()
    bills_count = Billing.objects.count()
    readings_count = EnergyUsage.objects.count()
    unread_alerts = Alert.objects.filter(is_read=False).count()
    
    today = timezone.now().date()
    upcoming_bills = Billing.objects.filter(
        is_paid=False,
        due_date__gte=today
    ).count()
    
    overdue_bills = Billing.objects.filter(
        is_paid=False,
        due_date__lt=today
    ).count()
    
    last_reading = EnergyUsage.objects.order_by('-timestamp').first()
    last_alert = Alert.objects.order_by('-created_at').first()
    
    rooms_over_limit = []
    for room in Room.objects.all():
        total_usage = EnergyUsage.objects.filter(
            room=room,
            timestamp__month=timezone.now().month
        ).aggregate(total=models.Sum('kwh'))['total'] or 0
        
        if total_usage > room.limit:
            rooms_over_limit.append({
                'name': room.name,
                'usage': total_usage,
                'limit': room.limit
            })
    
    return render(request, 'system/health_dashboard.html', {
        'username': request.user.username,
        'stats': {
            'rooms': rooms_count,
            'tenants': tenants_count,
            'bills': bills_count,
            'readings': readings_count,
            'unread_alerts': unread_alerts,
            'upcoming': upcoming_bills,
            'overdue': overdue_bills,
        },
        'last_reading': last_reading,
        'last_alert': last_alert,
        'rooms_over_limit': rooms_over_limit,
        'unread_alerts_count': unread_alerts,
    })

@login_required
def edit_profile(request):
    """Allow tenants to edit their profile information"""
    from django.contrib import messages
    from django.contrib.auth.models import User
    
    profile = request.user.userprofile
    
    if profile.user_type != 'tenant':
        return redirect('dashboard')
    
    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        email = request.POST.get('email', '').strip()
        phone_number = request.POST.get('phone_number', '').strip()
        
        errors = []
        
        if not email:
            errors.append("Email is required.")
        elif User.objects.filter(email=email).exclude(id=request.user.id).exists():
            errors.append("Email already used by another account.")
        
        if errors:
            return render(request, 'user/edit_profile.html', {
                'profile': profile,
                'errors': errors,
                'first_name': first_name,
                'last_name': last_name,
                'email': email,
                'phone_number': phone_number,
                'username': request.user.username,
            })
        
        user = request.user
        user.first_name = first_name
        user.last_name = last_name
        user.email = email
        user.save()
        
        profile.phone_number = phone_number
        profile.save()
        
        messages.success(request, "✅ Profile updated successfully!")
        return redirect('tenant_dashboard')
    
    return render(request, 'user/edit_profile.html', {
        'profile': profile,
        'first_name': request.user.first_name,
        'last_name': request.user.last_name,
        'email': request.user.email,
        'phone_number': profile.phone_number or '',
        'username': request.user.username,
    })


# ============== GCASH/PAYMENT VIEWS ==============
@login_required
def create_gcash_payment(request, bill_id):
    """Create GCash payment via PayMongo"""
    profile = request.user.userprofile
    
    if profile.user_type != 'tenant':
        return redirect('dashboard')
    
    bill = get_object_or_404(Billing, id=bill_id, room=profile.room)
    
    if bill.is_paid:
        messages.warning(request, "This bill is already paid.")
        return redirect('tenant_dashboard')
    
    payment = Payment.objects.filter(bill=bill, status='pending').first()
    if not payment:
        payment = create_payment_record(bill, profile, 'gcash')
        print(f"✅ Created new payment with reference: {payment.reference_number}")
    else:
        print(f"✅ Using existing payment with reference: {payment.reference_number}")
    
    # IMPORTANT: Gamitin ang APP_BASE_URL mula sa settings
    from django.conf import settings as django_settings
    base_url = getattr(django_settings, 'APP_BASE_URL', 'http://127.0.0.1:8000')
    
    success_url = f"{base_url}/payment/success/{payment.reference_number}/"
    cancel_url = f"{base_url}/tenant/"
    
    # ✅ IMPORTANT: Isama ang reference_number sa description para makuha ng webhook
    description = f"Electricity Bill - {bill.room.name} - {bill.billing_month} - Ref: {payment.reference_number}"
    
    print(f"🔗 Success URL: {success_url}")
    print(f"🔗 Cancel URL: {cancel_url}")
    print(f"💰 Amount: {bill.cost}")
    print(f"📝 Reference: {payment.reference_number}")
    print(f"📝 Description: {description}")
    
    if not PAYMONGO_AVAILABLE:
        messages.info(request, "GCash payment is in simulation mode. Use the checkout link to test.")
        return redirect('payment_checkout_simulation', reference=payment.reference_number)
    
    try:
        paymongo = get_paymongo()
        
        result = paymongo.create_checkout_session(
            amount=bill.cost,
            description=description,  # ✅ ITO ANG BAGO - may reference_number na
            success_url=success_url,
            cancel_url=cancel_url,
            reference=payment.reference_number
        )
        
        if result.get('status') == 'success' or 'data' in result:
            checkout_url = result['data']['attributes']['checkout_url']
            print(f"🚀 Redirecting to: {checkout_url}")
            return redirect(checkout_url)
        else:
            error_msg = result.get('error', 'Unknown error')
            messages.error(request, f"Failed to create payment: {error_msg}")
            return redirect('tenant_dashboard')
            
    except Exception as e:
        print(f"❌ PayMongo error: {str(e)}")
        messages.error(request, f"Payment gateway error: {str(e)}")
        return redirect('tenant_dashboard')


@login_required
def payment_success(request, reference_number):
    """Handle successful payment callback"""
    payment = get_object_or_404(Payment, reference_number=reference_number)
    
    mark_payment_as_paid(payment, transaction_id=reference_number)
    
    messages.success(request, f"✅ Payment of ₱{payment.amount} for {payment.bill.billing_month} has been received!")
    
    return redirect('tenant_dashboard')


@login_required
def manual_paid_confirmation(request, bill_id):
    """Tenant confirms cash payment (notifies owner)"""
    profile = request.user.userprofile
    
    if profile.user_type != 'tenant':
        return redirect('dashboard')
    
    bill = get_object_or_404(Billing, id=bill_id, room=profile.room)
    
    if request.method == 'POST':
        payment = Payment.objects.filter(bill=bill, status='pending').first()
        if not payment:
            payment = create_payment_record(bill, profile, 'cash')
        
        payment.notes = request.POST.get('notes', '')
        payment.save()
        
        Alert.objects.create(
            room=bill.room,
            alert_type='billing',
            message=f"Tenant {profile.user.username} has paid ₱{bill.cost} for {bill.billing_month} via CASH. Please verify and mark as paid."
        )
        
        messages.info(request, f"Your payment for {bill.billing_month} has been recorded. The owner will verify and update your bill status.")
        return redirect('tenant_dashboard')
    
    return render(request, 'user/cash_payment_confirmation.html', {
        'bill': bill,
        'reference_number': f"PAY-{bill.room.name}-{bill.billing_month.replace(' ', '')}",
        'username': request.user.username,
    })

@login_required
def payment_method(request):
    """Display payment options for tenant"""
    profile = request.user.userprofile
    
    if profile.user_type != 'tenant':
        return redirect('dashboard')
    
    room = profile.room
    if not room:
        return redirect('tenant_dashboard')
    
    current_month = timezone.now().strftime("%B %Y")
    current_bill = Billing.objects.filter(room=room, billing_month=current_month).first()
    
    if not current_bill:
        messages.info(request, "No bill available for this month.")
        return redirect('tenant_dashboard')
    
    pending_payment = Payment.objects.filter(bill=current_bill, status='pending').first()
    
    if not pending_payment and not current_bill.is_paid:
        pending_payment = create_payment_record(current_bill, profile, 'cash')
    
    return render(request, 'user/payment_method.html', {
        'bill': current_bill,
        'pending_payment': pending_payment,
        'room': room,
        'username': request.user.username,
        'electricity_rate': get_settings().electricity_rate,
    })

@login_required
def payment_checkout_simulation(request, reference):
    """Simulate PayMongo checkout page - FIXED for testing"""
    from .models import Payment
    
    payment = get_object_or_404(Payment, reference_number=reference)
    bill = payment.bill
    
    # Para sa debugging
    print(f"\n🔍 SIMULATION CHECKOUT - Reference: {reference}")
    print(f"   Payment ID: {payment.id}")
    print(f"   Bill ID: {bill.id}")
    print(f"   Bill is_paid: {bill.is_paid}")
    print(f"   Payment status: {payment.status}")
    
    if request.method == 'POST':
        print("\n💳 POST request received - Processing payment...")
        
        # Mark as paid
        payment.status = 'paid'
        payment.paid_at = timezone.now()
        payment.transaction_id = f"SIM_{reference}"
        payment.save()
        
        # Update the bill
        bill.is_paid = True
        bill.save()
        
        # Create alert
        Alert.objects.create(
            room=bill.room,
            alert_type='billing',
            message=f"✅ Payment of ₱{payment.amount} for {bill.billing_month} has been received via SIMULATION."
        )
        
        print(f"✅ Payment marked as paid!")
        print(f"   Payment status: {payment.status}")
        print(f"   Bill is_paid: {bill.is_paid}")
        
        messages.success(request, f"✅ Payment of ₱{payment.amount} for {bill.billing_month} has been received!")
        return redirect('tenant_dashboard')
    
    return render(request, 'user/payment_checkout.html', {
        'payment': payment,
        'bill': bill,
        'reference': reference,
        'username': request.user.username,
    })

@login_required
def export_billing_csv(request):
    """Export billing history as formatted CSV file"""
    profile = request.user.userprofile
    
    if profile.user_type != 'owner':
        return redirect('dashboard')
    
    room_name = request.GET.get('room_name', '').strip()
    month_filter = request.GET.get('month', '')
    status_filter = request.GET.get('status', '')
    start_date = request.GET.get('start_date', '')
    end_date = request.GET.get('end_date', '')
    
    bills_query = Billing.objects.filter(
        room__userprofile__user_type='tenant'
    ).select_related('room').distinct()
    
    if room_name:
        bills_query = bills_query.filter(room__name__icontains=room_name)
    if month_filter:
        bills_query = bills_query.filter(billing_month=month_filter)
    if status_filter == 'paid':
        bills_query = bills_query.filter(is_paid=True)
    elif status_filter == 'unpaid':
        bills_query = bills_query.filter(is_paid=False)
    if start_date:
        try:
            start_datetime = datetime.strptime(start_date, '%Y-%m-%d')
            bills_query = bills_query.filter(created_at__date__gte=start_datetime)
        except ValueError:
            pass
    if end_date:
        try:
            end_datetime = datetime.strptime(end_date, '%Y-%m-%d')
            bills_query = bills_query.filter(created_at__date__lte=end_datetime)
        except ValueError:
            pass
    
    bills = bills_query.order_by('-billing_month', 'room__name')
    
    total_bills = bills.count()
    total_amount = sum(bill.cost for bill in bills)
    paid_bills = sum(1 for bill in bills if bill.is_paid)
    unpaid_bills = total_bills - paid_bills
    collection_rate = (paid_bills / total_bills * 100) if total_bills > 0 else 0
    
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="billing_report_{now().strftime("%Y%m%d_%H%M%S")}.csv"'
    
    writer = csv.writer(response)
    
    writer.writerow(['=' * 80])
    writer.writerow(['SMART ENERGY MONITORING SYSTEM'])
    writer.writerow(['BILLING REPORT'])
    writer.writerow([f'Generated: {now().strftime("%B %d, %Y %I:%M %p")}'])
    
    filters = []
    if room_name: filters.append(f'Room: {room_name}')
    if month_filter: filters.append(f'Month: {month_filter}')
    if status_filter: filters.append(f'Status: {status_filter.upper()}')
    if start_date: filters.append(f'From: {start_date}')
    if end_date: filters.append(f'To: {end_date}')
    
    writer.writerow([f'Filter: {", ".join(filters) if filters else "All Records"}'])
    writer.writerow(['=' * 80])
    writer.writerow([])
    
    writer.writerow(['SUMMARY STATISTICS'])
    writer.writerow(['-' * 40])
    writer.writerow([f'Total Bills:,{total_bills}'])
    writer.writerow([f'Total Revenue:,₱{total_amount:,.2f}'])
    writer.writerow([f'Paid Bills:,{paid_bills}'])
    writer.writerow([f'Unpaid Bills:,{unpaid_bills}'])
    writer.writerow([f'Collection Rate:,{collection_rate:.1f}%'])
    writer.writerow([])
    writer.writerow(['=' * 80])
    writer.writerow([])
    
    writer.writerow(['BILLING DETAILS'])
    writer.writerow(['-' * 100])
    writer.writerow([
        'Room', 'Tenant', 'Billing Month', 'kWh', 'Rate', 'Amount', 'Status', 'Due Date', 'Days'
    ])
    writer.writerow(['-' * 100])
    
    for bill in bills:
        tenant_name = 'N/A'
        try:
            tenant_profile = UserProfile.objects.get(room=bill.room, user_type='tenant')
            tenant_name = tenant_profile.user.get_full_name() or tenant_profile.user.username
        except:
            pass
        
        writer.writerow([
            bill.room.name,
            tenant_name,
            bill.billing_month,
            bill.kwh,
            f'₱23',
            f'₱{bill.cost:,.2f}',
            'PAID' if bill.is_paid else 'UNPAID',
            bill.due_date.strftime('%Y-%m-%d') if bill.due_date else '',
            bill.days_occupied if hasattr(bill, 'days_occupied') else '0'
        ])
    
    writer.writerow(['-' * 100])
    writer.writerow([f'End of Report - {total_bills} record(s)'])
    
    return response

@login_required
def billing_report_html(request):
    """Display printable HTML billing report"""
    profile = request.user.userprofile
    
    if profile.user_type != 'owner':
        return redirect('dashboard')
    
    room_name = request.GET.get('room_name', '').strip()
    month_filter = request.GET.get('month', '')
    status_filter = request.GET.get('status', '')
    start_date = request.GET.get('start_date', '')
    end_date = request.GET.get('end_date', '')
    
    bills_query = Billing.objects.filter(
        room__userprofile__user_type='tenant'
    ).select_related('room').distinct()
    
    if room_name:
        bills_query = bills_query.filter(room__name__icontains=room_name)
    if month_filter:
        bills_query = bills_query.filter(billing_month=month_filter)
    if status_filter == 'paid':
        bills_query = bills_query.filter(is_paid=True)
    elif status_filter == 'unpaid':
        bills_query = bills_query.filter(is_paid=False)
    if start_date:
        try:
            start_datetime = datetime.strptime(start_date, '%Y-%m-%d')
            bills_query = bills_query.filter(created_at__date__gte=start_datetime)
        except ValueError:
            pass
    if end_date:
        try:
            end_datetime = datetime.strptime(end_date, '%Y-%m-%d')
            bills_query = bills_query.filter(created_at__date__lte=end_datetime)
        except ValueError:
            pass
    
    bills = bills_query.order_by('-billing_month', 'room__name')
    
    total_bills = bills.count()
    total_amount = sum(bill.cost for bill in bills)
    paid_bills = sum(1 for bill in bills if bill.is_paid)
    unpaid_bills = total_bills - paid_bills
    collection_rate = (paid_bills / total_bills * 100) if total_bills > 0 else 0
    
    filter_text = "All Records"
    if room_name or month_filter or status_filter or start_date or end_date:
        filters = []
        if room_name: filters.append(f"Room: {room_name}")
        if month_filter: filters.append(f"Month: {month_filter}")
        if status_filter: filters.append(f"Status: {status_filter.upper()}")
        if start_date: filters.append(f"From: {start_date}")
        if end_date: filters.append(f"To: {end_date}")
        filter_text = ", ".join(filters)
    
    return render(request, 'system/billing_report.html', {
        'bills': bills,
        'total_bills': total_bills,
        'total_amount': total_amount,
        'paid_bills': paid_bills,
        'unpaid_bills': unpaid_bills,
        'collection_rate': collection_rate,
        'filter_text': filter_text,
        'room_name': room_name,
        'month_filter': month_filter,
        'status_filter': status_filter,
        'start_date': start_date,
        'end_date': end_date,
        'username': request.user.username,
    })

@login_required
def activity_log(request):
    """View for owner to see all system activities"""
    profile = request.user.userprofile
    
    if profile.user_type != 'owner':
        return redirect('dashboard')
    
    logs = ActivityLog.objects.all()
    
    action_filter = request.GET.get('action', '')
    if action_filter:
        logs = logs.filter(action=action_filter)
    
    user_type_filter = request.GET.get('user_type', '')
    if user_type_filter:
        logs = logs.filter(user_type=user_type_filter)
    
    search = request.GET.get('search', '')
    if search:
        logs = logs.filter(user__username__icontains=search)
    
    from django.core.paginator import Paginator
    paginator = Paginator(logs, 50)
    page_number = request.GET.get('page')
    logs_page = paginator.get_page(page_number)
    
    total_actions = ActivityLog.objects.count()
    recent_24h = ActivityLog.objects.filter(created_at__gte=timezone.now() - timezone.timedelta(hours=24)).count()
    
    action_counts = {}
    for action in dict(ActivityLog.ACTION_TYPES).keys():
        action_counts[action] = ActivityLog.objects.filter(action=action).count()
    
    return render(request, 'system/activity_log.html', {
        'logs': logs_page,
        'total_actions': total_actions,
        'recent_24h': recent_24h,
        'action_counts': action_counts,
        'current_filter': action_filter,
        'user_type_filter': user_type_filter,
        'search': search,
        'username': request.user.username,
        'unread_alerts_count': Alert.objects.filter(is_read=False).count(),
    })