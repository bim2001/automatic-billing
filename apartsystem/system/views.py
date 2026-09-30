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
from .models import Room, Billing, Alert, UserProfile, SystemSettings, EnergyUsage, Payment, TenantAssignment, ActivityLog, APIToken
from django.http import HttpResponse
import csv
from django.db import connection
from dateutil.relativedelta import relativedelta

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


def get_client_ip(request):
    forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def log_activity(request, action, description, user=None):
    try:
        actor = user or getattr(request, 'user', None)
        if not actor or not actor.is_authenticated:
            actor = None

        user_type = 'tenant'
        if actor and hasattr(actor, 'userprofile'):
            user_type = actor.userprofile.user_type
        elif actor and (actor.is_staff or actor.is_superuser):
            user_type = 'owner'

        ActivityLog.objects.create(
            user=actor,
            user_type=user_type,
            action=action,
            description=description,
            ip_address=get_client_ip(request)
        )
    except Exception as exc:
        logger.warning("Activity log skipped: %s", exc)


def get_active_assignment(room):
    return TenantAssignment.objects.filter(room=room, is_active=True).select_related('tenant__user').first()


def get_bill_cycle_details(assignment, target_date=None):
    target_date = target_date or timezone.now().date()
    if not assignment:
        return {
            'move_in_date': None,
            'cycle_start': None,
            'cycle_due_date': None,
            'days_occupied': 0,
        }

    cycle_start = assignment.move_in_date
    cycle_due_date = assignment.get_due_date()
    while cycle_due_date and target_date >= cycle_due_date + relativedelta(months=1):
        cycle_start = cycle_due_date
        cycle_due_date = cycle_due_date + relativedelta(months=1)

    cycle_end = min(target_date, cycle_due_date) if cycle_due_date else target_date
    days_occupied = max((cycle_end - cycle_start).days + 1, 0)

    return {
        'move_in_date': assignment.move_in_date,
        'cycle_start': cycle_start,
        'cycle_due_date': cycle_due_date,
        'days_occupied': days_occupied,
    }


def attach_bill_display_data(bill, assignment=None):
    assignment = assignment or bill.tenant_assignment or get_active_assignment(bill.room)
    cycle = get_bill_cycle_details(assignment, timezone.now().date())
    bill.active_assignment = assignment
    if assignment:
        bill.tenant_name = assignment.tenant.user.get_full_name() or assignment.tenant.user.username
    else:
        bill.tenant_name = bill.room.get_tenant_name()
    bill.move_in_date_display = cycle['move_in_date']
    bill.cycle_start_display = cycle['cycle_start']
    bill.due_date_display = cycle['cycle_due_date'] or bill.due_date
    bill.days_occupied_display = cycle['days_occupied'] or bill.days_occupied
    return bill


def get_previous_bill_summary(bill):
    previous = Billing.objects.filter(
        room=bill.room,
        created_at__lt=bill.created_at
    ).order_by('-created_at').first()

    if not previous:
        return None

    return {
        'id': previous.id,
        'billing_month': previous.billing_month,
        'kwh': round(previous.kwh, 2),
        'amount': round(previous.cost, 2),
        'status': 'PAID' if previous.is_paid else 'UNPAID',
        'is_paid': previous.is_paid,
    }

def get_last_day_of_month(year, month):
    return calendar.monthrange(year, month)[1]

def generate_reference_number(bill):
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    room_code = bill.room.name.replace(' ', '').upper()[:10]
    amount_hash = hashlib.md5(str(bill.cost).encode()).hexdigest()[:6]
    return f"PAY-{room_code}-{timestamp}-{amount_hash}"

def create_payment_record(bill, tenant, payment_method):
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
    payment.status = 'paid'
    payment.paid_at = timezone.now()
    if transaction_id:
        payment.transaction_id = transaction_id
    payment.save()
    
    bill = payment.bill
    bill.is_paid = True
    bill.save()
    
    Alert.objects.create(
        room=bill.room,
        alert_type='billing',
        message=f"✅ Payment received for {bill.billing_month} via {payment.get_payment_method_display()}. Reference: {payment.reference_number}"
    )
    
    return payment

# ============== PAYMONGO WEBHOOK ==============
@csrf_exempt
def paymongo_webhook(request):
    if request.method != 'POST':
        return JsonResponse({"error": "Method not allowed"}, status=405)
    
    try:
        from .api import verify_paymongo_signature
        if not verify_paymongo_signature(request):
            return JsonResponse({"error": "Invalid webhook signature"}, status=401)

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
        
        reference_number = None
        if description:
            import re
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
    profile = request.user.userprofile
    
    if profile.user_type != 'owner':
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    settings = get_settings()
    rooms = Room.objects.all()
    today = datetime.now()
    year = today.year
    month = today.month
    
    room_stats = []
    total_building_usage = 0
    occupied_rooms_count = 0
    
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
        
        if room.is_occupied():
            occupied_rooms_count += 1
    
    room_stats.sort(key=lambda x: x['usage'], reverse=True)
    
    if occupied_rooms_count > 0:
        avg_per_room = total_building_usage / occupied_rooms_count
    else:
        avg_per_room = 0
    
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
        'occupied_rooms': occupied_rooms_count,
        'total_usage': round(total_building_usage, 2),
        'avg_per_room': round(avg_per_room, 2),
        'room_stats': room_stats[:5],
        'daily_building': daily_building,
        'days': days,
        'month': today.strftime("%B %Y")
    })

@login_required
def get_room_usage_data(request):
    profile = request.user.userprofile
    
    if profile.user_type != 'tenant':
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    room = profile.room
    if not room:
        return JsonResponse({'error': 'No room assigned'}, status=404)
    
    today = datetime.now()
    year = today.year
    month = today.month
    
    daily_usage = []
    days_in_month = []
    
    readings = EnergyUsage.objects.filter(
        room=room,
        timestamp__year=year,
        timestamp__month=month
    ).order_by('timestamp')
    
    from collections import defaultdict
    daily_totals = defaultdict(float)
    
    for reading in readings:
        day = reading.timestamp.day
        daily_totals[day] += reading.kwh
    
    for day in range(1, 32):
        if day in daily_totals:
            daily_usage.append(round(daily_totals[day], 2))
            days_in_month.append(f"Day {day}")
    
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
    
    print(f"DEBUG: Room {room.name} - Current month total: {current_month_total}")
    
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
    def get_last_day_of_month(year, month):
        return calendar.monthrange(year, month)[1]
    
    if year is None or month is None:
        today = datetime.now()
        year = today.year
        month = today.month
    
    month_name = datetime(year, month, 1).strftime("%B %Y")
    month_start = date(year, month, 1)
    
    current_date = timezone.now().date()
    
    if year == current_date.year and month == current_date.month:
        month_end = current_date
    else:
        month_end = date(
            year,
            month,
            get_last_day_of_month(year, month)
        )
    
    settings = SystemSettings.get_settings()
    electricity_rate = settings.electricity_rate
    
    print(f"\n📊 Generating bills for {month_name}...")
    print(f"   Period: {month_start} to {month_end}")
    print("=" * 60)
    
    rooms = Room.objects.all()
    
    bills_created = 0
    bills_updated = 0
    
    for room in rooms:
        assignment = TenantAssignment.objects.filter(
            room=room,
            is_active=True,
            move_in_date__lte=month_end
        ).filter(
            Q(move_out_date__isnull=True) |
            Q(move_out_date__gte=month_start)
        ).first()
        
        if not assignment:
            Billing.objects.filter(
                room=room,
                billing_month=month_name
            ).delete()
            print(f"❌ {room.name}: No tenant - Bill deleted")
            continue
        
        days_occupied = assignment.days_occupied_in_month(year, month)
        
        if days_occupied == 0:
            Billing.objects.filter(
                room=room,
                billing_month=month_name
            ).delete()
            print(f"⚠️ {room.name}: Days occupied 0 - Bill deleted")
            continue
        
        total_kwh = EnergyUsage.objects.filter(
            room=room,
            timestamp__year=year,
            timestamp__month=month
        ).aggregate(total=Sum('kwh'))['total'] or 0
        
        if year == current_date.year and month == current_date.month:
            total_days = current_date.day
        else:
            total_days = get_last_day_of_month(year, month)
        
        prorated_kwh = (total_kwh / total_days) * days_occupied if total_days > 0 else 0
        prorated_cost = prorated_kwh * electricity_rate
        due_date = assignment.get_due_date()
        
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
        
        print(f"{status}: {room.name} - {assignment.tenant.user.username}")
        print(f"   Move-in: {assignment.move_in_date}")
        print(f"   Days Occupied: {days_occupied}/{total_days}")
        print(f"   kWh: {total_kwh:.2f}")
        print(f"   Prorated kWh: {prorated_kwh:.2f}")
        print(f"   Amount: ₱{prorated_cost:.2f}")
        print(f"   Due Date: {due_date}")
        print("-" * 40)
    
    print("=" * 60)
    print(f"✅ Done! Created: {bills_created}, Updated: {bills_updated}")
    
    return bills_created, bills_updated

