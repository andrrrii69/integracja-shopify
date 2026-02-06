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


def _to_grosze(amount) -> int:
    return int(round(float(amount or 0) * 100))


def _pick_tax_symbol(tax_lines) -> str:
    """Map Shopify tax_lines[].rate (e.g. 0.23) to inFakt tax_symbol strings."""
    if not tax_lines:
        return '0'

    rate = tax_lines[0].get('rate')
    if rate is None:
        return '23'

    r = round(float(rate), 4)
    if abs(r - 0.23) < 0.0001:
        return '23'
    if abs(r - 0.08) < 0.0001:
        return '8'
    if abs(r - 0.05) < 0.0001:
        return '5'
    if abs(r - 0.00) < 0.0001:
        return '0'
    return '23'


def prepare_services(order):
    services = []

    # LINE ITEMS
    for item in order.get('line_items', []):
        qty = int(item.get('quantity', 0) or 0)
        if qty <= 0:
            continue

        unit_price_gross = _to_grosze(item.get('price'))

        # Shopify discount_allocations are total discount amounts for this line item
        discount_total_gross = 0
        for da in (item.get('discount_allocations') or []):
            discount_total_gross += _to_grosze(da.get('amount'))

        gross_total = unit_price_gross * qty - discount_total_gross

        tax_total = 0
        tax_lines = item.get('tax_lines', []) or []
        for tl in tax_lines:
            tax_total += _to_grosze(tl.get('price'))

        net_total = gross_total - tax_total

        # unit net (rounded); totals go into gross_price/tax_price anyway
        unit_net = int(round(net_total / qty)) if qty else net_total

        services.append({
            'name': item.get('title', 'Produkt'),
            'tax_symbol': _pick_tax_symbol(tax_lines),
            'quantity': qty,
            'unit_net_price': unit_net,
            'unit_cost': unit_net,
            'gross_price': gross_total,
            'tax_price': tax_total,
            'flat_rate_tax_symbol': '3'
        })

    # SHIPPING
    for shipping in (order.get('shipping_lines') or []):
        # prefer discounted_price if present, else price
        price_field = shipping.get('discounted_price', shipping.get('price', 0))
        gross_total = _to_grosze(price_field)

        # skip free shipping
        if gross_total <= 0:
            continue

        tax_total = 0
        tax_lines = shipping.get('tax_lines', []) or []
        for tl in tax_lines:
            tax_total += _to_grosze(tl.get('price'))

        net_total = gross_total - tax_total

        services.append({
            'name': f"Wysyłka - {shipping.get('title', 'dostawa')}",
            'tax_symbol': _pick_tax_symbol(tax_lines),
            'quantity': 1,
            'unit_net_price': net_total,
            'unit_cost': net_total,
            'gross_price': gross_total,
            'tax_price': tax_total,
            'flat_rate_tax_symbol': '3'
        })

    # NOTE: no separate "Rabat" line — discounts are already applied via discount_allocations
    return services


@app.route('/', methods=['GET'])
def healthcheck():
    return 'OK', 200


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
        'kind': 'vat',
        'series': os.getenv('INFAKT_SERIES', 'A'),
        'status': 'issued',
        'sell_date': sell,
        'issue_date': sell,
        'payment_due_date': due,
        'payment_method': 'transfer',
        'currency': 'PLN',
        'external_id': str(order['id']),
        **client,
        'services': prepare_services(order)
    }}

    r = requests.post(VAT_ENDPOINT, json=payload, headers=HEADERS)
    if not r.ok:
        app.logger.error(f"[VAT ERROR] {r.status_code} {r.text}")
        return None

    uuid = r.json().get('uuid')
    if uuid:
        requests.post(f'https://{HOST}/api/v3/async/invoices/{uuid}/paid.json', headers=HEADERS)
    return uuid


def create_correction(order, reason):
    oid = str(order.get('id'))
    r = requests.get(f'https://{HOST}/api/v3/invoices.json?external_id={oid}', headers=HEADERS)
    if not r.ok:
        app.logger.error(f"[SEARCH ERROR] status={r.status_code}, body={r.text}")
        return False

    data = r.json()
    invoices = data if isinstance(data, list) else data.get('invoices', [])
    if not invoices:
        app.logger.warning(f"Brak faktury dla zamówienia {oid}")
        return False

    invoice_uuid = invoices[0].get('uuid')
    if not invoice_uuid:
        app.logger.error(f"Brak UUID faktury w odpowiedzi dla zamówienia {oid}")
        return False

    payload = {'correction': {'reason': reason, 'services': prepare_services(order)}}
    c = requests.post(
        f'https://{HOST}/api/v3/invoices/{invoice_uuid}/correction.json',
        json=payload,
        headers=HEADERS
    )
    if not c.ok:
        app.logger.error(f"[CORR ERROR] status={c.status_code}, body={c.text}")
        return False

    return True


@app.route('/webhook/orders/create', methods=['POST'])
def orders_create():
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256', '')
    if not verify_shopify_webhook(raw, sig):
        abort(401)
    order = request.get_json()
    if create_invoice(order):
        app.logger.info(f"Invoice {order['id']} created")
    return '', 200


@app.route('/webhook/orders/updated', methods=['POST'])
def orders_updated():
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256', '')
    if not verify_shopify_webhook(raw, sig):
        abort(401)
    order = request.get_json()
    if create_correction(order, 'Edycja zamówienia'):
        app.logger.info(f"Correction updated {order['id']}")
    return '', 200


@app.route('/webhook/orders/cancelled', methods=['POST'])
def orders_cancelled():
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256', '')
    if not verify_shopify_webhook(raw, sig):
        abort(401)
    order = request.get_json()
    if create_correction(order, 'Anulowanie zamówienia'):
        app.logger.info(f"Correction cancelled {order['id']}")
    return '', 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
