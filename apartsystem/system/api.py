import json
import secrets
import hmac
import hashlib
import re
from django.http import JsonResponse
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from .models import EnergyUsage, Room, Alert, Billing, Payment

import logging

logger = logging.getLogger(__name__)


# ==================== API TOKEN AUTHENTICATION ====================

def verify_api_token(request):
    """
    Verify API token from Authorization header.
    Returns: APIToken object if valid, None otherwise.
    """
    # Skip token verification for local requests (optional)
    # if request.META.get('REMOTE_ADDR') in ['127.0.0.1', 'localhost']:
    #     return True
    
    auth_header = request.headers.get('Authorization', '')
    
    # Check if Bearer token is present
    if not auth_header.startswith('Bearer '):
        logger.warning(f"Missing or invalid Authorization header: {auth_header[:20]}")
        return None
    
    token_key = auth_header.split(' ')[1]
    
    try:
        # Import APIToken model
        from .models import APIToken
        token = APIToken.objects.get(token=token_key, is_active=True)
        logger.info(f"API token verified: {token.name} for room {token.room.name if token.room else 'all'}")
        return token
    except ImportError:
        # APIToken model not yet created
        logger.warning("APIToken model not found. Please run migrations.")
        return None
    except Exception as e:
        logger.warning(f"Invalid API token: {e}")
        return None


def generate_api_token():
    """
    Generate a new random API token.
    Returns: string token
    """
    return secrets.token_urlsafe(32)


def _bad_json_response():
    return JsonResponse({'status': 'error', 'message': 'Invalid JSON payload'}, status=400)


def _to_float(value, field_name):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{field_name} must be a number')
    return parsed


def verify_paymongo_signature(request):
    """Verify PayMongo webhook signatures when a webhook secret is configured."""
    webhook_secret = getattr(settings, 'PAYMONGO_WEBHOOK_SECRET', None)
    if not webhook_secret:
        logger.warning("PAYMONGO_WEBHOOK_SECRET is not configured; webhook signature check skipped.")
        return True

    signature_header = (
        request.headers.get('Paymongo-Signature')
        or request.headers.get('PayMongo-Signature')
        or request.headers.get('X-Paymongo-Signature')
        or ''
    )
    if not signature_header:
        logger.warning("Missing PayMongo webhook signature header.")
        return False

    signatures = {}
    for part in signature_header.split(','):
        if '=' in part:
            key, value = part.split('=', 1)
            signatures[key.strip()] = value.strip()

    timestamp = signatures.get('t')
    signed_payload = request.body
    if timestamp:
        signed_payload = f'{timestamp}.'.encode('utf-8') + request.body

    expected = hmac.new(
        webhook_secret.encode('utf-8'),
        signed_payload,
        hashlib.sha256
    ).hexdigest()

    possible_signatures = [
        signatures.get('te'),
        signatures.get('li'),
        signatures.get('v1'),
        signature_header.strip(),
    ]
    return any(
        sig and hmac.compare_digest(expected, sig)
        for sig in possible_signatures
    )


# ==================== MAIN API ENDPOINTS ====================

@csrf_exempt
@require_http_methods(["POST"])
def meter_reading(request):
    try:
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return _bad_json_response()

        auth_header = request.headers.get('Authorization', '')
        token = verify_api_token(request) if auth_header else None
        auth_required = getattr(settings, 'IOT_API_TOKEN_REQUIRED', False)
        if auth_required and token is None:
            return JsonResponse({'status': 'error', 'message': 'Invalid or missing API token'}, status=401)

        if isinstance(data, list):
            return process_batch_readings(data, token)
        if isinstance(data, dict) and 'readings' in data:
            return process_batch_readings(data.get('readings') or [], token)
        if not isinstance(data, dict):
            return JsonResponse({'status': 'error', 'message': 'Payload must be an object or list'}, status=400)
        
        room_name = data.get('room')
        kwh = data.get('kwh')
        voltage = data.get('vrms', 0)     # optional
        current = data.get('irms', 0)     # optional
        power = data.get('power', 0)      # optional
        
        if not room_name or kwh is None:
            return JsonResponse({'status': 'error', 'message': 'Missing room or kwh'}, status=400)

        try:
            kwh = _to_float(kwh, 'kwh')
            voltage = _to_float(voltage, 'vrms')
            current = _to_float(current, 'irms')
            power = _to_float(power, 'power')
            if kwh < 0:
                return JsonResponse({'status': 'error', 'message': 'kwh cannot be negative'}, status=400)
        except ValueError as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
        
        try:
            room = Room.objects.get(name=room_name)
        except Room.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': f'Room {room_name} not found'}, status=404)

        if token and token.room and token.room != room:
            return JsonResponse({'status': 'error', 'message': f'Token not authorized for room {room_name}'}, status=403)
        
        # Save reading
        usage = EnergyUsage.objects.create(
            room=room,
            kwh=kwh,
            voltage=voltage,
            current=current,
            power=power
        )
        
        return JsonResponse({
            'status': 'success',
            'message': f'Saved {kwh}kWh for {room_name}',
            'data': {
                'id': usage.id,
                'room': room.name,
                'kwh': kwh,
                'voltage': voltage,
                'current': current,
                'power': power,
                'timestamp': usage.timestamp
            }
        })
        
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