def send_payment_reminders(days_before_due=3, test_mode=False):
    today = timezone.now().date()
    reminder_date = today + timedelta(days=days_before_due)
    
    print(f"\nCHECKING BILLS DUE ON: {reminder_date} (in {days_before_due} days)")
    print("=" * 60)
    
    bills = Billing.objects.filter(
        due_date=reminder_date,
        is_paid=False,
        reminder_sent=False
    )
    
    if not bills.exists():
        print("No bills to process")
        return {'sent': 0, 'failed': 0, 'skipped': 0}
    
    print(f"Found {bills.count()} bill(s) to process")
    print("-" * 60)
    
    sent_count = 0
    failed_count = 0
    skipped_count = 0
    
    for bill in bills:
        try:
            tenant_profile = UserProfile.objects.get(room=bill.room, user_type='tenant')
            tenant = tenant_profile.user
            tenant_email = tenant.email
            tenant_name = tenant.get_full_name() or tenant.username
            
            if not tenant_email:
                print(f"SKIPPED: {bill.room.name} - No email for {tenant_name}")
                skipped_count += 1
                continue
            
            days_remaining = (bill.due_date - today).days
            
            subject = f"🧾 Bill Reminder: {bill.billing_month} due in {days_remaining} days"
            
            message = f"""
Hi {tenant_name},

This is a reminder about your electricity bill.

━━━━━━━━━━━━━━━━━━━━━━━━
Room: {bill.room.name}
Billing Month: {bill.billing_month}
Amount Due: ₱{bill.cost:.2f}
Due Date: {bill.due_date}
Days Remaining: {days_remaining}
━━━━━━━━━━━━━━━━━━━━━━━━

Please settle your payment before the due date.

If you have already paid, please ignore this message.

Thank you,
Smart Energy Monitor System
            """
            
            if test_mode:
                print(f"TEST MODE - Would send to: {tenant_email}")
                print(f"   Subject: {subject}")
                print(f"   Message: {message[:100]}...")
                sent_count += 1
            else:
                send_mail(
                    subject=subject,
                    message=message,
                    from_email=django_settings.EMAIL_HOST_USER,
                    recipient_list=[tenant_email],
                    fail_silently=False,
                )
                
                bill.reminder_sent = True
                bill.save(update_fields=['reminder_sent'])
                
                print(f"SENT: {bill.room.name} - {tenant_email}")
                sent_count += 1
            
        except UserProfile.DoesNotExist:
            print(f"FAILED: {bill.room.name} - No tenant assigned")
            failed_count += 1
        except Exception as e:
            print(f"FAILED: {bill.room.name} - {str(e)}")
            failed_count += 1
    
    print("-" * 60)
    print(f"SUMMARY: Sent: {sent_count}, Failed: {failed_count}, Skipped: {skipped_count}")
    
    return {
        'sent': sent_count,
        'failed': failed_count,
        'skipped': skipped_count
    }


from django.core.mail import send_mail
from django.conf import settings  # <-- I-ADD ITO SA TAAS

def send_approval_email(user):
    """Send email notification to tenant when approved"""
    from django.core.mail import send_mail
    
    subject = "✅ Your Smart Energy Account Has Been Approved!"
    message = f"""
Hi {user.get_full_name() or user.username},

Good news! Your tenant registration has been APPROVED by the owner.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔑 LOGIN DETAILS:
   Username: {user.username}
   Email: {user.email}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

You can now log in to your dashboard using your email or username.

👉 Login here: http://127.0.0.1:8000/login/

Once logged in, you will be able to:
✓ View your real-time electricity consumption
✓ See your monthly bills
✓ Receive payment reminders
✓ Pay via GCash

If you haven't been assigned a room yet, the owner will assign one soon.

Thank you for choosing Smart Energy Monitoring System!

Best regards,
Smart Energy Team
"""
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.EMAIL_HOST_USER,
            recipient_list=[user.email],
            fail_silently=False,
        )
        print(f"✅ Approval email sent to {user.email}")
    except Exception as e:
        print(f"❌ Failed to send email: {e}")


# ============== AUTHENTICATION VIEWS ==============
import random
from django.contrib import messages
from django.core.files.storage import FileSystemStorage
import os

import re
import os
from django.core.files.storage import FileSystemStorage
from django.conf import settings

def clean_filename(filename):
    """Automatically clean filename - remove spaces, special characters, and random suffixes"""
    # Get file extension
    name, ext = os.path.splitext(filename)
    
    # Remove spaces (replace with underscore)
    name = name.replace(' ', '_')
    
    # Remove parentheses and other special characters (keep letters, numbers, underscore, dot)
    name = re.sub(r'[^a-zA-Z0-9_.-]', '', name)
    
    # Remove duplicate underscores
    name = re.sub(r'_+', '_', name)
    
    # Remove "download" word if present (common from browsers)
    name = name.replace('download', '')
    name = name.replace('_download', '')
    
    # Remove numbers in parentheses like (1), (2), etc.
    name = re.sub(r'\([0-9]+\)', '', name)
    
    # Clean up any leftover underscores at ends
    name = name.strip('_')
    
    # If name becomes empty, use a default
    if not name:
        name = 'upload'
    
    return f"{name}{ext}"


