import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from .models import EnergyUsage, Room, Alert
import logging

logger = logging.getLogger(__name__)

@csrf_exempt
@require_http_methods(["POST"])
def meter_reading(request):
    """
    Enhanced API endpoint para sa IoT device (ESP32)
    Accepts: 
        - Single reading: {"room": "R01", "kwh": 0.5}
        - Multiple readings: {"readings": [{"room": "R01", "kwh": 0.5}, ...]}
        - With timestamp: {"room": "R01", "kwh": 0.5, "timestamp": "2026-03-14 10:30:00"}
    """
    try:
        # Log the request
        logger.info(f"Received meter reading request: {request.body}")
        
        # Parse JSON data
        data = json.loads(request.body)
        
        # Check if it's a batch of readings
        if 'readings' in data and isinstance(data['readings'], list):
            return process_batch_readings(data['readings'])
        
        # Single reading
        return process_single_reading(data)
        
    except json.JSONDecodeError:
        logger.error("Invalid JSON received")
        return JsonResponse({
            'status': 'error',
            'message': 'Invalid JSON format'
        }, status=400)
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return JsonResponse({
            'status': 'error',
            'message': f'Server error: {str(e)}'
        }, status=500)


def process_single_reading(data):
    """Process a single meter reading"""
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


def process_batch_readings(readings):
    """Process multiple readings at once"""
    results = {
        'success': [],
        'failed': []
    }
    
    for idx, reading in enumerate(readings):
        try:
            result = process_single_reading(reading)
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
        'supported_formats': ['single', 'batch']
    })