def process_single_reading(data, token=None):
    """Process a single meter reading with optional token validation"""
    room_name = data.get('room')
    kwh = data.get('kwh')
    timestamp_str = data.get('timestamp')
    voltage = data.get('vrms', 0)
    current = data.get('irms', 0)
    power = data.get('power', 0)
    
    # Validate required fields
    if not room_name or kwh is None:
        return JsonResponse({
            'status': 'error',
            'message': 'Missing required fields: room and kwh'
        }, status=400)
    
    # Validate kwh is a number
    try:
        kwh = _to_float(kwh, 'kwh')
        voltage = _to_float(voltage, 'vrms')
        current = _to_float(current, 'irms')
        power = _to_float(power, 'power')
        if kwh < 0:
            return JsonResponse({
                'status': 'error',
                'message': 'kwh cannot be negative'
            }, status=400)
    except ValueError as e:
        return JsonResponse({
            'status': 'error',
            'message': str(e)
        }, status=400)
    
    # Find the room
    try:
        room = Room.objects.get(name=room_name)
    except Room.DoesNotExist:
        return JsonResponse({
            'status': 'error',
            'message': f'Room {room_name} not found'
        }, status=404)
    
    # Optional: Check if token is allowed to send to this room
    if token and token.room and token.room != room:
        logger.warning(f"Token {token.name} attempted to send to wrong room: {room_name}")
        return JsonResponse({
            'status': 'error',
            'message': f'Token not authorized for room {room_name}'
        }, status=403)
    
    # Parse timestamp if provided
    if timestamp_str:
        try:
            from datetime import datetime
            timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
            # Make it timezone aware
            timestamp = timezone.make_aware(timestamp)
        except ValueError:
            return JsonResponse({
                'status': 'error',
                'message': 'Invalid timestamp format. Use: YYYY-MM-DD HH:MM:SS'
            }, status=400)
    else:
        timestamp = timezone.now()
    
    # Save the reading
    usage = EnergyUsage.objects.create(
        room=room,
        kwh=kwh,
        voltage=voltage,
        current=current,
        power=power,
        timestamp=timestamp
    )
    
    # Check for immediate alerts (optional)
    check_immediate_alerts(room, kwh)
    
    return JsonResponse({
        'status': 'success',
        'message': f'Saved {kwh}kWh for {room_name}',
        'data': {
            'id': usage.id,
            'room': room.name,
            'kwh': kwh,
            'voltage': voltage,
            'current': current,
            'power': power,
            'timestamp': usage.timestamp,
            'date': usage.date
        }
    })


def process_batch_readings(readings, token=None):
    """Process multiple readings at once"""
    if not isinstance(readings, list):
        return JsonResponse({
            'status': 'error',
            'message': 'readings must be a list'
        }, status=400)

    results = {
        'success': [],
        'failed': []
    }
    
    for idx, reading in enumerate(readings):
        try:
            if not isinstance(reading, dict):
                results['failed'].append({
                    'index': idx,
                    'error': 'Reading must be an object'
                })
                continue

            result = process_single_reading(reading, token)
            result_data = json.loads(result.content)
            
            if result.status_code == 200:
                results['success'].append({
                    'index': idx,
                    'data': result_data.get('data')
                })
            else:
                results['failed'].append({
                    'index': idx,
                    'error': result_data.get('message')
                })
        except Exception as e:
            results['failed'].append({
                'index': idx,
                'error': str(e)
            })
    
    return JsonResponse({
        'status': 'complete',
        'summary': {
            'total': len(readings),
            'success': len(results['success']),
            'failed': len(results['failed'])
        },
        'results': results
    })


