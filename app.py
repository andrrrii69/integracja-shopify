import os
import hmac
import hashlib
import base64
from datetime import datetime, timedelta

from flask import Flask, request, abort
import requests

app = Flask(__name__)

# Konfiguracja środowiskowa
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
    
    # 1. Produkty - Obsługa cen po rabacie i prezentów (0 zł)
    for item in order.get('line_items', []):
        qty = item['quantity']
        if qty <= 0: continue

        # Pobieramy faktyczne Brutto i Podatek naliczone przez Shopify dla CAŁEJ linii (po rabatach)
        # Pole 'price' to cena przed rabatem, dlatego odejmujemy sumę z 'discount_allocations'
        total_discount = sum(float(d.get('amount', 0)) for d in item.get('discount_allocations', []))
        gross_at_start = float(item.get('price', 0)) * qty
        line_gross_after_discount = max(0, gross_at_start - total_discount)
        
        # Podatek dla linii (już po uwzględnieniu rabatów przez Shopify)
        line_tax_total = sum(float(tax.get('price', 0)) for tax in item.get('tax_lines', []))
        
        # Wyliczamy Netto dla całej linii
        total_net_grosze = int(round((line_gross_after_discount - line_tax_total) * 100))
        
        # Cena jednostkowa netto (zabezpieczenie przed 0 zł dla prezentów)
        unit_net_price_grosze = int(total_net_grosze // qty) if total_net_grosze > 0 else 0
        
        # Stawka VAT (jeśli 0 zł lub brak podatku -> 'zw', w innym wypadku z Shopify)
        tax_symbol = "zw"
        if line_tax_total > 0 and item.get('tax_lines'):
            rate = float(item['tax_lines'][0].get('rate', 0.23))
            tax_symbol = str(int(rate * 100))
        elif line_gross_after_discount > 0:
            tax_symbol = "23" # Domyślna dla produktów płatnych bez zdefiniowanej stawki

        services.append({
            'name': item['title'],
            'tax_symbol': tax_symbol,
            'quantity': qty,
            'unit_net_price': unit_net_price_grosze,
            'flat_rate_tax_symbol': '3'
        })

    # 2. Wysyłka
    for shipping in order.get('shipping_lines', []):
        # Pobieramy cenę po ewentualnym rabacie na wysyłkę
        gross_ship = float(shipping.get('discounted_price', shipping.get('price', 0)))
        if gross_ship < 0: gross_ship = 0
            
        tax_ship = sum(float(tax.get('price', 0)) for tax in shipping.get('tax_lines', []))
        net_ship_grosze = int(round((gross_ship - tax_ship) * 100))
        
        ship_tax_symbol = "zw" if gross_ship == 0 else "23"
        if shipping.get('tax_lines') and gross_ship > 0:
            rate = float(shipping['tax_lines'][0].get('rate', 0.23))
            ship_tax_symbol = str(int(rate * 100))

        services.append({
            'name': f"Wysyłka - {shipping.get('title', 'dostawa')}",
            'tax_symbol': ship_tax_symbol,
            'quantity': 1,
            'unit_net_price': net_ship_grosze,
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
        'client_city': billing.get('city', ''),
        'client_post_code': billing.get('zip', ''),
        'client_business_activity_kind': activity,
    }
    if nip:
        client['client_tax_code'] = nip

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
        'external_id': str(order['id']),
        **client,
        'services': prepare_services(order)
    }}
    
    r = requests.post(VAT_ENDPOINT, json=payload, headers=HEADERS)
    if not r.ok:
        app.logger.error(f"[VAT ERROR] {r.status_code} {r.text}")
        return None
        
    uuid = r.json().get('uuid')
    if uuid and order.get('financial_status') == 'paid':
        requests.post(f'https://{HOST}/api/v3/async/invoices/{uuid}/paid.json', headers=HEADERS)
    return uuid

@app.route('/webhook/orders/create', methods=['POST'])
def orders_create():
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256','')
    if not verify_shopify_webhook(raw, sig): abort(401)
    order = request.get_json()
    if create_invoice(order): 
        app.logger.info(f"Invoice for order {order['id']} created")
    return '', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
