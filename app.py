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
CREATE_ENDPOINT = f'https://{HOST}/api/v3/async/invoices.json'
STATUS_ENDPOINT_TMPL = f'https://{HOST}/api/v3/async/invoices/status/{{ref}}.json'
CLIENTS_ENDPOINT = f'https://{HOST}/api/v3/clients.json'

HEADERS = {
    'X-inFakt-ApiKey': INFAKT_API_KEY,
    'Content-Type': 'application/json',
    'Accept': 'application/json',
}

def verify_shopify_webhook(data: bytes, hmac_header: str) -> bool:
    computed = base64.b64encode(hmac.new(SHOPIFY_WEBHOOK_SECRET, data, hashlib.sha256).digest())
    return hmac.compare_digest(computed.decode('utf-8'), hmac_header)

def get_or_create_client(billing, email):
    # Try to find existing client by email
    resp = requests.get(CLIENTS_ENDPOINT, headers=HEADERS, params={'search': email})
    resp.raise_for_status()
    clients = resp.json().get('clients', [])
    if clients:
        return clients[0]['id']
    # Create new client as private person
    client_payload = {
        'client': {
            'client_kind': 'private_person',
            'vat_payer': False,
            'name': f"{billing.get('first_name','')} {billing.get('last_name','')}".strip(),
            'email': email,
            'street': billing.get('address1'),
            'city': billing.get('city'),
            'zip_code': billing.get('zip'),
            'country_code': billing.get('country_code'),
        }
    }
    rc = requests.post(CLIENTS_ENDPOINT, json=client_payload, headers=HEADERS)
    if not rc.ok:
        app.logger.error(f"[inFakt CLIENT CREATE ERROR] status={rc.status_code}, body={rc.text}")
        rc.raise_for_status()
    return rc.json().get('client', {}).get('id')

@app.route('/', methods=['GET'])
def healthcheck():
    return 'OK', 200

@app.route('/webhook/orders/create', methods=['POST'])
def orders_create():
    raw_body = request.get_data()
    shopify_hmac = request.headers.get('X-Shopify-Hmac-Sha256', '')
    if not verify_shopify_webhook(raw_body, shopify_hmac):
        abort(401, 'Invalid HMAC signature')

    order = request.get_json()
    billing = order.get('billing_address') or order.get('customer', {}).get('default_address', {})
    email = order.get('email')
    client_id = get_or_create_client(billing, email)

    positions = [{
        'name': line['title'],
        'quantity': line['quantity'],
        'unit_gross_price': float(line['price']),
        'vat_rate': 23
    } for line in order.get('line_items', [])]

    created_at = datetime.strptime(order['created_at'], '%Y-%m-%dT%H:%M:%S%z')
    sell_date = created_at.date().isoformat()
    issue_date = sell_date
    payment_due = (created_at + timedelta(days=7)).date().isoformat()

    payload = {'invoice': {
        'client_id': client_id,
        'status': 'draft',
        'sell_date': sell_date,
        'issue_date': issue_date,
        'payment_due_date': payment_due,
        'payment_method': 'transfer',
        'positions': positions
    }
    }

    resp = requests.post(CREATE_ENDPOINT, json=payload, headers=HEADERS)
    if not resp.ok:
        app.logger.error(f"[inFakt CREATE ERROR] status={resp.status_code}, body={resp.text}")
        resp.raise_for_status()
    data = resp.json()
    ref = data.get('invoice_task_reference_number')

    for _ in range(12):
        st = requests.get(STATUS_ENDPOINT_TMPL.format(ref=ref), headers=HEADERS)
        st.raise_for_status()
        info = st.json()
        code = info.get('processing_code')
        if code == 201:
            app.logger.info(f"Invoice created successfully: {info}")
            break
        elif code == 422:
            app.logger.error(f"[inFakt PROCESSING ERROR] {info}")
            break
        time.sleep(5)
    return '', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))