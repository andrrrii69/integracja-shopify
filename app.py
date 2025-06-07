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
    for shipping_line in order.get('shipping_lines', []):
        amount = float(shipping_line.get('price', 0))
        if amount > 0:
            gross = int(round(amount * 100))
            net = int(round(gross / 1.23))
            tax = gross - net
            services.append({
                'name': f"Wysyłka - {shipping_line.get('title', 'dostawa')}",
                'tax_symbol': '23',
                'quantity': 1,
                'unit_net_price': net,
                'unit_cost': net,
                'gross_price': gross,
                'tax_price': tax,
                'flat_rate_tax_symbol': '3'
            })
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

@app.route('/webhook/orders/create', methods=['POST'])
def orders_create():
    raw = request.get_data()
    signature = request.headers.get('X-Shopify-Hmac-Sha256', '')
    if not verify_shopify_webhook(raw, signature):
        abort(401, 'Invalid HMAC signature')

    order = request.get_json()
    billing = order.get('billing_address') or order.get('customer', {}).get('default_address', {})

    nip = billing.get('nip') or billing.get('company_nip')
    activity_kind = 'other_business' if nip else 'private_person'

    client_fields = {
        'client_first_name': billing.get('first_name', ''),
        'client_last_name': billing.get('last_name', ''),
        'client_company_name': billing.get('company', ''),
        'client_street': billing.get('address1', ''),
        'client_flat_number': billing.get('address2', ''),
        'client_city': billing.get('city', ''),
        'client_post_code': billing.get('zip', ''),
        'client_business_activity_kind': activity_kind,
    }
    if nip:
        client_fields['client_tax_code'] = nip

    created = datetime.strptime(order['created_at'], '%Y-%m-%dT%H:%M:%S%z')
    sell_date = created.date().isoformat()
    issue_date = sell_date
    due_date = (created + timedelta(days=7)).date().isoformat()

    payload = {
        'invoice': {
            'kind': 'vat',
            'series': os.getenv('INFAKT_SERIES', 'A'),
            'status': 'issued',
            'sell_date': sell_date,
            'issue_date': issue_date,
            'payment_due_date': due_date,
            'payment_method': 'transfer',
            'currency': 'PLN',
            'external_id': str(order['id']),
            **client_fields,
            'services': prepare_services(order)
        }
    }

    resp = requests.post(VAT_ENDPOINT, json=payload, headers=HEADERS)
    if not resp.ok:
        app.logger.error(f"[inFakt VAT ERROR] status={resp.status_code}, body={resp.text}")
        return '', 500

    invoice_uuid = resp.json().get('uuid')
    if not invoice_uuid:
        app.logger.error("Brak invoice_uuid w odpowiedzi!")
        return '', 500

    requests.post(f'https://{HOST}/api/v3/async/invoices/{invoice_uuid}/paid.json', headers=HEADERS)

    return '', 200


@app.route('/webhook/orders/edited', methods=['POST'])
@app.route('/webhook/orders/cancelled', methods=['POST'])
def orders_update_or_cancel():
    raw = request.get_data()
    signature = request.headers.get('X-Shopify-Hmac-Sha256', '')
    if not verify_shopify_webhook(raw, signature):
        abort(401, 'Invalid HMAC signature')

    order = request.get_json()
    order_id = str(order.get('id'))

    # Szukamy faktury po external_id
    search_url = f'https://{HOST}/api/v3/invoices.json?external_id={order_id}'
    search_resp = requests.get(search_url, headers=HEADERS)
    if not search_resp.ok:
        app.logger.error(f"[inFakt SEARCH ERROR] status={search_resp.status_code}, body={search_resp.text}")
        return '', 200

    invoices = search_resp.json()
    if not invoices:
        app.logger.warning(f"Brak faktury do zamówienia {order_id} — pomijam korektę")
        return '', 200

    invoice_uuid = invoices[0]['uuid']
    reason = 'Edycja zamówienia' if request.path.endswith('edited') else 'Anulowanie zamówienia'

    correction_payload = {
        'correction': {
            'reason': reason,
            'services': prepare_services(order)
        }
    }

    correction_url = f'https://{HOST}/api/v3/invoices/{invoice_uuid}/correction.json'
    correction_resp = requests.post(correction_url, json=correction_payload, headers=HEADERS)
    if not correction_resp.ok:
        app.logger.error(f"[inFakt CORRECTION ERROR] status={correction_resp.status_code}, body={correction_resp.text}")
        return '', 500

    return '', 200