def check_immediate_alerts(room, kwh):
    """Check for immediate alerts based on reading"""
    from django.db.models import Sum, Avg
    
    # Get today's total usage
    today = timezone.now().date()
    today_total = EnergyUsage.objects.filter(
        room=room,
        timestamp__date=today
    ).aggregate(total=Sum('kwh'))['total'] or 0
    
    # Check if approaching limit
    if today_total > room.limit * 0.8:  # 80% of limit
        Alert.objects.create(
            room=room,
            alert_type='high_consumption',
            message=f"⚠️ You've used {today_total:.1f} kWh today ({int((today_total/room.limit)*100)}% of monthly limit)"
        )
    
    # Check for unusually high reading
    avg_daily = EnergyUsage.objects.filter(
        room=room,
        timestamp__date__gte=today - timezone.timedelta(days=7)
    ).aggregate(avg=Avg('kwh'))['avg'] or 0
    
    if kwh > avg_daily * 3 and avg_daily > 0:
        Alert.objects.create(
            room=room,
            alert_type='abnormal_usage',
            message=f"⚠️ Unusually high reading: {kwh}kWh ({(kwh/avg_daily):.1f}x your average)"
        )


@csrf_exempt
@require_http_methods(["GET"])
def device_info(request):
    """Endpoint for IoT device to get configuration"""
    return JsonResponse({
        'status': 'success',
        'server_time': timezone.now().isoformat(),
        'version': '1.0',
        'endpoints': {
            'meter_reading': '/api/meter-reading/',
            'device_info': '/api/device-info/'
        },
        'supported_formats': ['single', 'batch'],
        'auth_required': getattr(settings, 'IOT_API_TOKEN_REQUIRED', False),
        'auth_type': 'Bearer Token'
    })


# ==================== TOKEN MANAGEMENT (for admin) ====================

@csrf_exempt
@require_http_methods(["POST"])
def create_api_token(request):
    """Admin endpoint to create new API tokens"""
    from .models import APIToken, Room
    
    if not request.user.is_authenticated or request.user.userprofile.user_type != 'owner':
        return JsonResponse({'status': 'error', 'message': 'Unauthorized'}, status=403)
    
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return _bad_json_response()
    name = data.get('name')
    room_name = data.get('room')
    
    if not name:
        return JsonResponse({'status': 'error', 'message': 'Name required'}, status=400)
    
    room = None
    if room_name:
        try:
            room = Room.objects.get(name=room_name)
        except Room.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': f'Room {room_name} not found'}, status=404)
    
    token_value = generate_api_token()
    
    token = APIToken.objects.create(
        name=name,
        token=token_value,
        room=room,
        is_active=True
    )
    
    return JsonResponse({
        'status': 'success',
        'data': {
            'id': token.id,
            'name': token.name,
            'token': token.token,
            'room': token.room.name if token.room else 'All rooms',
            'created_at': token.created_at
        }
    })


# ==================== PAYMONGO WEBHOOK ====================

@csrf_exempt
@require_POST
def paymongo_webhook(request):
    """Handle PayMongo webhook callbacks for payment status updates"""
    try:
        if not verify_paymongo_signature(request):
            return JsonResponse({'status': 'error', 'message': 'Invalid webhook signature'}, status=401)

        payload = request.body
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return JsonResponse({'status': 'error', 'message': 'Invalid JSON'}, status=400)
        
        # SAFE EXTRACTION - handle None values
        data_obj = data.get('data')
        if data_obj is None:
            data_obj = {}
        
        attrs = data_obj.get('attributes', {})
        if attrs is None:
            attrs = {}
        
        event_type = attrs.get('type', '')
        
        # Get payment data safely
        event_data_obj = attrs.get('data')
        if event_data_obj is None:
            event_data_obj = {}
        
        event_attrs = event_data_obj.get('attributes', {})
        if event_attrs is None:
            event_attrs = {}
        
        checkout_id = event_data_obj.get('id')
        description = event_attrs.get('description', '') or ''
        status = event_attrs.get('status', '') or ''
        
        # Extract reference number
        reference_number = None
        if description:
            import re
            match = re.search(r'Ref:\s*(PAY-[A-Z0-9]+-\d+-[a-f0-9]+)', description, re.IGNORECASE)
            if match:
                reference_number = match.group(1)
        
        paid_event = event_type in ['checkout_session.payment.paid', 'payment.paid'] or status == 'paid'
        
        if paid_event and reference_number:
            from .models import Payment
            payment = Payment.objects.filter(reference_number=reference_number, status='pending').first()
            
            if payment:
                payment.status = 'paid'
                payment.paid_at = timezone.now()
                payment.transaction_id = checkout_id
                payment.webhook_received = True
                payment.webhook_data = data
                payment.save()
                
                bill = payment.bill
                bill.is_paid = True
                bill.save()
                
                Alert.objects.create(
                    room=bill.room,
                    alert_type='billing',
                    message=f"✅ Payment of ₱{payment.amount} for {bill.billing_month} confirmed via GCash"
                )
                
                print(f"✅ Payment {reference_number} marked as paid via WEBHOOK!")
            else:
                print(f"⚠️ Payment not found for reference: {reference_number}")
        
        return JsonResponse({'status': 'success'}, status=200)
        
    except Exception as e:
        print(f"❌ Webhook error: {str(e)}")
        import traceback
        traceback.print_exc()
        return JsonResponse({'error': str(e)}, status=500)
        
