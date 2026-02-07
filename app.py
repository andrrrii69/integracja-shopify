import os
import hmac
import hashlib
import base64
from datetime import datetime, timedelta

from flask import Flask, request, abort
import requests

app = Flask(__name__)

# Konfiguracja z Twojego pliku
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
    
    # 1. Produkty - pobieranie netto i doliczanie VAT
    for item in order.get('line_items', []):
        qty = item['quantity']
        
        # Shopify podaje cenę brutto w 'price'. Obliczamy netto:
        gross_total_item = float(item['price']) * qty
        tax_total_item = sum(float(tax.get('price', 0)) for tax in item.get('tax_lines', []))
        
        # Obliczamy netto w groszach: (Brutto - Podatek)
        net_total_grosze = int(round((gross_total_item - tax_total_item) * 100))
        unit_net_grosze = int(round(net_total_grosze / qty))
        
        # Wyznaczanie stawki VAT (domyślnie 23%)
        tax_symbol = "23"
        if item.get('tax_lines'):
            rate = float(item['tax_lines'][0].get('rate', 0.23))
            tax_symbol = str(int(rate * 100)) if rate > 0 else "zw"
        elif not item.get('taxable'):
            tax_symbol = "zw"

        services.append({
            'name': item['title'],
            'tax_symbol': tax_symbol,
            'quantity': qty,
            'unit_net_price': unit_net_grosze,
            'flat_rate_tax_symbol': '3'
        })

    # 2. Wysyłka
    for shipping in order.get('shipping_lines', []):
        gross_ship = float(shipping.get('price', 0))
        if gross_ship <= 0: continue
            
        tax_ship = sum(float(tax.get('price', 0)) for tax in shipping.get('tax_lines', []))
        net_ship_grosze = int(round((gross_ship - tax_ship) * 100))
        
        ship_tax_symbol = "23"
        if shipping.get('tax_lines'):
            rate = float(shipping['tax_lines'][0].get('rate', 0.23))
            ship_tax_symbol = str(int(rate * 100)) if rate > 0 else "zw"

        services.append({
            'name': f"Wysyłka - {shipping.get('title', 'dostawa')}",
            'tax_symbol': ship_tax_symbol,
            'quantity': 1,
            'unit_net_price': net_ship_grosze,
            'flat_rate_tax_symbol': '3'
        })

    # 3. Rabaty (jako ujemna pozycja netto)
    discount_gross = float(order.get('total_discounts', 0))
    if discount_gross > 0:
        # Zakładamy rabat od kwoty netto przy stawce 23%
        net_disc_grosze = int(round((discount_gross / 1.23) * 100))
        services.append({
            'name': 'Rabat',
            'tax_symbol': '23',
            'quantity': 1,
            'unit_net_price': -net_disc_grosze,
            'flat_rate_tax_symbol': '3'
        })
        
    return services

@app.route('/', methods=['GET'])
def healthcheck():
    return 'OK', 200

def create_invoice(order):
    billing = order.get('billing_address') or order.get('customer', {}).get('default_address', {})
    # Próba wyciągnięcia NIP z pola firmy (częsta praktyka w Shopify PL)
    company_field = billing.get('company', '')
    nip = "".join(filter(str.isdigit, company_field)) if company_field else None
    if nip and len(nip) < 9: nip = None # prosta walidacja

    activity = 'other_business' if nip else 'private_person'
    client = {
        'client_first_name': billing.get('first_name', ''),
        'client_last_name': billing.get('last_name', ''),
        'client_company_name': billing.get('company', ''),
        'client_street': billing.get('address1', ''),
        'client_city': billing.get('city', ''),
        'client_post_code': billing.get('zip', ''),
        'client_business_activity_kind': activity,
    }
    if nip: client['client_tax_code'] = nip

    sell_date = order['created_at'].split('T')[0]
    due_date = (datetime.strptime(sell_date, '%Y-%m-%d') + timedelta(days=7)).date().isoformat()

    payload = {'invoice': {
        'kind': 'vat',
        'series': os.getenv('INFAKT_SERIES','A'),
        'status': 'issued',
        'sell_date': sell_date,
        'issue_date': sell_date,
        'payment_due_date': due_date,
        'payment_method': 'transfer',
        'currency': order.get('currency', 'PLN'),
        **client,
        'services': prepare_services(order)
    }}
    
    r = requests.post(VAT_ENDPOINT, json=payload, headers=HEADERS)
    if not r.ok:
        app.logger.error(f"[VAT ERROR] {r.text}")
        return None
        
    uuid = r.json().get('uuid')
    if uuid and order.get('financial_status') == 'paid':
        requests.post(f'https://{HOST}/api/v3/async/invoices/{uuid}/paid.json', headers=HEADERS)
    return uuid

def create_correction(order, reason):
    oid = str(order.get('id'))
    r = requests.get(f'https://{HOST}/api/v3/invoices.json?external_id={oid}', headers=HEADERS)
    if not r.ok: return False
    
    data = r.json()
    invoices = data.get('invoices', []) if isinstance(data, dict) else data
    if not invoices: return False
        
    invoice_uuid = invoices[0].get('uuid')
    payload = {'correction': {'reason': reason, 'services': prepare_services(order)}}
    c = requests.post(f'https://{HOST}/api/v3/invoices/{invoice_uuid}/correction.json', json=payload, headers=HEADERS)
    return c.ok

@app.route('/webhook/orders/create', methods=['POST'])
def orders_create():
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256','')
    if not verify_shopify_webhook(raw, sig): abort(401)
    if create_invoice(request.get_json()): return 'OK', 200
    return 'Error', 500

@app.route('/webhook/orders/updated', methods=['POST'])
def orders_updated():
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256','')
    if not verify_shopify_webhook(raw, sig): abort(401)
    create_correction(request.get_json(), 'Edycja zamówienia')
    return 'OK', 200

@app.route('/webhook/orders/cancelled', methods=['POST'])
def orders_cancelled():
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256','')
    if not verify_shopify_webhook(raw, sig): abort(401)
    create_correction(request.get_json(), 'Anulowanie zamówienia')
    return 'OK', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))