from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.utils import timezone
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
from django.db import models 
# Import models
from .models import Room, Billing, Alert, UserProfile, SystemSettings, EnergyUsage, TenantAssignment

logger = logging.getLogger(__name__)

# ============== HELPER FUNCTIONS ==============
def get_settings():
    return SystemSettings.get_settings()

def get_last_day_of_month(year, month):
    return calendar.monthrange(year, month)[1]


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


from django.conf import settings as django_settings  # I-add ito sa taas

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
                    from_email=django_settings.EMAIL_HOST_USER,  # <--- ITO ANG BAGO
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
        
        user = authenticate(request, username=username, password=password)
        
        if user is not None:
            login(request, user)
            
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
    
    return render(request, 'system/login.html', {'login_error': error})


def logout_view(request):
    logout(request)
    return redirect('login_view')

# ============== SMART FEATURES ==============

def detect_abnormal_usage():
    """
    Detect abnormal electricity usage patterns
    Run this daily to check for anomalies
    """
    settings = get_settings()
    
    print("\n🔍 Checking for abnormal usage patterns...")
    print("-" * 50)
    
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    
    # Kunin lahat ng rooms
    rooms = Room.objects.all()
    alerts_created = 0
    
    for room in rooms:
        # Kunin ang historical data (last 30 days, excluding yesterday)
        thirty_days_ago = today - timedelta(days=30)
        historical_data = EnergyUsage.objects.filter(
            room=room,
            timestamp__date__gte=thirty_days_ago,
            timestamp__date__lt=yesterday
        ).values_list('kwh', flat=True)
        
        # Kunin ang yesterday's usage
        yesterday_usage = EnergyUsage.objects.filter(
            room=room,
            timestamp__date=yesterday
        ).aggregate(total=Sum('kwh'))['total'] or 0
        
        # Kailangan ng at least 7 days of data para mag-compare
        if len(historical_data) < 7:
            continue
        
        # Compute average and standard deviation
        avg_usage = sum(historical_data) / len(historical_data)
        
        # Calculate standard deviation
        if len(historical_data) > 1:
            variance = sum((x - avg_usage) ** 2 for x in historical_data) / len(historical_data)
            std_dev = variance ** 0.5
        else:
            std_dev = avg_usage * 0.3  # Fallback
        
        # Check for abnormalities using threshold from settings
        threshold = settings.abnormal_threshold
        
        if yesterday_usage > 0:
            # Rule 1: More than threshold standard deviations from average
            if yesterday_usage > avg_usage + (threshold * std_dev):
                percent_increase = ((yesterday_usage - avg_usage) / avg_usage) * 100
                
                Alert.objects.create(
                    room=room,
                    alert_type='abnormal_usage',
                    message=f"⚠️ Abnormal usage detected! Yesterday's consumption ({yesterday_usage:.2f} kWh) is {percent_increase:.1f}% higher than your average ({avg_usage:.2f} kWh)."
                )
                alerts_created += 1
                print(f"  ⚠️ {room.name}: {percent_increase:.1f}% increase")
            
            # Rule 2: Sudden spike (more than 3x average)
            elif yesterday_usage > avg_usage * 3:
                Alert.objects.create(
                    room=room,
                    alert_type='abnormal_usage',
                    message=f"🚨 Sudden spike detected! Yesterday's usage ({yesterday_usage:.2f} kWh) is 3x your normal consumption."
                )
                alerts_created += 1
                print(f"  🚨 {room.name}: 3x spike detected")
    
    print("-" * 50)
    print(f"✅ Created {alerts_created} abnormal usage alerts")
    return alerts_created


