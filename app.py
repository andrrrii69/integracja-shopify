import os
import hmac
import hashlib
import base64
import time
from datetime import datetime, timedelta

from flask import Flask, request, abort
import requests

app = Flask(__name__)

# CONFIG
SHOPIFY_WEBHOOK_SECRET = os.getenv('SHOPIFY_WEBHOOK_SECRET', '').encode('utf-8')
INFAKT_API_KEY = os.getenv('INFAKT_API_KEY')
HOST = os.getenv('INFAKT_HOST', 'api.infakt.pl')
VAT_ENDPOINT = f'https://{HOST}/api/v3/invoices.json'

HEADERS = {
    'X-inFakt-ApiKey': INFAKT_API_KEY,
    'Content-Type': 'application/json',
    'Accept': 'application/json',
}

def verify_shopify_webhook(data: bytes, hmac_header: str) -> bool:
    computed = base64.b64encode(hmac.new(SHOPIFY_WEBHOOK_SECRET, data, hashlib.sha256).digest())
    return hmac.compare_digest(computed.decode('utf-8'), hmac_header)

@app.route('/', methods=['GET'])
def healthcheck():
    return 'OK', 200

@app.route('/webhook/orders/create', methods=['POST'])
def orders_create():
    raw = request.get_data()
    signature = request.headers.get('X-Shopify-Hmac-Sha256', '')
    if not verify_shopify_webhook(raw, signature):
        abort(401, 'Invalid HMAC signature')

    order = request.get_json()
    billing = order.get('billing_address') or order.get('customer', {}).get('default_address', {})

    # Client fields
    client_fields = {
        'client_first_name': billing.get('first_name', ''),
        'client_last_name': billing.get('last_name', ''),
        'client_company_name': billing.get('company', ''),
        'client_street': billing.get('address1', ''),
        'client_flat_number': billing.get('address2', ''),
        'client_city': billing.get('city', ''),
        'client_post_code': billing.get('zip', ''),
    }
    # Only include NIP if provided
    clean_nip = billing.get('nip') or billing.get('company_nip')
    if clean_nip:
        client_fields['client_tax_code'] = clean_nip

    # Build 'services' array
    services = []
    for item in order.get('line_items', []):
        qty = item['quantity']
        gross_per_unit = int(round(float(item['price']) * 100))
        net_unit = int(round(gross_per_unit / 1.23))
        tax_unit = gross_per_unit - net_unit
        services.append({
            'name': item['title'],
            'tax_symbol': '23',
            'quantity': qty,
            'unit_net_price': net_unit,
            'unit_cost': net_unit,
            'gross_price': gross_per_unit * qty,
            'tax_price': tax_unit * qty,
            'flat_rate_tax_symbol': '3',
        })

    created = datetime.strptime(order['created_at'], '%Y-%m-%dT%H:%M:%S%z')
    sell_date = created.date().isoformat()
    issue_date = sell_date
    due_date = (created + timedelta(days=7)).date().isoformat()

    payload = {
        'invoice': {
            'kind': 'vat',
            'series': os.getenv('INFAKT_SERIES', 'A'),
            'status': 'draft',
            'sell_date': sell_date,
            'issue_date': issue_date,
            'payment_due_date': due_date,
            'payment_method': 'transfer',
            'currency': 'PLN',
            **client_fields,
            'services': services
        }
    }

    resp = requests.post(VAT_ENDPOINT, json=payload, headers=HEADERS)
    if not resp.ok:
        app.logger.error(f"[inFakt VAT ERROR] status={resp.status_code}, body={resp.text}")
        resp.raise_for_status()
    app.logger.info("VAT invoice created: %s", resp.json())
    return '', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))