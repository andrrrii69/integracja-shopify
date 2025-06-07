import os
import hmac
import hashlib
import base64
from datetime import datetime, timedelta

from flask import Flask, request, abort
import requests

app = Flask(__name__)

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
    computed = base64.b64encode(
        hmac.new(SHOPIFY_WEBHOOK_SECRET, data, hashlib.sha256).digest()
    )
    return hmac.compare_digest(computed.decode('utf-8'), hmac_header)


def prepare_services(order):
    services = []
    # produkty
    for item in order.get('line_items', []):
        qty = item['quantity']
        gross = int(round(float(item['price']) * 100))
        net = int(round(gross / 1.23))
        tax = gross - net
        services.append({
            'name': item['title'],
            'tax_symbol': '23',
            'quantity': qty,
            'unit_net_price': net,
            'unit_cost': net,
            'gross_price': gross * qty,
            'tax_price': tax * qty,
            'flat_rate_tax_symbol': '3'
        })
    # wysyłka
    for shipping in order.get('shipping_lines', []):
        amount = float(shipping.get('price', 0))
        gross = int(round(amount * 100))
        net = int(round(gross / 1.23)) if gross > 0 else 0
        tax = gross - net
        services.append({
            'name': f"Wysyłka - {shipping.get('title', 'dostawa')}",
            'tax_symbol': '23',
            'quantity': 1,
            'unit_net_price': net,
            'unit_cost': net,
            'gross_price': gross,
            'tax_price': tax,
            'flat_rate_tax_symbol': '3'
        })
    # rabat
    discount_value = float(order.get('total_discounts', 0))
    if discount_value > 0:
        gross = int(round(discount_value * 100))
        net = int(round(gross / 1.23))
        tax = gross - net
        services.append({
            'name': 'Rabat',
            'tax_symbol': '23',
            'quantity': 1,
            'unit_net_price': -net,
            'unit_cost': -net,
            'gross_price': -gross,
            'tax_price': -tax,
            'flat_rate_tax_symbol': '3'
        })
    return services

@app.route('/', methods=['GET'])
def healthcheck():
    return 'OK', 200

# Tworzenie faktury przy nowym zamówieniu
def create_invoice(order):
    billing = order.get('billing_address') or order.get('customer', {}).get('default_address', {})
    nip = billing.get('nip') or billing.get('company_nip')
    activity = 'other_business' if nip else 'private_person'
    client = {
        'client_first_name': billing.get('first_name', ''),
        'client_last_name': billing.get('last_name', ''),
        'client_company_name': billing.get('company', ''),
        'client_street': billing.get('address1', ''),
        'client_flat_number': billing.get('address2', ''),
        'client_city': billing.get('city', ''),
        'client_post_code': billing.get('zip', ''),
        'client_business_activity_kind': activity,
    }
    if nip:
        client['client_tax_code'] = nip

    created = datetime.strptime(order['created_at'], '%Y-%m-%dT%H:%M:%S%z')
    sell = created.date().isoformat()
    due = (created + timedelta(days=7)).date().isoformat()

    payload = {'invoice': {
        'kind': 'vat','series': os.getenv('INFAKT_SERIES','A'),'status':'issued',
        'sell_date': sell,'issue_date': sell,'payment_due_date': due,
        'payment_method':'transfer','currency':'PLN','external_id': str(order['id']),
        **client,'services': prepare_services(order)
    }}
    r = requests.post(VAT_ENDPOINT, json=payload, headers=HEADERS)
    if not r.ok:
        app.logger.error(f"[VAT ERROR] {r.status_code} {r.text}")
        return None
    uuid = r.json().get('uuid')
    if uuid:
        requests.post(f'https://{HOST}/api/v3/async/invoices/{uuid}/paid.json', headers=HEADERS)
    return uuid

# Wystawienie korekty
def create_correction(order, reason):
    oid = str(order.get('id'))
    r = requests.get(f'https://{HOST}/api/v3/invoices.json?external_id={oid}', headers=HEADERS)
    if not r.ok or not r.json():
        app.logger.warning(f"Brak faktury {oid}")
        return False
    uuid = r.json()[0]['uuid']
    payload = {'correction': {'reason': reason, 'services': prepare_services(order)}}
    c = requests.post(f'https://{HOST}/api/v3/invoices/{uuid}/correction.json', json=payload, headers=HEADERS)
    if not c.ok:
        app.logger.error(f"[CORR ERROR] {c.status_code} {c.text}")
        return False
    return True

@app.route('/webhook/orders/create', methods=['POST'])
def orders_create():
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256','')
    if not verify_shopify_webhook(raw, sig): abort(401)
    order = request.get_json()
    if create_invoice(order): app.logger.info(f"Invoice {order['id']} created")
    return '',200

@app.route('/webhook/orders/updated', methods=['POST'])
def orders_updated():
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256','')
    if not verify_shopify_webhook(raw, sig): abort(401)
    order = request.get_json()
    if create_correction(order, 'Edycja zamówienia'):
        app.logger.info(f"Correction updated {order['id']}")
    return '',200

@app.route('/webhook/orders/cancelled', methods=['POST'])
def orders_cancelled():
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256','')
    if not verify_shopify_webhook(raw, sig): abort(401)
    order = request.get_json()
    if create_correction(order, 'Anulowanie zamówienia'):
        app.logger.info(f"Correction cancelled {order['id']}")
    return '',200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT',5000)))

