import json
import secrets
import hmac
import hashlib
import re
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from django.utils import timezone
from .models import EnergyUsage, Room, Alert

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


# ==================== MAIN API ENDPOINTS ====================

@csrf_exempt
@require_http_methods(["POST"])
def meter_reading(request):
    try:
        data = json.loads(request.body)
        
        room_name = data.get('room')
        kwh = data.get('kwh')
        voltage = data.get('vrms', 0)     # optional
        current = data.get('irms', 0)     # optional
        power = data.get('power', 0)      # optional
        
        if not room_name or kwh is None:
            return JsonResponse({'status': 'error', 'message': 'Missing room or kwh'}, status=400)
        
        try:
            room = Room.objects.get(name=room_name)
        except Room.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': f'Room {room_name} not found'}, status=404)
        
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
    
    # Validate required fields
    if not room_name or kwh is None:
        return JsonResponse({
            'status': 'error',
            'message': 'Missing required fields: room and kwh'
        }, status=400)
    
    # Validate kwh is a number
    try:
        kwh = float(kwh)
        if kwh < 0:
            return JsonResponse({
                'status': 'error',
                'message': 'kwh cannot be negative'
            }, status=400)
    except ValueError:
        return JsonResponse({
            'status': 'error',
            'message': 'kwh must be a number'
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
            'timestamp': usage.timestamp,
            'date': usage.date
        }
    })


def process_batch_readings(readings, token=None):
    """Process multiple readings at once"""
    results = {
        'success': [],
        'failed': []
    }
    
    for idx, reading in enumerate(readings):
        try:
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
        'auth_required': True,
        'auth_type': 'Bearer Token'
    })


# ==================== TOKEN MANAGEMENT (for admin) ====================

@csrf_exempt
@require_http_methods(["POST"])
def create_api_token(request):
    """Admin endpoint to create new API tokens"""
    from .models import APIToken, Room
    
    # You may want to add admin authentication here
    
    data = json.loads(request.body)
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
        payload = request.body
        data = json.loads(payload)
        event_type = data.get('data', {}).get('attributes', {}).get('type', '')
        
        print(f"📡 Webhook received: {event_type}")
        
        if event_type == 'checkout_session.payment.paid':
            checkout_data = data.get('data', {}).get('attributes', {}).get('data', {})
            checkout_id = checkout_data.get('id')
            description = checkout_data.get('attributes', {}).get('description', '')
            
            # Extract reference_number from description
            import re
            reference_number = None
            match = re.search(r'Ref:\s*(PAY-[A-Z0-9]+-\d+-[a-f0-9]+)', description)
            if match:
                reference_number = match.group(1)
            
            if reference_number:
                from .models import Payment
                payment = Payment.objects.filter(reference_number=reference_number, status='pending').first()
                
                if payment:
                    # ✅ WEBHOOK LANG ANG GUMAWA NITO
                    payment.status = 'paid'
                    payment.paid_at = timezone.now()
                    payment.transaction_id = checkout_id
                    payment.webhook_received = True
                    payment.webhook_data = data
                    payment.save()
                    
                    bill = payment.bill
                    bill.is_paid = True
                    bill.save()
                    
                    print(f"✅ Payment {reference_number} marked as paid via WEBHOOK!")
                else:
                    print(f"⚠️ Payment not found for reference: {reference_number}")
        
        return JsonResponse({'status': 'success'}, status=200)
        
    except Exception as e:
        print(f"❌ Webhook error: {str(e)}")
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