# ==================== ROOM STATUS API ====================

def room_status(request, room_name):
    """API endpoint para makuha ng ESP32 ang power_status ng room"""
    try:
        room = Room.objects.get(name=room_name)
        return JsonResponse({
            'room': room.name,
            'power_status': room.power_status
        })
    except Room.DoesNotExist:
        return JsonResponse({'error': 'Room not found'}, status=404)

@login_required
def get_building_stats(request):
    profile = request.user.userprofile
    if profile.user_type != 'owner':
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    
    from django.db.models import Sum
    from datetime import datetime, timedelta
    from .models import Billing  # ✅ IMPORTANTE: I-import ang Billing
    
    start_date = request.GET.get('start')
    end_date = request.GET.get('end')
    
    if start_date and end_date:
        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')
    else:
        today = datetime.now()
        start = datetime(today.year, today.month, 1)
        end = today
    
    rooms = Room.objects.all()
    room_names = []
    room_usages = []
    daily_building = []
    days = []
    
    # Get daily totals
    current = start
    while current <= end:
        day_total = EnergyUsage.objects.filter(
            timestamp__date=current.date()
        ).aggregate(total=Sum('kwh'))['total'] or 0
        daily_building.append(round(day_total, 2))
        days.append(current.strftime('%b %d'))
        current += timedelta(days=1)
    
    # Get room totals
    total_building_usage = 0
    occupied_rooms_count = 0
    room_stats = []
    
    for room in rooms:
        total_kwh = EnergyUsage.objects.filter(
            room=room,
            timestamp__date__gte=start.date(),
            timestamp__date__lte=end.date()
        ).aggregate(total=Sum('kwh'))['total'] or 0
        
        room_names.append(room.name)
        room_usages.append(round(total_kwh, 2))
        
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
    avg_per_room = round(total_building_usage / occupied_rooms_count, 2) if occupied_rooms_count > 0 else 0
    
    # ========== ✅ BAGONG CODE: Current Bill Total ==========
    month_name = end.strftime("%B %Y")
    
    # Kunin ang total unpaid bills para sa current month
    total_bill = 0
    try:
        total_bill = sum(
            bill.cost for bill in Billing.objects.filter(
                billing_month=month_name,
                is_paid=False
            )
        )
    except Exception as e:
        print(f"Error getting total bill: {e}")
        total_bill = 0
    
    return JsonResponse({
        'total_rooms': len(rooms),
        'occupied_rooms': occupied_rooms_count,
        'total_usage': round(total_building_usage, 2),
        'avg_per_room': avg_per_room,
        'room_stats': room_stats,
        'room_names': room_names,
        'room_usages': room_usages,
        'daily_building': daily_building,
        'days': days,
        'month': month_name,
        'current_bill_total': float(total_bill),           # ✅ BAGO
        'total_kwh_this_month': round(total_building_usage, 2),  # ✅ BAGO
    })

