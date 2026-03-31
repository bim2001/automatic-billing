import requests
import json
import secrets
from django.conf import settings

class PayMongoSimulator:
    """Simulate PayMongo payment gateway for testing"""
    
    @staticmethod
    def create_checkout_session(amount, description, success_url, cancel_url, reference):
        """Create a checkout session (simulated)"""
        
        # This is a simulation - no actual payment
        checkout_url = f"/payment/checkout/{reference}/"
        
        return {
            'status': 'success',
            'data': {
                'id': f'sim_{secrets.token_hex(16)}',
                'attributes': {
                    'checkout_url': checkout_url,
                    'amount': int(amount * 100),
                    'description': description,
                    'reference_number': reference,
                    'status': 'pending'
                }
            }
        }
    
    @staticmethod
    def verify_payment(payment_id):
        """Verify payment status (simulated)"""
        # In simulation, assume payment is successful
        return {
            'status': 'success',
            'data': {
                'attributes': {
                    'status': 'paid',
                    'paid_at': '2026-03-30T10:00:00Z'
                }
            }
        }

class PayMongoLive:
    """Actual PayMongo API integration (for production)"""
    
    @staticmethod
    def create_checkout_session(amount, description, success_url, cancel_url, reference):
        headers = {
            'Authorization': f'Basic {settings.PAYMONGO_SECRET_KEY}',
            'Content-Type': 'application/json',
        }
        
        data = {
            'data': {
                'attributes': {
                    'amount': int(amount * 100),
                    'description': description,
                    'success_url': success_url,
                    'cancel_url': cancel_url,
                    'payment_method_types': ['gcash'],
                    'reference_number': reference,
                }
            }
        }
        
        response = requests.post(
            'https://api.paymongo.com/v1/checkout_sessions',
            headers=headers,
            json=data
        )
        
        return response.json()
    
    @staticmethod
    def verify_payment(checkout_id):
        headers = {
            'Authorization': f'Basic {settings.PAYMONGO_SECRET_KEY}',
        }
        
        response = requests.get(
            f'https://api.paymongo.com/v1/checkout_sessions/{checkout_id}',
            headers=headers
        )
        
        return response.json()

# Use this to switch between simulation and live
def get_paymongo():
    if getattr(settings, 'PAYMONGO_MODE', 'test') == 'live':
        return PayMongoLive()
    return PayMongoSimulator()