def register_tenant(request):
    if request.method == 'POST':
        # Step 1: Account
        email = request.POST.get('email')
        password = request.POST.get('password')
        password2 = request.POST.get('password2')
        
        # Step 2: Personal Details
        first_name = request.POST.get('first_name', '').strip()
        middle_name = request.POST.get('middle_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        phone_number = request.POST.get('phone_number', '').strip()
        emergency_person = request.POST.get('emergency_person', '').strip()
        emergency_number = request.POST.get('emergency_number', '').strip()
        
        # Step 3: Screening
        occupants = request.POST.get('occupants', 1)
        employment_status = request.POST.get('employment_status', '')
        
        # Step 4: Consent
        agree_terms = request.POST.get('agree_terms') == 'on'
        agree_privacy = request.POST.get('agree_privacy') == 'on'
        
        # Validation
        errors = []
        
        if not email:
            errors.append("Email is required")
        elif User.objects.filter(email=email).exists():
            errors.append("Email already registered")
        
        if not password:
            errors.append("Password is required")
        elif len(password) < 6:
            errors.append("Password must be at least 6 characters")
        elif password != password2:
            errors.append("Passwords do not match")
        
        if not first_name:
            errors.append("First name is required")
        if not last_name:
            errors.append("Last name is required")
        if not phone_number:
            errors.append("Mobile number is required")
        
        if not agree_terms or not agree_privacy:
            errors.append("You must agree to the terms and privacy policy")
        
        if errors:
            return render(request, 'system/login.html', {
                'register_error': ' | '.join(errors)
            })
        
        # Create username from email
        username = email.split('@')[0]
        while User.objects.filter(username=username).exists():
            username = username + str(random.randint(1, 999))
        
        # Create user
        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name
        )
        
        # ========== FILE UPLOADS WITH AUTO CLEAN ==========
        valid_id_path = ''
        selfie_path = ''
        
        # Create media directories if they don't exist
        media_root = settings.MEDIA_ROOT
        ids_dir = os.path.join(media_root, 'ids')
        selfies_dir = os.path.join(media_root, 'selfies')
        
        os.makedirs(ids_dir, exist_ok=True)
        os.makedirs(selfies_dir, exist_ok=True)
        
        # Handle Valid ID upload
        if request.FILES.get('valid_id'):
            valid_id_file = request.FILES['valid_id']
            original_name = valid_id_file.name
            clean_name = clean_filename(original_name)
            safe_filename = f"{username}_id_{clean_name}"
            
            file_path = os.path.join('ids', safe_filename)
            full_path = os.path.join(media_root, file_path)
            
            # Save the file
            with open(full_path, 'wb+') as destination:
                for chunk in valid_id_file.chunks():
                    destination.write(chunk)
            
            valid_id_path = file_path
            print(f"✅ ID saved: {original_name} → {safe_filename}")
        
        # Handle Selfie upload
        if request.FILES.get('selfie'):
            selfie_file = request.FILES['selfie']
            original_name = selfie_file.name
            clean_name = clean_filename(original_name)
            safe_filename = f"{username}_selfie_{clean_name}"
            
            file_path = os.path.join('selfies', safe_filename)
            full_path = os.path.join(media_root, file_path)
            
            # Save the file
            with open(full_path, 'wb+') as destination:
                for chunk in selfie_file.chunks():
                    destination.write(chunk)
            
            selfie_path = file_path
            print(f"✅ Selfie saved: {original_name} → {safe_filename}")
        
        # Update profile
        profile = user.userprofile
        profile.middle_name = middle_name
        profile.phone_number = phone_number
        profile.emergency_contact_person = emergency_person
        profile.emergency_contact_number = emergency_number
        profile.number_of_occupants = occupants
        profile.employment_status = employment_status
        profile.valid_id_file = valid_id_path
        profile.selfie_verification_file = selfie_path
        profile.agreed_to_terms = agree_terms
        profile.agreed_to_privacy = agree_privacy
        profile.is_approved = False
        profile.user_type = 'tenant'
        profile.save()
        
        messages.success(request, "Registration complete! Your account is pending approval by the owner. You will receive an email once approved.")
        return redirect('login_view')
    
    return redirect('login_view')

def login_view(request):
    error = None
    
    if request.method == 'POST':
        username_or_email = request.POST.get('username')
        password = request.POST.get('password')
        
        if not username_or_email or not password:
            error = "Username/Email and password are required."
        else:
            # Check if input is email or username
            if '@' in username_or_email:
                # Try to find user by email
                try:
                    user_obj = User.objects.get(email=username_or_email)
                    username = user_obj.username
                except User.DoesNotExist:
                    username = username_or_email
            else:
                username = username_or_email
            
            # Authenticate
            user = authenticate(request, username=username, password=password)
            
            if user is not None:
                # Check if user has a profile
                try:
                    profile = user.userprofile
                except UserProfile.DoesNotExist:
                    # Create profile if missing
                    user_type = 'owner' if user.is_staff or user.is_superuser else 'tenant'
                    profile = UserProfile.objects.create(user=user, user_type=user_type)
                
                # For tenant users, check if approved
                if profile.user_type == 'tenant' and not profile.is_approved:
                    error = "Your account is pending approval. Please wait for the owner to approve your registration."
                else:
                    login(request, user)
                    log_activity(user, 'login', f"User {user.username} logged in")
                    
                    if profile.user_type == 'tenant':
                        return redirect('tenant_dashboard')
                    else:
                        return redirect('dashboard')
            else:
                error = "Invalid username/email or password. Please try again."
    
    return render(request, 'system/login.html', {'error': error, 'login_error': error})

def logout_view(request):
    if request.user.is_authenticated:
        log_activity(request, 'logout', "User logged out.")
    logout(request)
    return redirect('login_view')

# ============== OWNER DASHBOARD ==============
@login_required
def dashboard(request):
    profile = request.user.userprofile

    if profile.user_type != 'owner':
        return redirect('tenant_dashboard')

    rooms = Room.objects.all()
    settings = get_settings()
    ELECTRICITY_RATE = settings.electricity_rate
    
    for room in rooms:
        current_usage = room.get_current_usage()
        room.current_usage = current_usage
        room.cost = current_usage * ELECTRICITY_RATE


        room.over_limit = current_usage > room.limit
        
        if room.over_limit and not Alert.objects.filter(
            room=room, 
            alert_type='over_limit',
            created_at__date=timezone.now().date()
        ).exists():
            Alert.objects.create(
                room=room,
                alert_type='over_limit',
                message=f"Room {room.name} is over limit! Current: {current_usage} kWh, Limit: {room.limit} kWh"
            )
    
    total_rooms = rooms.count()
    occupied_rooms = sum(1 for room in rooms if room.is_occupied())
    total_kwh = sum(room.current_usage for room in rooms)
    total_cost = sum(room.cost for room in rooms)
    over_limit_count = sum(1 for room in rooms if room.over_limit)
    
    recent_alerts = Alert.objects.order_by('-created_at')[:10]
    unread_alerts_count = Alert.objects.filter(is_read=False).count()
    
    available_tenants = UserProfile.objects.filter(
        user_type='tenant', 
        room__isnull=True
    ).select_related('user')
    
    from datetime import date
    today = date.today()
    
    return render(request, 'system/dashboard.html', {
        'rooms': rooms,
        'username': request.user.username,
        'electricity_rate': ELECTRICITY_RATE,
        'recent_alerts': recent_alerts,
        'unread_alerts_count': unread_alerts_count,
        'available_tenants': available_tenants,
        'today': today,
        'stats': {
            'total_rooms': total_rooms,
            'occupied_rooms': occupied_rooms,
            'total_kwh': total_kwh,
            'total_cost': total_cost,
            'over_limit_count': over_limit_count,
        }
    })

# ============== TENANT DASHBOARD ==============
@login_required
def tenant_dashboard(request):
    send_payment_reminders(days_before_due=3)

    profile = request.user.userprofile
    if profile.user_type != 'tenant':
        return redirect('dashboard')

    room = profile.room
    settings = get_settings()
    admin_info = settings

    if not room:
        return render(request, 'user/tenant_dashboard.html', {
            'no_room': True,
            'admin_info': admin_info,
            'username': request.user.username,
            'owner_announcement': settings.owner_announcement,
        })

    ELECTRICITY_RATE = settings.electricity_rate

    current_usage = room.get_current_usage()
    room.current_usage = current_usage
    room.cost = current_usage * ELECTRICITY_RATE
    room.usage = current_usage 

    bills = Billing.objects.filter(room=room).order_by('-created_at')
    current_month = timezone.now().strftime("%B %Y")
    current_date = timezone.now()
    current_bill = bills.filter(billing_month=current_month).first()
    if current_bill:
        current_bill = attach_bill_display_data(current_bill)

    from .models import TenantAssignment
    from dateutil.relativedelta import relativedelta
    
    move_in_date = None
    may_bill_due_date = None
    next_due_date = None
    may_bill_is_paid = False
    may_bill_amount = 0
    late_penalty = 0
    
    try:
        assignment = TenantAssignment.objects.get(room=room, is_active=True)
        move_in_date = assignment.move_in_date
        
        may_bill_due_date = move_in_date + relativedelta(months=1)
        next_due_date = move_in_date + relativedelta(months=2)
        
        may_bill = Billing.objects.filter(room=room, billing_month='May 2026').first()
        if may_bill:
            may_bill_is_paid = may_bill.is_paid
            may_bill_amount = may_bill.cost

    except TenantAssignment.DoesNotExist:
        pass

    tenant_alert_types = ['over_limit', 'power_off', 'power_on', 'billing']
    recent_alerts = Alert.objects.filter(
        room=room,
        alert_type__in=tenant_alert_types
    ).order_by('-created_at')[:5]
    
    alerts_count = Alert.objects.filter(
        room=room,
        alert_type__in=tenant_alert_types
    ).count()
    
    unread_alerts_count = Alert.objects.filter(
        room=room,
        alert_type__in=tenant_alert_types,
        is_read=False
    ).count()

    total_kwh = sum(bill.kwh for bill in bills)
    total_paid = sum(bill.cost for bill in bills if bill.is_paid)

    days_in_month = 30
    avg_daily_usage = current_usage / days_in_month if current_usage else 0
    bill_kwh = current_bill.kwh if current_bill else current_usage
    bill_amount = current_bill.cost if current_bill else room.cost
    bill_formula = f"{bill_kwh:.2f} kWh x PHP {ELECTRICITY_RATE:.2f}/kWh"

    due_date = None
    if current_bill and current_bill.due_date:
        due_date = current_bill.due_date

    return render(request, 'user/tenant_dashboard.html', {
        'room': room,
        'bills': bills,
        'current_bill': current_bill,
        'current_month': current_month,
        'current_date': current_date,
        'due_date': due_date,
        'electricity_rate': ELECTRICITY_RATE,
        'username': request.user.username,
        'recent_alerts': recent_alerts,
        'alerts_count': alerts_count,
        'unread_alerts_count': unread_alerts_count,
        'admin_info': admin_info,
        'avg_daily_usage': avg_daily_usage,
        'bill_kwh': bill_kwh,
        'bill_amount': bill_amount,
        'bill_formula': bill_formula,
        'total_kwh': total_kwh,
        'total_paid': total_paid,
        'move_in_date': move_in_date,
        'may_bill_due_date': may_bill_due_date,
        'next_due_date': next_due_date,
        'may_bill_is_paid': may_bill_is_paid,
        'may_bill_amount': may_bill_amount,
        'late_penalty': late_penalty,
        'owner_announcement': settings.owner_announcement,
    })

