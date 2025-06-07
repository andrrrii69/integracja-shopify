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
    total_item_gross = 0

    for item in order.get('line_items', []):
        qty = item['quantity']
        gross_per_unit = round(float(item['price']) * 100)
        net_unit = round(gross_per_unit / 1.23)
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
        total_item_gross += gross_per_unit * qty

    for shipping_line in order.get('shipping_lines', []):
        shipping_price = float(shipping_line.get('price', 0))
        if shipping_price > 0:
            gross = round(shipping_price * 100)
            net = round(gross / 1.23)
            tax = gross - net
            services.append({
                'name': f"Wysyłka - {shipping_line.get('title', 'dostawa')}",
                'tax_symbol': '23',
                'quantity': 1,
                'unit_net_price': net,
                'unit_cost': net,
                'gross_price': gross,
                'tax_price': tax,
                'flat_rate_tax_symbol': '3',
            })
            total_item_gross += gross

    # Rabat z całego zamówienia
    discount_value = float(order.get('total_discounts', 0))
    if discount_value > 0:
        discount_gross = round(discount_value * 100)
        net = round(discount_gross / 1.23)
        tax = discount_gross - net
        services.append({
            'name': 'Rabat',
            'tax_symbol': '23',
            'quantity': 1,
            'unit_net_price': -net,
            'unit_cost': -net,
            'gross_price': -discount_gross,
            'tax_price': -tax,
            'flat_rate_tax_symbol': '3',
        })

    return services

@app.route('/webhook/orders/create', methods=['POST'])
@app.route('/webhook/orders/updated', methods=['POST'])
@app.route('/webhook/orders/cancelled', methods=['POST'])
def orders_handler():
    raw = request.get_data()
    signature = request.headers.get('X-Shopify-Hmac-Sha256', '')
    if not verify_shopify_webhook(raw, signature):
        abort(401, 'Invalid HMAC signature')

    order = request.get_json()
    event_type = request.path.split("/")[-1]
    order_id = str(order['id'])

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

    # Szukamy faktury po external_id
    search_url = f'https://{HOST}/api/v3/invoices.json?external_id={order_id}'
    search_resp = requests.get(search_url, headers=HEADERS)
    invoice_exists = search_resp.ok and search_resp.json()
    created = datetime.strptime(order['created_at'], '%Y-%m-%dT%H:%M:%S%z')
    sell_date = created.date().isoformat()
    issue_date = sell_date
    due_date = (created + timedelta(days=7)).date().isoformat()

    if event_type == 'create' and not invoice_exists:
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
                'external_id': order_id,
                **client_fields,
                'services': prepare_services(order)
            }
        }
        resp = requests.post(VAT_ENDPOINT, json=payload, headers=HEADERS)
        if not resp.ok:
            app.logger.error(f"[inFakt VAT ERROR] status={resp.status_code}, body={resp.text}")
            return '', 500
        invoice_uuid = resp.json().get('uuid')
        requests.post(f'https://{HOST}/api/v3/async/invoices/{invoice_uuid}/paid.json', headers=HEADERS)
        return '', 200

    elif invoice_exists and event_type in ['updated', 'cancelled']:
        invoice = search_resp.json()[0]
        invoice_uuid = invoice['uuid']
        correction_payload = {
            'correction': {
                'reason': 'Edycja zamówienia' if event_type == 'updated' else 'Anulowanie zamówienia',
                'services': prepare_services(order)
            }
        }
        correction_url = f'https://{HOST}/api/v3/invoices/{invoice_uuid}/correction.json'
        correction_resp = requests.post(correction_url, json=correction_payload, headers=HEADERS)
        if not correction_resp.ok:
            app.logger.error(f"[inFakt CORRECTION ERROR] status={correction_resp.status_code}, body={correction_resp.text}")
            return '', 500
        return '', 200

    return '', 200

@app.route('/', methods=['GET'])
def healthcheck():
    return 'OK', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))