def check_high_consumption():
    """
    Check for rooms approaching or exceeding their limit
    """
    print("\n📊 Checking for high consumption...")
    print("-" * 50)
    
    today = datetime.now()
    current_month = today.month
    current_year = today.year
    
    rooms = Room.objects.all()
    alerts_created = 0
    
    for room in rooms:
        # Kunin ang total usage for current month
        total_usage = EnergyUsage.objects.filter(
            room=room,
            timestamp__year=current_year,
            timestamp__month=current_month
        ).aggregate(total=Sum('kwh'))['total'] or 0
        
        # Calculate percentage of limit used
        if room.limit > 0:
            percentage = (total_usage / room.limit) * 100
            
            # Alert at different thresholds
            if percentage >= 90 and percentage < 100:
                Alert.objects.create(
                    room=room,
                    alert_type='high_consumption',
                    message=f"⚠️ You've used {percentage:.1f}% of your monthly limit ({total_usage:.1f}/{room.limit} kWh). Consider reducing consumption."
                )
                alerts_created += 1
                print(f"  ⚠️ {room.name}: {percentage:.1f}% of limit")
            
            elif percentage >= 100:
                Alert.objects.create(
                    room=room,
                    alert_type='over_limit',
                    message=f"🚨 You've EXCEEDED your monthly limit! Current: {total_usage:.1f} kWh, Limit: {room.limit} kWh"
                )
                alerts_created += 1
                print(f"  🚨 {room.name}: EXCEEDED limit!")
    
    print("-" * 50)
    print(f"✅ Created {alerts_created} consumption alerts")
    return alerts_created


def apply_late_payment_penalty(penalty_amount=None):
    """
    Apply late payment penalty to overdue bills
    Run this daily
    """
    settings = get_settings()
    
    # Use settings value if penalty_amount not provided
    if penalty_amount is None:
        penalty_amount = settings.late_penalty_amount
    
    print("\n💰 Checking for late payments...")
    print("-" * 50)
    
    today = datetime.now().date()
    
    # Kunin ang unpaid bills na overdue na
    overdue_bills = Billing.objects.filter(
        is_paid=False,
        due_date__lt=today
    )
    
    penalties_applied = 0
    
    for bill in overdue_bills:
        # Check if penalty already applied (you might want to add a field for this)
        # For now, we'll just add an alert
        
        # Create alert for late payment
        days_late = (today - bill.due_date).days
        
        Alert.objects.create(
            room=bill.room,
            alert_type='late_payment',
            message=f"⚠️ Your bill for {bill.billing_month} is {days_late} days late. A penalty of ₱{penalty_amount} will be applied to your next bill."
        )
        
        # Optional: I-add ang penalty sa next bill
        # You can implement this logic later
        
        penalties_applied += 1
        print(f"  ⚠️ {bill.room.name}: {bill.billing_month} - {days_late} days late")
    
    print("-" * 50)
    print(f"✅ Created {penalties_applied} late payment alerts")
    return penalties_applied


def run_smart_features_daily():
    """
    Run all smart features in one go
    This can be scheduled daily
    """
    print("\n" + "="*60)
    print("🤖 RUNNING SMART FEATURES")
    print("="*60)
    
    # 1. Check for abnormal usage
    abnormal = detect_abnormal_usage()
    
    # 2. Check high consumption
    high_cons = check_high_consumption()
    
    # 3. Apply late payment penalties
    late = apply_late_payment_penalty()
    
    print("\n" + "="*60)
    print(f"📊 SUMMARY: {abnormal} abnormal, {high_cons} high consumption, {late} late payments")
    print("="*60)
    
    return {
        'abnormal': abnormal,
        'high_consumption': high_cons,
        'late_payments': late
    }


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
        
        # Validate
        if not name:
            messages.error(request, "Room name is required.")
            return render(request, 'system/add_room.html')
        
        # Create new room
        room = Room.objects.create(
            name=name,
            usage=usage,
            limit=limit,
            power_status=True  # Default to ON
        )
        
        # Create alert for new room
        #Alert.objects.create(
        #    alert_type='power_on',
        #    message=f"New room added: {name}",
        #    room=room
        #)
        
        messages.success(request, f"Room '{name}' added successfully!")

        return redirect('dashboard')
    
    # Get unread alerts count
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
    
    if request.method == 'POST':
        room.name = request.POST.get('name')
        room.limit = float(request.POST.get('limit', 200))
        room.usage = float(request.POST.get('usage', room.usage))
        room.save()
        
        Alert.objects.create(
            alert_type='billing',
            message=f"Room {room.name} updated",
            room=room
        )

        return redirect('dashboard')
    
    # Get unread alerts count
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
    
    # Remove any tenants from this room
    UserProfile.objects.filter(room=room, user_type='tenant').update(room=None)
    
    # Create alert before deleting
    Alert.objects.create(
        alert_type='power_off',
        message=f"Room deleted: {room_name}",
        room=None  # No room since it's being deleted
    )
    
    room.delete()
    return redirect('dashboard')