@login_required
def tenant_notifications(request):
    profile = request.user.userprofile

    if profile.user_type != 'tenant':
        return redirect('dashboard')

    room = profile.room

    if not room:
        return redirect('tenant_dashboard')

    tenant_alert_types = [
        'over_limit',
        'power_off',
        'power_on',
        'billing',
        'late_payment',
        'abnormal_usage',
        'high_consumption'
    ]

    all_alerts = Alert.objects.filter(
        room=room,
        alert_type__in=tenant_alert_types
    ).order_by('-created_at')

    if request.method == 'POST':
        if 'mark_all_read' in request.POST:
            all_alerts.filter(is_read=False).update(is_read=True)
        elif 'delete_all_alerts' in request.POST:
            all_alerts.delete()
        return redirect('tenant_notifications')

    from django.core.paginator import Paginator
    paginator = Paginator(all_alerts, 20)
    page_number = request.GET.get('page')
    alerts = paginator.get_page(page_number)
    unread_count = all_alerts.filter(is_read=False).count()

    return render(request, 'user/tenant_notifications.html', {
        'alerts': alerts,
        'unread_count': unread_count,
        'unread_alerts_count': unread_count,
        'room': room,
        'username': request.user.username,
    })
    
@login_required
def tenant_billing_history(request):
    profile = request.user.userprofile
    
    if profile.user_type != 'tenant':
        return redirect('dashboard')
    
    room = profile.room
    
    if not room:
        return redirect('tenant_dashboard')
    
    bills = Billing.objects.filter(room=room).order_by('-billing_month', '-created_at')
    payment_records = Payment.objects.filter(
        bill__room=room,
        tenant=profile
    ).select_related('bill', 'bill__room').order_by('-created_at')
    receipt_records = payment_records.filter(status='paid').order_by('-paid_at', '-created_at')
    
    total_bills = bills.count()
    total_kwh = sum(bill.kwh for bill in bills)
    total_amount = sum(bill.cost for bill in bills)
    paid_bills = bills.filter(is_paid=True).count()
    unpaid_bills = total_bills - paid_bills
    
    settings = get_settings()
    electricity_rate = settings.electricity_rate
    
    tenant_alert_types = ['over_limit', 'power_off', 'power_on', 'billing']
    unread_alerts_count = Alert.objects.filter(
        room=room,
        alert_type__in=tenant_alert_types,
        is_read=False
    ).count()
    
    return render(request, 'user/tenant_billing_history.html', {
        'bills': bills,
        'total_bills': total_bills,
        'total_kwh': total_kwh,
        'total_amount': total_amount,
        'paid_bills': paid_bills,
        'unpaid_bills': unpaid_bills,
        'payment_records': payment_records,
        'receipt_records': receipt_records,
        'electricity_rate': electricity_rate,
        'room': room,
        'username': request.user.username,
        'unread_alerts_count': unread_alerts_count,
    })


