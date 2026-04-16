import json
import secrets
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
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
    """
    Enhanced API endpoint para sa IoT device (ESP32)
    Accepts: 
        - Single reading: {"room": "R01", "kwh": 0.5}
        - Multiple readings: {"readings": [{"room": "R01", "kwh": 0.5}, ...]}
        - With timestamp: {"room": "R01", "kwh": 0.5, "timestamp": "2026-03-14 10:30:00"}
    
    Requires: Authorization: Bearer <API_TOKEN>
    """
    # ==================== TOKEN VERIFICATION ====================
    #token = verify_api_token(request)
    
    # Check if token is required (you can disable for local testing)
    #TOKEN_REQUIRED = True  # Set to False for local testing only
    
    #if TOKEN_REQUIRED and not token:
     #   return JsonResponse({
      #      'status': 'error',
       #     'message': 'Invalid or missing API token. Please provide valid Authorization: Bearer <token>'
        #}, status=401)
    
    #try:
        # Log the request (mask token)
     #   safe_body = request.body.decode('utf-8')[:200] if request.body else ''
      #  logger.info(f"Received meter reading request: {safe_body}")
        
        # Parse JSON data
       # data = json.loads(request.body)
        
        # Check if it's a batch of readings
        #if 'readings' in data and isinstance(data['readings'], list):
         #   return process_batch_readings(data['readings'], token)
        
        # Single reading
        #return process_single_reading(data, token)
        
    #except json.JSONDecodeError:
     #   logger.error("Invalid JSON received")
      #  return JsonResponse({
       #     'status': 'error',
        #    'message': 'Invalid JSON format'
        #}, status=400)
    #except Exception as e:
     #   logger.error(f"Unexpected error: {str(e)}")
      #  return JsonResponse({
       #     'status': 'error',
        #    'message': f'Server error: {str(e)}'
        #}, status=500)


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
    import models
    
    # Get today's total usage
    today = timezone.now().date()
    today_total = EnergyUsage.objects.filter(
        room=room,
        timestamp__date=today
    ).aggregate(total=models.Sum('kwh'))['total'] or 0
    
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
    ).aggregate(avg=models.Avg('kwh'))['avg'] or 0
    
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
    from django.utils import timezone
    
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

from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from django.utils import timezone
import json
import hmac
import hashlib

@csrf_exempt
@require_POST
def paymongo_webhook(request):
    """
    PayMongo webhook endpoint for payment updates
    This is called by PayMongo when payment status changes
    """
    try:
        # Get the raw request body
        payload = request.body
        signature = request.headers.get('Paymongo-Signature', '')
        
        print(f"📡 Webhook received!")
        print(f"   Signature: {signature[:50] if signature else 'None'}...")
        
        # Parse the webhook data
        data = json.loads(payload)
        
        # Get event type
        event_data = data.get('data', {})
        event_attributes = event_data.get('attributes', {})
        event_type = event_attributes.get('type')
        
        print(f"   Event type: {event_type}")
        
        # Handle checkout_session.payment.paid event
        if event_type == 'checkout_session.payment.paid':
            checkout_data = event_attributes.get('data', {})
            checkout_id = checkout_data.get('id')
            
            print(f"   Checkout ID: {checkout_id}")
            
            if checkout_id:
                # Find the payment record
                from .models import Payment, Alert
                payment = Payment.objects.filter(
                    checkout_session_id=checkout_id, 
                    status='pending'
                ).first()
                
                if payment:
                    print(f"✅ Payment found: {payment.reference_number}")
                    
                    # Mark as paid
                    payment.status = 'paid'
                    payment.webhook_received = True
                    payment.webhook_data = data
                    payment.paid_at = timezone.now()
                    payment.save()
                    
                    # Update the bill
                    bill = payment.bill
                    bill.is_paid = True
                    bill.save()
                    
                    # Create alert
                    Alert.objects.create(
                        room=bill.room,
                        alert_type='billing',
                        message=f"✅ Payment received for {bill.billing_month} via GCash. Reference: {payment.reference_number}"
                    )
                    
                    print(f"✅ Payment confirmed for {payment.reference_number}")
                else:
                    print(f"⚠️ No pending payment found for checkout_id: {checkout_id}")
        
        return JsonResponse({'status': 'success'}, status=200)
        
    except Exception as e:
        print(f"❌ Webhook error: {str(e)}")
        return JsonResponse({'error': str(e)}, status=500)