# ============== TENANT ASSIGNMENT ==============
@login_required
def assign_tenant(request, room_id):
    if request.user.userprofile.user_type != 'owner':
        messages.error(request, "You don't have permission to do that.")
        return redirect('dashboard')
    
    if request.method == 'POST':
        tenant_id = request.POST.get('tenant_id')
        move_in_date = request.POST.get('move_in_date')  # <--- Add this
        room = get_object_or_404(Room, id=room_id)
        
        if tenant_id:
            tenant_profile = get_object_or_404(UserProfile, id=tenant_id, user_type='tenant')
            
            # Deactivate previous assignment for this room
            TenantAssignment.objects.filter(room=room, is_active=True).update(is_active=False)
            
            # Create new assignment
            assignment = TenantAssignment.objects.create(
                tenant=tenant_profile,
                room=room,
                move_in_date=move_in_date or date.today(),
                is_active=True
            )
            
            # Assign room to tenant profile
            tenant_profile.room = room
            tenant_profile.save()
            
            Alert.objects.create(
                room=room,
                alert_type='tenant_assigned',
                message=f"Tenant {tenant_profile.user.username} assigned to room {room.name} starting {assignment.move_in_date}"
            )
        else:
            # Remove tenant
            TenantAssignment.objects.filter(room=room, is_active=True).update(is_active=False)
            UserProfile.objects.filter(room=room, user_type='tenant').update(room=None)
            messages.success(request, f"Tenant removed from {room.name}.")
    
    return redirect('dashboard')


@login_required
def remove_tenant(request, room_id):
    if request.user.userprofile.user_type != 'owner':
        messages.error(request, "You don't have permission to do that.")
        return redirect('dashboard')
    
    room = get_object_or_404(Room, id=room_id)
    
    # Find and remove tenant
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
    
    # Remove unread_alerts_count if not needed
    # unread_alerts_count = Alert.objects.filter(is_read=False).count()
    
    return render(request, 'system/tenant_list.html', {
        'tenants': tenants,
        'total_tenants': total_tenants,
        'with_room': with_room,
        'without_room': without_room,
        # 'unread_alerts_count': unread_alerts_count,  # Tinanggal
        'username': request.user.username,
    })


# ============== BILLING VIEWS ==============
@login_required
def billing_view(request):
    settings = get_settings()
    
    if request.user.userprofile.user_type != 'owner':
        return redirect('tenant_dashboard')
    
    current_month = timezone.now().strftime("%B %Y")
    
    # Compute due date
    from datetime import date
    import calendar
    today = date.today()
    last_day = calendar.monthrange(today.year, today.month)[1]
    due_date = date(today.year, today.month, last_day)
    
    # Debug: I-print para makita
    print(f"Current month: {current_month}")
    print(f"Due date: {due_date}")
    
    rooms = Room.objects.all()
    for room in rooms:
        # Check if room has an active tenant
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
            # If no tenant, delete any existing bill for this month
            Billing.objects.filter(room=room, billing_month=current_month).delete()
    
    # Filter: Only show bills for rooms with tenants
    bills = Billing.objects.filter(
        billing_month=current_month,
        room__userprofile__user_type='tenant',  # May tenant ang room
        room__userprofile__isnull=False
    ).select_related('room').distinct()
    
    total_kwh = sum(bill.kwh for bill in bills)
    total_cost = sum(bill.cost for bill in bills)
    paid_count = sum(1 for bill in bills if bill.is_paid)
    
    # Get unread alerts count
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
    
    # Check if room has a tenant before allowing to mark as paid
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
    
    # Get all search parameters
    room_name = request.GET.get('room_name', '').strip()
    month_filter = request.GET.get('month', '')
    status_filter = request.GET.get('status', '')
    start_date = request.GET.get('start_date', '')
    end_date = request.GET.get('end_date', '')
    
    # Base query - ONLY rooms with tenants (added filter)
    bills_query = Billing.objects.filter(
        room__userprofile__user_type='tenant'  # May tenant ang room
    ).select_related('room').distinct()
    
    # Apply Room Name filter (case-insensitive search)
    if room_name:
        bills_query = bills_query.filter(
            Q(room__name__icontains=room_name)
        )
    
    # Apply Month filter
    if month_filter:
        bills_query = bills_query.filter(billing_month=month_filter)
    
    # Apply Status filter
    if status_filter == 'paid':
        bills_query = bills_query.filter(is_paid=True)
    elif status_filter == 'unpaid':
        bills_query = bills_query.filter(is_paid=False)
    
    # Apply Date Range filter
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
    
    # Order results
    all_bills = bills_query.order_by('-billing_month', 'room__name')
    
    # Get unique months for dropdown - ONLY rooms with tenants
    available_months = Billing.objects.filter(
        room__userprofile__user_type='tenant'
    ).values_list('billing_month', flat=True).distinct().order_by('-billing_month')
    
    # Group bills by month for display
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
    
    # Sort months in descending order
    def month_sort_key(month_str):
        try:
            # Try to parse as date for proper sorting
            return datetime.strptime(month_str, "%B %Y")
        except:
            return datetime.min
    
    sorted_months = dict(sorted(
        bills_by_month.items(), 
        key=lambda x: month_sort_key(x[0]), 
        reverse=True
    ))
    
    # Prepare search params for template
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
    # Full alerts list (for alerts page)
    all_alerts = Alert.objects.order_by('-created_at')

    # Mark all as read
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
    
    # Only owners can run this
    if profile.user_type != 'owner':
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    results = run_smart_features_daily()
    
    return JsonResponse(results)

