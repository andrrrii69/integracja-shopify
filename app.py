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
    
    # 1. Produkty - Wyliczanie ceny netto z danych Shopify
    for item in order.get('line_items', []):
        qty = item['quantity']
        
        # Shopify podaje cenę jednostkową brutto
        gross_unit_grosze = int(round(float(item['price']) * 100))
        total_gross_grosze = gross_unit_grosze * qty
        
        # Pobieramy faktyczną kwotę podatku dla tej pozycji z Shopify
        item_tax_total_grosze = int(round(sum(float(tax.get('price', 0)) for tax in item.get('tax_lines', [])) * 100))
        
        # Obliczamy czyste netto: Brutto - Podatek
        total_net_grosze = total_gross_grosze - item_tax_total_grosze
        unit_net_price_grosze = int(round(total_net_grosze / qty))
        
        # Wyznaczanie symbolu podatku (np. "23")
        tax_symbol = "23"
        if item.get('tax_lines'):
            rate = float(item['tax_lines'][0].get('rate', 0.23))
            tax_symbol = str(int(rate * 100)) if rate > 0 else "zw"

        services.append({
            'name': item['title'],
            'tax_symbol': tax_symbol,
            'quantity': qty,
            'unit_net_price': unit_net_price_grosze, # Wysyłamy TYLKO netto
            'flat_rate_tax_symbol': '3'
        })

    # 2. Wysyłka - Wyliczanie netto
    for shipping in order.get('shipping_lines', []):
        amount_gross = float(shipping.get('price', 0))
        if amount_gross <= 0:
            continue
            
        gross_grosze = int(round(amount_gross * 100))
        tax_grosze = int(round(sum(float(tax.get('price', 0)) for tax in shipping.get('tax_lines', [])) * 100))
        net_grosze = gross_grosze - tax_grosze
        
        ship_tax_symbol = "23"
        if shipping.get('tax_lines'):
            rate = float(shipping['tax_lines'][0].get('rate', 0.23))
            ship_tax_symbol = str(int(rate * 100)) if rate > 0 else "zw"

        services.append({
            'name': f"Wysyłka - {shipping.get('title', 'dostawa')}",
            'tax_symbol': ship_tax_symbol,
            'quantity': 1,
            'unit_net_price': net_grosze, # Wysyłamy TYLKO netto
            'flat_rate_tax_symbol': '3'
        })

    # 3. Rabaty
    discount_value = float(order.get('total_discounts', 0))
    if discount_value > 0:
        gross_disc_grosze = int(round(discount_value * 100))
        # Dla rabatu musimy wyliczyć bazę netto (zakładamy 23% VAT dla rabatu ogólnego)
        net_disc_grosze = int(round(gross_disc_grosze / 1.23))
        
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
    nip = billing.get('company') if (billing.get('company') and any(char.isdigit() for char in billing.get('company'))) else None
    
    activity = 'other_business' if nip else 'private_person'
    client = {
        'client_first_name': billing.get('first_name', ''),
        'client_last_name': billing.get('last_name', ''),
        'client_company_name': billing.get('company', ''),
        'client_street': billing.get('address1', ''),
        'client_flat_number': billing.get('address2', '') or '',
        'client_city': billing.get('city', ''),
        'client_post_code': billing.get('zip', ''),
        'client_business_activity_kind': activity,
    }
    if nip:
        client['client_tax_code'] = nip

    created_str = order['created_at'].split('T')[0]
    sell = created_str 
    due = (datetime.strptime(sell, '%Y-%m-%d') + timedelta(days=7)).date().isoformat()

    payload = {'invoice': {
        'kind': 'vat',
        'series': os.getenv('INFAKT_SERIES','A'),
        'status': 'issued',
        'sell_date': sell,
        'issue_date': sell,
        'payment_due_date': due,
        'payment_method': 'transfer',
        'currency': order.get('currency', 'PLN'),
        'external_id': str(order['id']),
        **client,
        'services': prepare_services(order)
    }}
    
    r = requests.post(VAT_ENDPOINT, json=payload, headers=HEADERS)
    if not r.ok:
        return None
        
    uuid = r.json().get('uuid')
    if uuid and order.get('financial_status') in ['paid', 'voided']:
        requests.post(f'https://{HOST}/api/v3/async/invoices/{uuid}/paid.json', headers=HEADERS)
    return uuid

def create_correction(order, reason):
    oid = str(order.get('id'))
    r = requests.get(f'https://{HOST}/api/v3/invoices.json?external_id={oid}', headers=HEADERS)
    if not r.ok:
        return False
    
    data = r.json()
    invoices = data.get('invoices', []) if isinstance(data, dict) else data
    if not invoices:
        return False
        
    invoice_uuid = invoices[0].get('uuid')
    payload = {
        'correction': {
            'reason': reason, 
            'services': prepare_services(order)
        }
    }
    
    c = requests.post(f'https://{HOST}/api/v3/invoices/{invoice_uuid}/correction.json', json=payload, headers=HEADERS)
    return c.ok

@app.route('/webhook/orders/create', methods=['POST'])
def orders_create():
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256','')
    if not verify_shopify_webhook(raw, sig): abort(401)
    order = request.get_json()
    create_invoice(order)
    return '', 200

@app.route('/webhook/orders/updated', methods=['POST'])
def orders_updated():
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256','')
    if not verify_shopify_webhook(raw, sig): abort(401)
    order = request.get_json()
    create_correction(order, 'Edycja zamówienia')
    return '', 200

@app.route('/webhook/orders/cancelled', methods=['POST'])
def orders_cancelled():
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256','')
    if not verify_shopify_webhook(raw, sig): abort(401)
    order = request.get_json()
    create_correction(order, 'Anulowanie zamówienia')
    return '', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
