import requests
import json
import hmac
import hashlib
import base64
from django.conf import settings

class PayMongoClient:
    """PayMongo API integration"""
    
    def __init__(self):
        self.secret_key = settings.PAYMONGO_SECRET_KEY
        self.base_url = "https://api.paymongo.com/v1"
        
        # Proper Basic Auth format: Base64(secret_key:)
        auth_string = f"{self.secret_key}:"
        encoded_auth = base64.b64encode(auth_string.encode()).decode()
        
        self.headers = {
            'Authorization': f'Basic {encoded_auth}',
            'Content-Type': 'application/json',
        }
        
        # Get the base URL from settings (para dynamic)
        self.app_base_url = getattr(settings, 'APP_BASE_URL', 'http://127.0.0.1:8000')
    
    def create_checkout_session(self, amount, description, success_url, cancel_url, reference):
        """Create a checkout session with line_items"""
        
        # Kung ang success_url ay localhost, palitan ng external URL
        # Ito ay fallback - pero mas maganda kung ang dumadaan na success_url ay tama na
        if '127.0.0.1' in success_url or 'localhost' in success_url:
            # Gamitin ang APP_BASE_URL from settings
            base = self.app_base_url
            # Extract the path from the original URL
            path = success_url.split('/payment/success/')[-1] if '/payment/success/' in success_url else reference
            corrected_success_url = f"{base}/payment/success/{path}"
            corrected_cancel_url = f"{base}/tenant/"
            
            print(f"⚠️ Fixed success URL from {success_url} to {corrected_success_url}")
            success_url = corrected_success_url
            cancel_url = corrected_cancel_url
        
        data = {
            'data': {
                'attributes': {
                    'line_items': [{
                        'name': description,
                        'amount': int(amount * 100),  # Convert to centavos
                        'quantity': 1,
                        'currency': 'PHP',
                    }],
                    'payment_method_types': ['gcash'],
                    'success_url': success_url,
                    'cancel_url': cancel_url,
                    'description': description,
                    'reference_number': reference,
                    'send_email_receipt': True,
                }
            }
        }
        
        print(f"🔗 Creating checkout session with:")
        print(f"   Success URL: {success_url}")
        print(f"   Cancel URL: {cancel_url}")
        
        response = requests.post(
            f'{self.base_url}/checkout_sessions',
            headers=self.headers,
            json=data
        )
        
        if response.status_code == 200 or response.status_code == 201:
            return response.json()
        else:
            print(f"PayMongo error: {response.text}")
            return {'error': response.text}
    
    def get_checkout_session(self, checkout_id):
        """Retrieve checkout session status"""
        response = requests.get(
            f'{self.base_url}/checkout_sessions/{checkout_id}',
            headers=self.headers
        )
        return response.json()
    
    def verify_webhook_signature(self, payload, signature):
        """Verify webhook signature for security"""
        webhook_secret = settings.PAYMONGO_WEBHOOK_SECRET
        expected = hmac.new(
            webhook_secret.encode('utf-8'),
            payload,
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature, expected)

def get_paymongo():
    return PayMongoClient()