@login_required
def system_settings(request):
    """System settings page for owner"""
    profile = request.user.userprofile
    
    # Only owners can access
    if profile.user_type != 'owner':
        return redirect('dashboard')
    
    from .models import SystemSettings
    settings = SystemSettings.get_settings()
    
    if request.method == 'POST':
        # Update settings
        settings.electricity_rate = float(request.POST.get('electricity_rate', 23))
        settings.late_penalty_amount = float(request.POST.get('late_penalty_amount', 50))
        settings.reminder_days_before = int(request.POST.get('reminder_days_before', 3))
        settings.abnormal_threshold = float(request.POST.get('abnormal_threshold', 2))
        settings.system_name = request.POST.get('system_name', 'Smart Energy Monitor')
        settings.contact_email = request.POST.get('contact_email', 'admin@example.com')
        settings.contact_phone = request.POST.get('contact_phone', '+63 XXX XXX XXXX')
        settings.save()
        
        # ADD extra_tags para mafilter sa template
        messages.success(request, "✅ System settings updated successfully!", extra_tags='settings')
        return redirect('system_settings')
    
    return render(request, 'system/settings.html', {
        'settings': settings,
        'username': request.user.username,
        'unread_alerts_count': Alert.objects.filter(is_read=False).count(),
    })

@login_required
@login_required
def system_health(request):
    """System health check for owner"""
    profile = request.user.userprofile
    
    if profile.user_type != 'owner':
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    # Check database
    rooms_count = Room.objects.count()
    tenants_count = UserProfile.objects.filter(user_type='tenant').count()
    bills_count = Billing.objects.count()
    readings_count = EnergyUsage.objects.count()
    alerts_count = Alert.objects.filter(is_read=False).count()
    
    # Check upcoming bills
    today = timezone.now().date()
    upcoming_bills = Billing.objects.filter(
        is_paid=False,
        due_date__gte=today
    ).count()
    
    overdue_bills = Billing.objects.filter(
        is_paid=False,
        due_date__lt=today
    ).count()
    
    # Check recent activity
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
    
    # Only owners can access
    if profile.user_type != 'owner':
        return redirect('dashboard')
    
    # Kunin ang data (same sa API)
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
    
    # Kunin din ang rooms with issues
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
    
    # Only tenants can access this
    if profile.user_type != 'tenant':
        return redirect('dashboard')
    
    if request.method == 'POST':
        # Get form data
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        email = request.POST.get('email', '').strip()
        phone_number = request.POST.get('phone_number', '').strip()
        
        # Validate
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
        
        # Update user info
        user = request.user
        user.first_name = first_name
        user.last_name = last_name
        user.email = email
        user.save()
        
        # Update profile info
        profile.phone_number = phone_number
        profile.save()
        
        messages.success(request, "✅ Profile updated successfully!")
        return redirect('tenant_dashboard')
    
    # GET request - show form with current data
    return render(request, 'user/edit_profile.html', {
        'profile': profile,
        'first_name': request.user.first_name,
        'last_name': request.user.last_name,
        'email': request.user.email,
        'phone_number': profile.phone_number or '',
        'username': request.user.username,
    })