@login_required
def bill_details(request, bill_id):
    """API endpoint para makuha ang detailed breakdown ng bill"""
    try:
        bill = Billing.objects.get(id=bill_id)
        
        # I-check kung ang user ay may access sa bill na ito
        if not request.user.is_authenticated:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        
        # Kung tenant, i-check kung sa kanila ang bill
        if hasattr(request.user, 'userprofile') and request.user.userprofile.user_type == 'tenant':
            if bill.room != request.user.userprofile.room:
                return JsonResponse({'error': 'Unauthorized - This bill does not belong to you'}, status=403)
        
        late_fee = 0
        total = bill.cost
        
        # Kunin ang payment record kung paid
        paid_date = None
        if bill.is_paid:
            payment = Payment.objects.filter(bill=bill, status='paid').first()
            if payment and payment.paid_at:
                paid_date = payment.paid_at.strftime('%Y-%m-%d %H:%M:%S')
        
        # Kunin ang electricity rate mula sa settings
        from .models import SystemSettings, TenantAssignment
        settings = SystemSettings.get_settings()
        electricity_rate = settings.electricity_rate
        assignment = bill.tenant_assignment or TenantAssignment.objects.filter(room=bill.room, is_active=True).first()
        move_in_date = assignment.move_in_date if assignment else None
        due_date = assignment.get_due_date() if assignment else bill.due_date
        today = timezone.now().date()
        if move_in_date:
            cycle_end = min(today, due_date) if due_date else today
            days_occupied = max((cycle_end - move_in_date).days + 1, 0)
        else:
            days_occupied = bill.days_occupied if hasattr(bill, 'days_occupied') else 30

        previous = Billing.objects.filter(
            room=bill.room,
            created_at__lt=bill.created_at
        ).order_by('-created_at').first()
        previous_bill = None
        if previous:
            previous_bill = {
                'billing_month': previous.billing_month,
                'kwh': round(previous.kwh, 2),
                'amount': round(previous.cost, 2),
                'status': 'PAID' if previous.is_paid else 'UNPAID',
                'is_paid': previous.is_paid,
            }

        payments = Payment.objects.filter(bill=bill).order_by('-created_at')
        
        return JsonResponse({
            'bill_id': bill.id,
            'billing_month': bill.billing_month,
            'previous_bill': previous_bill,
            'room_name': bill.room.name,
            'kwh': round(bill.kwh, 2),
            'rate': round(electricity_rate, 2),
            'base_amount': round(bill.cost, 2),
            'formula': f"{bill.kwh:.2f} kWh x PHP {electricity_rate:.2f}/kWh",
            'late_fee': late_fee,
            'total_amount': round(total, 2),
            'move_in_date': move_in_date.strftime('%Y-%m-%d') if move_in_date else 'N/A',
            'cycle_start': move_in_date.strftime('%Y-%m-%d') if move_in_date else 'N/A',
            'due_date': due_date.strftime('%Y-%m-%d') if due_date else 'N/A',
            'is_paid': bill.is_paid,
            'paid_date': paid_date,
            'days_occupied': days_occupied,
            'payment_history': [
                {
                    'method': payment.get_payment_method_display(),
                    'reference_number': payment.reference_number,
                    'transaction_id': payment.transaction_id or '',
                    'status': payment.status.upper(),
                    'amount': round(payment.amount, 2),
                    'created_at': payment.created_at.strftime('%Y-%m-%d %H:%M'),
                    'paid_at': payment.paid_at.strftime('%Y-%m-%d %H:%M') if payment.paid_at else '',
                }
                for payment in payments
            ]
        })
        
    except Billing.DoesNotExist:
        return JsonResponse({'error': 'Bill not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from .models import UserProfile

@login_required
def tenant_details_api(request, tenant_id):
    """API to get tenant details for admin"""
    # Check if user is authenticated and has profile
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Unauthorized'}, status=401)
    
    # Check if user is owner
    try:
        profile = request.user.userprofile
        if profile.user_type != 'owner':
            return JsonResponse({'error': 'Unauthorized - Owner only'}, status=403)
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'User profile not found'}, status=403)
    
    try:
        tenant = UserProfile.objects.get(id=tenant_id, user_type='tenant')
        
        return JsonResponse({
            'id': tenant.id,
            'username': tenant.user.username,
            'first_name': tenant.user.first_name,
            'middle_name': tenant.middle_name or '',
            'last_name': tenant.user.last_name,
            'email': tenant.user.email,
            'phone_number': tenant.phone_number or '',
            'emergency_person': tenant.emergency_contact_person or '',
            'emergency_number': tenant.emergency_contact_number or '',
            'occupants': tenant.number_of_occupants or 1,
            'employment_status': tenant.employment_status or '',
            'agree_terms': tenant.agreed_to_terms,
            'agree_privacy': tenant.agreed_to_privacy,
            'is_approved': tenant.is_approved,
            'room': tenant.room.name if tenant.room else None,
            'room_id': tenant.room.id if tenant.room else None,
            'created_at': tenant.created_at.strftime('%B %d, %Y'),
            'valid_id_file': tenant.valid_id_file or '',
            'selfie_file': tenant.selfie_verification_file or '',
        })
    except UserProfile.DoesNotExist:
        return JsonResponse({'error': 'Tenant not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