@login_required
def tenant_download_bill(request, bill_id):
    profile = request.user.userprofile

    if profile.user_type != 'tenant':
        return redirect('dashboard')

    bill = get_object_or_404(Billing, id=bill_id, room=profile.room)
    settings = get_settings()
    paid_payment = Payment.objects.filter(bill=bill, status='paid').order_by('-paid_at').first()
    filename = f"bill_{bill.room.name}_{bill.billing_month.replace(' ', '_')}.html"

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Bill Summary - {bill.billing_month}</title>
    <style>
        body {{ font-family: Arial, sans-serif; color: #1f2937; padding: 32px; }}
        .receipt {{ max-width: 720px; margin: 0 auto; border: 1px solid #e5e7eb; padding: 24px; }}
        h1 {{ margin: 0 0 4px; font-size: 22px; }}
        .muted {{ color: #6b7280; font-size: 12px; margin-bottom: 24px; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        td {{ padding: 10px 0; border-bottom: 1px solid #f1f5f9; }}
        td:last-child {{ text-align: right; font-weight: 700; }}
        .total td {{ font-size: 16px; border-top: 2px solid #667eea; padding-top: 14px; }}
    </style>
</head>
<body>
    <div class="receipt">
        <h1>Digital Official Receipt</h1>
        <div class="muted">Smart Energy Monitor Bill Summary</div>
        <table>
            <tr><td>Receipt No.</td><td>OR-{paid_payment.id if paid_payment else bill.id:06d}</td></tr>
            <tr><td>Billing Month</td><td>{bill.billing_month}</td></tr>
            <tr><td>Room</td><td>{bill.room.name}</td></tr>
            <tr><td>Reference</td><td>{paid_payment.reference_number if paid_payment else 'N/A'}</td></tr>
            <tr><td>Paid Date</td><td>{paid_payment.paid_at.strftime('%Y-%m-%d %H:%M') if paid_payment and paid_payment.paid_at else 'N/A'}</td></tr>
            <tr><td>Consumption</td><td>{bill.kwh:.2f} kWh</td></tr>
            <tr><td>Rate</td><td>PHP {settings.electricity_rate:.2f}/kWh</td></tr>
            <tr><td>Status</td><td>{'PAID' if bill.is_paid else 'UNPAID'}</td></tr>
            <tr class="total"><td>Total Amount</td><td>PHP {bill.cost:.2f}</td></tr>
        </table>
    </div>
</body>
</html>"""

    response = HttpResponse(html, content_type='text/html')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response

# ============== ROOM MANAGEMENT ==============
@login_required
def toggle_power(request, room_id):
    if request.user.userprofile.user_type != 'owner':
        messages.error(request, "You don't have permission to do that.")
        return redirect('tenant_dashboard')
    
    room = get_object_or_404(Room, id=room_id)
    old_status = room.power_status
    room.power_status = not room.power_status
    room.save()
    
    if old_status:
        alert_type = 'power_off'
        message = f"Room {room.name} power manually turned OFF by owner."
    else:
        alert_type = 'power_on'
        message = f"Room {room.name} power manually turned ON by owner."
    
    Alert.objects.create(
        room=room,
        alert_type=alert_type,
        message=message
    )
    log_activity(request, 'toggle', message)
    
    return redirect('rooms_page')


@login_required
def add_room(request):
    if request.user.userprofile.user_type != 'owner':
        return redirect('tenant_dashboard')
    
    if request.method == 'POST':
        name = request.POST.get('name')
        limit = float(request.POST.get('limit', 200))
        power_status = request.POST.get('power_status') == 'on'
        
        if not name:
            messages.error(request, "Room name is required.")
            return render(request, 'system/add_room.html')
        
        room = Room.objects.create(
            name=name,
            limit=limit,
            power_status=power_status
        )
        
        Alert.objects.create(
            alert_type='power_on',
            message=f"New room added: {name}",
            room=room
        )
        
        messages.success(request, f"Room '{name}' added successfully!")
        log_activity(request, 'create', f"Created room {name} with {limit} kWh limit.")
        return redirect('rooms_page')
    
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
        room.save()
        
        Alert.objects.create(
            alert_type='billing',
            message=f"Room {room.name} updated",
            room=room
        )
        
        messages.success(request, f"Room '{room.name}' updated successfully!")
        log_activity(request, 'update', f"Updated room {old_name} to {room.name} with {room.limit} kWh limit.")
        return redirect('rooms_page')
    
    unread_alerts_count = Alert.objects.filter(is_read=False).count()
    
    current_usage = room.get_current_usage()
    
    return render(request, 'system/edit_room.html', {
        'room': room,
        'current_usage': current_usage,
        'username': request.user.username,
        'unread_alerts_count': unread_alerts_count
    })


@login_required
def delete_room(request, room_id):
    if request.user.userprofile.user_type != 'owner':
        return redirect('tenant_dashboard')
    
    room = get_object_or_404(Room, id=room_id)
    room_name = room.name
    
    UserProfile.objects.filter(room=room, user_type='tenant').update(room=None)
    
    Alert.objects.create(
        alert_type='power_off',
        message=f"Room deleted: {room_name}",
        room=None
    )
    
    room.delete()
    messages.success(request, f"Room '{room_name}' deleted successfully!")
    log_activity(request, 'delete', f"Deleted room {room_name}.")
    return redirect('rooms_page')


# ============== TENANT ASSIGNMENT ==============
@login_required
def assign_tenant(request, room_id):
    if request.user.userprofile.user_type != 'owner':
        messages.error(request, "You don't have permission to do that.")
        return redirect('dashboard')

    if request.method == 'POST':
        tenant_id = request.POST.get('tenant_id')
        move_in_date_str = request.POST.get('move_in_date')
        room = get_object_or_404(Room, id=room_id)

        if tenant_id:
            tenant_profile = get_object_or_404(UserProfile, id=tenant_id, user_type='tenant')

            if move_in_date_str:
                move_in_date = datetime.strptime(move_in_date_str, '%Y-%m-%d').date()
            else:
                move_in_date = date.today()

            TenantAssignment.objects.filter(room=room, is_active=True).update(is_active=False)

            assignment = TenantAssignment.objects.create(
                tenant=tenant_profile,
                room=room,
                move_in_date=move_in_date,
                is_active=True
            )

            tenant_profile.room = room
            tenant_profile.save()

            current_month = timezone.now().strftime("%B %Y")
            Billing.objects.filter(room=room, billing_month=current_month).delete()
            regenerate_bills_for_room(room)

            Alert.objects.create(
                room=room,
                alert_type='tenant_assigned',
                message=(
                    f"Tenant {tenant_profile.user.username} "
                    f"assigned to room {room.name} "
                    f"starting {assignment.move_in_date}. "
                    f"Due date: {assignment.get_due_date()}"
                )
            )

            messages.success(
                request,
                f"Tenant {tenant_profile.user.username} assigned to {room.name} starting {assignment.move_in_date}"
            )
            log_activity(
                request,
                'assign',
                f"Assigned tenant {tenant_profile.user.username} to {room.name} starting {assignment.move_in_date}."
            )
        else:
            TenantAssignment.objects.filter(room=room, is_active=True).update(is_active=False)
            UserProfile.objects.filter(room=room, user_type='tenant').update(room=None)
            current_month = timezone.now().strftime("%B %Y")
            Billing.objects.filter(room=room, billing_month=current_month).delete()
            messages.success(request, f"Tenant removed from {room.name}.")
            log_activity(request, 'remove', f"Removed tenant from {room.name}.")

    return redirect('rooms_page')

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
        log_activity(request, 'remove', f"Removed tenant {tenant_name} from {room.name}.")
    
    return redirect('rooms_page')

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
    pending_approval = tenants.filter(is_approved=False).count()
    pending_approvals_count = UserProfile.objects.filter(user_type='tenant', is_approved=False).count()
    
    rooms = Room.objects.all()
    available_rooms = rooms.filter(userprofile__isnull=True).count()
    
    return render(request, 'system/tenant_list.html', {
        'tenants': tenants,
        'total_tenants': total_tenants,
        'with_room': with_room,
        'without_room': without_room,
        'pending_approval': pending_approval,
        'available_rooms': available_rooms,
        'username': request.user.username,
        'unread_alerts_count': Alert.objects.filter(is_read=False).count(),
    })

# ============== BILLING VIEWS ==============
@login_required
def billing_view(request):
    settings = get_settings()
    
    if request.user.userprofile.user_type != 'owner':
        return redirect('tenant_dashboard')
    
    current_month = timezone.now().strftime("%B %Y")
    today = timezone.now().date()
    
    rooms = Room.objects.all()
    for room in rooms:
        assignment = get_active_assignment(room)
        has_tenant = assignment is not None
        
        if has_tenant:
            current_usage = room.get_current_usage()
            cycle = get_bill_cycle_details(assignment, today)
            bill, created = Billing.objects.get_or_create(
                room=room,
                billing_month=current_month,
                defaults={
                    'kwh': current_usage,
                    'cost': current_usage * settings.electricity_rate,
                    'is_paid': False,
                    'due_date': cycle['cycle_due_date'] or today,
                    'reminder_sent': False,
                    'tenant_assignment': assignment,
                    'days_occupied': cycle['days_occupied'],
                }
            )
            
            if not created:
                bill.kwh = current_usage
                bill.cost = current_usage * settings.electricity_rate
                bill.due_date = cycle['cycle_due_date'] or bill.due_date
                bill.tenant_assignment = assignment
                bill.days_occupied = cycle['days_occupied']
                bill.save()
        else:
            Billing.objects.filter(room=room, billing_month=current_month).delete()
    
    bills = Billing.objects.filter(
        billing_month=current_month,
        room__userprofile__user_type='tenant',
        room__userprofile__isnull=False
    ).select_related('room').distinct()
    bills = [attach_bill_display_data(bill) for bill in bills]
    unpaid_bills = [bill for bill in bills if not bill.is_paid]
    paid_bills = [bill for bill in bills if bill.is_paid]
    
    total_kwh = sum(bill.kwh for bill in bills)
    total_cost = sum(bill.cost for bill in bills)
    paid_count = sum(1 for bill in bills if bill.is_paid)
    unpaid_count = len(unpaid_bills)
    
    unread_alerts_count = Alert.objects.filter(is_read=False).count()
    
    return render(request, 'system/billing.html', {
        'bills': bills,
        'unpaid_bills': unpaid_bills,
        'paid_bills': paid_bills,
        'current_month': current_month,
        'total_kwh': total_kwh,
        'total_cost': total_cost,
        'paid_count': paid_count,
        'unpaid_count': unpaid_count,
        'username': request.user.username,
        'electricity_rate': settings.electricity_rate,
        'unread_alerts_count': unread_alerts_count
    })

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
        bills_query = bills_query.filter(
            Q(room__name__icontains=room_name) |
            Q(room__userprofile__user__username__icontains=room_name) |
            Q(room__userprofile__user__first_name__icontains=room_name) |
            Q(room__userprofile__user__last_name__icontains=room_name)
        )
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
    if request.user.userprofile.user_type != 'owner':
        return redirect('tenant_dashboard')

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
    if request.user.userprofile.user_type != 'owner':
        return redirect('tenant_dashboard')

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

# ============== SMART FEATURES FUNCTIONS ==============
def detect_abnormal_usage():
    print("\n🔍 Checking for abnormal usage patterns...")
    print("-" * 50)
    
    today = timezone.now().date()
    yesterday = today - timedelta(days=1)
    
    rooms = Room.objects.all()
    alerts_created = 0
    
    for room in rooms:
        thirty_days_ago = today - timedelta(days=30)
        historical_data = EnergyUsage.objects.filter(
            room=room,
            timestamp__date__gte=thirty_days_ago,
            timestamp__date__lt=yesterday
        ).values_list('kwh', flat=True)
        
        yesterday_usage = EnergyUsage.objects.filter(
            room=room,
            timestamp__date=yesterday
        ).aggregate(total=Sum('kwh'))['total'] or 0
        
        if len(historical_data) < 7:
            continue
        
        avg_usage = sum(historical_data) / len(historical_data)
        
        if len(historical_data) > 1:
            variance = sum((x - avg_usage) ** 2 for x in historical_data) / len(historical_data)
            std_dev = variance ** 0.5
        else:
            std_dev = avg_usage * 0.3
        
        if yesterday_usage > 0 and yesterday_usage > avg_usage + (2 * std_dev):
            percent_increase = ((yesterday_usage - avg_usage) / avg_usage) * 100
            Alert.objects.create(
                room=room,
                alert_type='abnormal_usage',
                message=f"⚠️ Abnormal usage detected! Yesterday's consumption ({yesterday_usage:.2f} kWh) is {percent_increase:.1f}% higher than average ({avg_usage:.2f} kWh)."
            )
            alerts_created += 1
            print(f"  ⚠️ {room.name}: {percent_increase:.1f}% increase")
    
    print("-" * 50)
    print(f"✅ Created {alerts_created} abnormal usage alerts")
    return alerts_created

def check_high_consumption():
    print("\n📊 Checking for high consumption...")
    print("-" * 50)
    
    today = timezone.now()
    current_month = today.month
    current_year = today.year
    
    rooms = Room.objects.all()
    alerts_created = 0
    
    for room in rooms:
        total_usage = EnergyUsage.objects.filter(
            room=room,
            timestamp__year=current_year,
            timestamp__month=current_month
        ).aggregate(total=Sum('kwh'))['total'] or 0
        
        if room.limit > 0:
            percentage = (total_usage / room.limit) * 100
            
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

def run_smart_features_daily():
    print("\n" + "="*60)
    print("🤖 RUNNING SMART FEATURES")
    print("="*60)
    
    abnormal = detect_abnormal_usage()
    high_cons = check_high_consumption()
    late = 0
    
    print("\n" + "="*60)
    print(f"📊 SUMMARY: {abnormal} abnormal, {high_cons} high consumption")
    print("="*60)
    
    return {
        'abnormal': abnormal,
        'high_consumption': high_cons,
        'late_payments': 0
    }    

@login_required
@require_POST
def run_smart_features_api(request):
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
    
    settings = SystemSettings.get_settings()
    
    if request.method == 'POST':
        admin_name = request.POST.get('admin_name', '').strip()
        admin_email = request.POST.get('admin_email', '').strip()
        admin_phone = request.POST.get('admin_phone', '').strip()
        system_name = request.POST.get('system_name', '').strip()
        
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
        
        settings.admin_name = admin_name if admin_name else "System Administrator"
        settings.admin_email = admin_email if admin_email else "admin@example.com"
        settings.admin_phone = admin_phone if admin_phone else "+63 XXX XXX XXXX"
        settings.system_name = system_name if system_name else "Smart Energy Monitor"
        settings.owner_announcement = request.POST.get('owner_announcement', '').strip()
        
        if request.POST.get('electricity_rate'):
            settings.electricity_rate = float(request.POST.get('electricity_rate'))
        if request.POST.get('reminder_days_before'):
            settings.reminder_days_before = int(request.POST.get('reminder_days_before'))
        settings.save()
        
        messages.success(request, "✅ System settings updated successfully!")
        return redirect('system_settings')
    
    return render(request, 'system/settings.html', {
        'settings': settings,
        'username': request.user.username,
    })



@login_required
def system_health(request):
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
def activity_log(request):
    profile = request.user.userprofile

    if profile.user_type != 'owner':
        return redirect('tenant_dashboard')

    logs = ActivityLog.objects.select_related('user').filter(user_type='owner')
    search = request.GET.get('search', '').strip()
    current_filter = request.GET.get('action', '').strip()

    if search:
        logs = logs.filter(
            Q(user__username__icontains=search) |
            Q(description__icontains=search) |
            Q(ip_address__icontains=search)
        )

    if current_filter:
        logs = logs.filter(action=current_filter)

    from django.core.paginator import Paginator
    from django.db.models import Count
    paginator = Paginator(logs, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    total_actions = ActivityLog.objects.filter(user_type='owner').count()
    recent_24h = ActivityLog.objects.filter(
        user_type='owner',
        created_at__gte=timezone.now() - timedelta(hours=24)
    ).count()
    action_counts = {
        row['action']: row['total']
        for row in ActivityLog.objects.filter(user_type='owner').values('action').annotate(total=Count('id'))
    }

    return render(request, 'system/activity_log.html', {
        'logs': page_obj,
        'username': request.user.username,
        'total_actions': total_actions,
        'recent_24h': recent_24h,
        'action_counts': action_counts,
        'search': search,
        'current_filter': current_filter,
        'unread_alerts_count': Alert.objects.filter(is_read=False).count(),
    })


@login_required
def health_dashboard(request):
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
            unread_alerts_count = Alert.objects.filter(room=profile.room, is_read=False).count() if profile.room else 0
            return render(request, 'user/edit_profile.html', {
                'profile': profile,
                'errors': errors,
                'first_name': first_name,
                'last_name': last_name,
                'email': email,
                'phone_number': phone_number,
                'username': request.user.username,
                'unread_alerts_count': unread_alerts_count,
            })
        
        user = request.user
        user.first_name = first_name
        user.last_name = last_name
        user.email = email
        user.save()
        
        profile.phone_number = phone_number
        profile.save()
        
        messages.success(request, "✅ Profile updated successfully!")
        return redirect('edit_profile')
    
    unread_alerts_count = Alert.objects.filter(room=profile.room, is_read=False).count() if profile.room else 0
    return render(request, 'user/edit_profile.html', {
        'profile': profile,
        'first_name': request.user.first_name,
        'last_name': request.user.last_name,
        'email': request.user.email,
        'phone_number': profile.phone_number or '',
        'username': request.user.username,
        'unread_alerts_count': unread_alerts_count,
    })

# ============== GCASH/PAYMENT VIEWS ==============
@login_required
def create_gcash_payment(request, bill_id):
    profile = request.user.userprofile
    
    if profile.user_type != 'tenant':
        return redirect('dashboard')
    
    bill = get_object_or_404(Billing, id=bill_id, room=profile.room)
    
    if bill.is_paid:
        messages.warning(request, "This bill is already paid.")
        return redirect('tenant_dashboard')
    
    payment = Payment.objects.filter(bill=bill, status='pending', payment_method='gcash').first()
    if not payment:
        payment = create_payment_record(bill, profile, 'gcash')
        print(f"✅ Created new payment with reference: {payment.reference_number}")
        log_activity(request, 'payment', f"Started GCash payment for {bill.room.name} {bill.billing_month}. Reference: {payment.reference_number}.")
    else:
        print(f"✅ Using existing payment with reference: {payment.reference_number}")
    
    from django.conf import settings as django_settings
    base_url = getattr(django_settings, 'APP_BASE_URL', 'http://127.0.0.1:8000')
    
    success_url = f"{base_url}/payment/success/{payment.reference_number}/"
    cancel_url = f"{base_url}/tenant/"
    
    description = f"Electricity Bill - {bill.room.name} - {bill.billing_month} - Ref: {payment.reference_number}"
    
    print(f"🔗 Success URL: {success_url}")
    print(f"🔗 Cancel URL: {cancel_url}")
    print(f"💰 Amount: {bill.cost}")
    print(f"📝 Reference: {payment.reference_number}")
    print(f"📝 Description: {description}")
    
    if not PAYMONGO_AVAILABLE:
        messages.error(request, "Payment gateway is not available. Please try again later or use Cash payment.")
        return redirect('payment_method')
    
    try:
        paymongo = get_paymongo()
        
        result = paymongo.create_checkout_session(
            amount=bill.cost,
            description=description,
            success_url=success_url,
            cancel_url=cancel_url,
            reference=payment.reference_number
        )
        
        if result.get('status') == 'success' or 'data' in result:
            checkout_url = result['data']['attributes']['checkout_url']
            checkout_id = result['data']['id']
            
            payment.checkout_session_id = checkout_id
            payment.save()
            
            print(f"🚀 Redirecting to PayMongo checkout: {checkout_url}")
            return redirect(checkout_url)
        else:
            error_msg = result.get('error', 'Unknown error')
            messages.error(request, f"Failed to create payment: {error_msg}")
            return redirect('payment_method')
            
    except Exception as e:
        print(f"❌ PayMongo error: {str(e)}")
        messages.error(request, f"Payment gateway error: {str(e)}")
        return redirect('payment_method')

@login_required
def payment_success(request, reference_number):
    payment = get_object_or_404(
        Payment,
        reference_number=reference_number
    )

    profile = request.user.userprofile

    # Tenant can only access their own payment
    if profile.user_type == 'tenant' and payment.tenant_id != profile.id:
        messages.error(
            request,
            "You do not have access to this payment."
        )
        return redirect('tenant_dashboard')

    # IMPORTANT:
    # Do NOT mark payment as paid here.
    # PayMongo webhook is the authoritative source.

    if payment.status == 'paid':
        messages.success(
            request,
            f"Payment of PHP {payment.amount:.2f} "
            f"for {payment.bill.billing_month} has been confirmed."
        )

    elif payment.status == 'pending':
        messages.info(
            request,
            "Your payment is being processed. "
            "Please wait for PayMongo confirmation."
        )

    elif payment.status == 'failed':
        messages.error(
            request,
            "Your payment was not successful."
        )

    else:
        messages.info(
            request,
            "Your payment status is still being processed."
        )

    return redirect('tenant_dashboard')
    
@login_required
def manual_paid_confirmation(request, bill_id):
    profile = request.user.userprofile
    
    if profile.user_type != 'tenant':
        return redirect('dashboard')
    
    bill = get_object_or_404(Billing, id=bill_id, room=profile.room)
    
    if request.method == 'POST':
        payment = Payment.objects.filter(bill=bill, status='pending', payment_method='cash').first()
        if not payment:
            payment = create_payment_record(bill, profile, 'cash')
        
        payment.notes = request.POST.get('notes', '')
        payment.save()
        log_activity(request, 'payment', f"Submitted cash payment notice for {bill.room.name} {bill.billing_month}. Reference: {payment.reference_number}.")
        
        Alert.objects.create(
            room=bill.room,
            alert_type='billing',
            visibility='admin_only',
            message=f"💵 Tenant {profile.user.username} has paid ₱{bill.cost} for {bill.billing_month} via CASH. Please verify and mark as paid.",
            action_url=f"/billing/"
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
    current_bill = attach_bill_display_data(current_bill)
    
    pending_payment = Payment.objects.filter(bill=current_bill, status='pending').first()
    
    return render(request, 'user/payment_method.html', {
        'bill': current_bill,
        'pending_payment': pending_payment,
        'room': room,
        'username': request.user.username,
        'electricity_rate': get_settings().electricity_rate,
        'bill_formula': f"{current_bill.kwh:.2f} kWh x PHP {get_settings().electricity_rate:.2f}/kWh",
    })

@login_required
def payment_checkout_simulation(request, reference):
    from .models import Payment
    
    payment = get_object_or_404(Payment, reference_number=reference)
    bill = payment.bill
    profile = request.user.userprofile

    if not django_settings.DEBUG:
        messages.error(request, "Payment simulation is disabled.")
        return redirect('tenant_dashboard')

    if profile.user_type != 'tenant' or payment.tenant_id != profile.id:
        messages.error(request, "You do not have access to this payment.")
        return redirect('tenant_dashboard')
    
    if request.method == 'POST':
        payment.status = 'paid'
        payment.paid_at = timezone.now()
        payment.transaction_id = f"SIM_{reference}"
        payment.save()
        
        bill.is_paid = True
        bill.save()
        log_activity(request, 'payment', f"Completed simulated payment for {bill.room.name} {bill.billing_month}. Reference: {reference}.")
        
        Alert.objects.create(
            room=bill.room,
            alert_type='billing',
            message=f"✅ Payment of ₱{payment.amount} for {bill.billing_month} has been received via SIMULATION."
        )
        
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
        'electricity_rate': get_settings().electricity_rate,
        'username': request.user.username,
    })

@login_required
def rooms_page(request):
    """Rooms management page"""
    profile = request.user.userprofile
    
    if profile.user_type != 'owner':
        return redirect('tenant_dashboard')
    
    rooms = Room.objects.all()
    settings = get_settings()
    ELECTRICITY_RATE = settings.electricity_rate
    
    # GET AVAILABLE TENANTS FOR ASSIGNMENT
    available_tenants = UserProfile.objects.filter(
        user_type='tenant', 
        room__isnull=True,
        is_approved=True
    ).select_related('user')
    
    for room in rooms:
        current_usage = room.get_current_usage()
        room.current_usage = current_usage
        room.cost = current_usage * ELECTRICITY_RATE
        room.over_limit = current_usage > room.limit
        
        # Check for unpaid bills
        from .models import Billing
        unpaid_bill = Billing.objects.filter(
            room=room,
            is_paid=False
        ).order_by('-billing_month').first()
        
        room.has_unpaid_bill = unpaid_bill is not None
        room.unpaid_bill_month = unpaid_bill.billing_month if unpaid_bill else None
        room.unpaid_bill_amount = unpaid_bill.cost if unpaid_bill else 0
        
        
        
        if room.over_limit and not Alert.objects.filter(
            room=room, 
            alert_type='over_limit',
            created_at__date=timezone.now().date()
        ).exists():
            Alert.objects.create(
                room=room,
                alert_type='over_limit',
                message=f"Room {room.name} is over limit! Current: {current_usage} kWh, Limit: {room.limit} kWh"
            )
    
    total_rooms = rooms.count()
    occupied_rooms = sum(1 for room in rooms if room.is_occupied())
    total_kwh = sum(room.current_usage for room in rooms)
    total_cost = sum(room.cost for room in rooms)
    over_limit_count = sum(1 for room in rooms if room.over_limit)
    
    recent_alerts = Alert.objects.order_by('-created_at')[:10]
    unread_alerts_count = Alert.objects.filter(is_read=False).count()
    
    from datetime import date
    today = date.today()
    
    # ✅ GAMITIN ANG TAMANG TEMPLATE NAME
    return render(request, 'system/rooms.html', {
        'rooms': rooms,
        'username': request.user.username,
        'electricity_rate': ELECTRICITY_RATE,
        'recent_alerts': recent_alerts,
        'unread_alerts_count': unread_alerts_count,
        'available_tenants': available_tenants,
        'today': today,
        'stats': {
            'total_rooms': total_rooms,
            'occupied_rooms': occupied_rooms,
            'total_kwh': total_kwh,
            'total_cost': total_cost,
            'over_limit_count': over_limit_count,
        }
    })

@login_required
def bill_details_api(request, bill_id):
    profile = request.user.userprofile
    
    if profile.user_type != 'owner':
        return JsonResponse(
            {'error': 'Unauthorized'},
            status=403
        )
    
    bill = get_object_or_404(Billing, id=bill_id)
    
    payments = Payment.objects.filter(
        bill=bill
    ).order_by('-created_at')
    
    settings = SystemSettings.get_settings()
    electricity_rate = settings.electricity_rate
    bill = attach_bill_display_data(bill)
    
    due_date_str = (
        bill.due_date_display.strftime('%Y-%m-%d')
        if bill.due_date_display else 'Not set'
    )
    
    days_occupied = bill.days_occupied_display
    late_penalty = 0
    tenant_name = bill.tenant_name
    paid_at = None
    
    if bill.is_paid:
        paid_payment = payments.filter(status='paid').first()
        
        if paid_payment and paid_payment.paid_at:
            paid_at = paid_payment.paid_at
    
    return JsonResponse({
        'id': bill.id,
        'room': bill.room.name,
        'tenant': tenant_name,
        'billing_month': bill.billing_month,
        'previous_bill': get_previous_bill_summary(bill),
        'move_in_date': bill.move_in_date_display.strftime('%Y-%m-%d') if bill.move_in_date_display else 'Not set',
        'cycle_start': bill.cycle_start_display.strftime('%Y-%m-%d') if bill.cycle_start_display else 'Not set',
        'due_date': due_date_str,
        'days_occupied': days_occupied,
        'kwh': round(bill.kwh, 2),
        'rate': round(electricity_rate, 2),
        'base_amount': round(bill.cost, 2),
        'formula': f"{bill.kwh:.2f} kWh x PHP {electricity_rate:.2f}/kWh",
        'late_penalty': 0,
        'total_amount': round(bill.cost, 2),
        'is_paid': bill.is_paid,
        'paid_at': (
            paid_at.strftime('%Y-%m-%d %H:%M')
            if paid_at else None
        ),
        'payment_history': [
            {
                'payment_method': p.payment_method,
                'amount': round(p.amount, 2),
                'status': p.status,
                'date': p.created_at.strftime(
                    '%Y-%m-%d %H:%M'
                ),
                'reference_number': p.reference_number
            }
            for p in payments
        ]
    })

def regenerate_bills_for_room(room, month=None, year=None):
    from .models import Billing, TenantAssignment, EnergyUsage
    from datetime import date, datetime
    from django.utils import timezone
    from django.db.models import Q, Sum
    import calendar

    if month is None or year is None:
        today = datetime.now()
        year = today.year
        month = today.month

    month_name = datetime(year, month, 1).strftime("%B %Y")
    month_start = date(year, month, 1)
    current_date = timezone.now().date()

    if year == current_date.year and month == current_date.month:
        month_end = current_date
    else:
        month_end = date(year, month, calendar.monthrange(year, month)[1])

    assignment = TenantAssignment.objects.filter(
        room=room,
        is_active=True,
        move_in_date__lte=month_end
    ).filter(
        Q(move_out_date__isnull=True) |
        Q(move_out_date__gte=month_start)
    ).first()

    if not assignment:
        Billing.objects.filter(room=room, billing_month=month_name).delete()
        return None

    move_in_date = assignment.move_in_date

    if move_in_date > month_end:
        days_occupied = 0
    elif move_in_date < month_start:
        days_occupied = (month_end - month_start).days + 1
    else:
        days_occupied = (month_end - move_in_date).days + 1
        if move_in_date == month_end:
            days_occupied = 1

    if days_occupied <= 0:
        Billing.objects.filter(room=room, billing_month=month_name).delete()
        return None

    total_kwh = EnergyUsage.objects.filter(
        room=room,
        timestamp__year=year,
       timestamp__month=month
    ).aggregate(total=Sum('kwh'))['total'] or 0

    total_days = (month_end - month_start).days + 1

    prorated_kwh = (total_kwh / total_days) * days_occupied if total_days > 0 else 0

    settings = SystemSettings.get_settings()
    prorated_cost = prorated_kwh * settings.electricity_rate
    due_date = assignment.get_due_date()

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

    print(f"{room.name} | Days: {days_occupied} | Due: {due_date}")
    return bill
    
@login_required
def delete_tenant(request, tenant_id):
    if request.user.userprofile.user_type != 'owner':
        messages.error(request, "You don't have permission to do that.")
        return redirect('dashboard')
    
    tenant = get_object_or_404(UserProfile, id=tenant_id, user_type='tenant')
    username = tenant.user.username
    
    try:
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA foreign_keys=OFF;")
            tenant.delete()
            tenant.user.delete()
            cursor.execute("PRAGMA foreign_keys=ON;")
        
        messages.success(request, f"Tenant '{username}' has been deleted successfully.")
        
    except Exception as e:
        messages.error(request, f"Error deleting tenant: {str(e)}")
    
    return redirect('tenant_list')


from django.utils import timezone
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from .models import UserProfile, Alert

def approve_tenant(request, tenant_id):
    """Approve tenant registration"""
    profile = request.user.userprofile
    
    if profile.user_type != 'owner':
        messages.error(request, "You don't have permission to do that.")
        return redirect('dashboard')
    
    tenant = get_object_or_404(UserProfile, id=tenant_id, user_type='tenant')
    
    if tenant.is_approved:
        messages.warning(request, f"Tenant {tenant.user.username} is already approved.")
        return redirect('tenant_list')
    
    # Approve the tenant
    tenant.is_approved = True
    tenant.approved_at = timezone.now()
    tenant.save()
    
    # Send approval email
    send_approval_email(tenant.user)
    
    # Create alert for owner
    Alert.objects.create(
        room=None,
        alert_type='tenant_assigned',
        message=f"Tenant {tenant.user.get_full_name() or tenant.user.username} has been approved. Please assign a room."
    )
    
    messages.success(request, f"✅ Tenant {tenant.user.username} approved! Now assign a room.")
    return redirect('assign_room_to_tenant', tenant_id=tenant.id)

def assign_room_to_tenant(request, tenant_id):
    """Assign room to approved tenant"""
    profile = request.user.userprofile
    
    if profile.user_type != 'owner':
        messages.error(request, "You don't have permission to do that.")
        return redirect('dashboard')
    
    tenant = get_object_or_404(UserProfile, id=tenant_id, user_type='tenant', is_approved=True)
    
    if request.method == 'POST':
        room_id = request.POST.get('room_id')
        move_in_date = request.POST.get('move_in_date')
        
        if not room_id:
            messages.error(request, "Please select a room.")
            return redirect('assign_room_to_tenant', tenant_id=tenant_id)
        
        room = get_object_or_404(Room, id=room_id)
        
        # Check if room is available
        if room.is_occupied():
            messages.error(request, f"Room {room.name} is already occupied.")
            return redirect('assign_room_to_tenant', tenant_id=tenant_id)
        
        # Deactivate old assignment if any
        from .models import TenantAssignment
        TenantAssignment.objects.filter(tenant=tenant, is_active=True).update(is_active=False)
        
        # Create new assignment
        from datetime import date
        assignment = TenantAssignment.objects.create(
            tenant=tenant,
            room=room,
            move_in_date=move_in_date or date.today(),
            is_active=True
        )
        
        # Assign room to tenant
        tenant.room = room
        tenant.save()
        
        # Create alert
        Alert.objects.create(
            room=room,
            alert_type='tenant_assigned',
            message=f"Tenant {tenant.user.get_full_name() or tenant.user.username} assigned to room {room.name}"
        )
        
        messages.success(request, f"✅ Tenant {tenant.user.username} assigned to {room.name}!")
        return redirect('tenant_list')
    
    # GET request - show room selection form
    available_rooms = Room.objects.filter(userprofile__isnull=True)
    from datetime import date
    today = date.today()
    
    return render(request, 'system/assign_room.html', {
        'tenant': tenant,
        'available_rooms': available_rooms,
        'today': today,
        'username': request.user.username,
    })

import re
import os

def clean_filename(filename):
    """Automatically clean filename - remove spaces, special characters, and random suffixes"""
    # Get file extension
    name, ext = os.path.splitext(filename)
    
    # Remove spaces (replace with underscore)
    name = name.replace(' ', '_')
    
    # Remove parentheses and other special characters (keep letters, numbers, underscore, dot)
    name = re.sub(r'[^a-zA-Z0-9_.-]', '', name)
    
    # Remove duplicate underscores
    name = re.sub(r'_+', '_', name)
    
    # Remove "download" word if present (common from browsers)
    name = name.replace('download', '')
    name = name.replace('_download', '')
    
    # Remove numbers in parentheses like (1), (2), etc.
    name = re.sub(r'\([0-9]+\)', '', name)
    
    # Clean up any leftover underscores at ends
    name = name.strip('_')
    
    # If name becomes empty, use a default
    if not name:
        name = 'upload'
    
    return f"{name}{ext}"
