import os
import hmac
import hashlib
import base64
from datetime import datetime, timedelta

from flask import Flask, request, abort
import requests

app = Flask(__name__)

# --- KONFIGURACJA ---
SHOPIFY_WEBHOOK_SECRET = os.getenv('SHOPIFY_WEBHOOK_SECRET', '').encode('utf-8')
INFAKT_API_KEY = os.getenv('INFAKT_API_KEY')
HOST = os.getenv('INFAKT_HOST', 'api.infakt.pl')
VAT_ENDPOINT = f'https://{HOST}/api/v3/invoices.json'
CORRECTION_ENDPOINT = f'https://{HOST}/api/v3/corrections.json' # Nowy endpoint dla korekt
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

# --- NOWE FUNKCJE DLA KOREKT ---

def find_invoice_by_order_id(order_id):
    """Szuka faktury w inFakt po external_id (ID zamówienia Shopify)."""
    params = {'q[external_id_eq]': str(order_id)}
    r = requests.get(VAT_ENDPOINT, headers=HEADERS, params=params)
    if r.ok:
        data = r.json()
        if data.get('entities'):
            return data['entities'][0]
    return None

def prepare_services_for_correction(refund):
    """Przygotowuje pozycje do korekty. Kwoty w PLN (float) dla lepszej kompatybilności z API korekt."""
    services = []
    for r_item in refund.get('refund_line_items', []):
        item = r_item['line_item']
        qty = r_item['quantity']
        unit_gross = float(item['price'])
        
        rate = 0.23
        if item.get('tax_lines'):
            rate = float(item['tax_lines'][0].get('rate', 0.23))
        
        tax_symbol = str(int(rate * 100))
        # Dla korekt używamy formatu PLN (np. 81.30) zamiast groszy
        unit_net_price = round(unit_gross / (1 + rate), 2)

        services.append({
            'name': item['title'],
            'tax_symbol': tax_symbol,
            'quantity': qty,
            'unit_net_price': unit_net_price,
            'flat_rate_tax_symbol': '3'
        })
    return services

def create_correction(refund):
    """Główna logika wystawiania korekty."""
    order_id = refund.get('order_id')
    original_invoice = find_invoice_by_order_id(order_id)
    
    if not original_invoice:
        app.logger.error(f"[CORRECTION ERROR] Nie znaleziono faktury dla zamówienia: {order_id}")
        return None

    correction_services = prepare_services_for_correction(refund)
    if not correction_services:
        return None

    payload = {
        'correction': {
            'invoice_id': original_invoice['id'],
            'reason': 'Zwrot towaru - Shopify Refund',
            'services': correction_services
        }
    }
    
    r = requests.post(CORRECTION_ENDPOINT, json=payload, headers=HEADERS)
    if not r.ok:
        # Logujemy szczegóły, aby uniknąć błędów 500 bez info
        app.logger.error(f"[CORRECTION ERROR] {r.status_code} {r.text}")
        return None
        
    return r.json().get('uuid')

# --- ISTNIEJĄCA LOGIKA TWORZENIA FAKTUR (BEZ ZMIAN) ---

def prepare_services(order):
    """Oryginalna funkcja przygotowania pozycji faktury."""
    services = []
    calculated_invoice_total_gross = 0
    
    for item in order.get('line_items', []):
        qty = item['quantity']
        unit_gross = float(item['price'])
        rate = 0.23 
        if item.get('tax_lines'):
            rate = float(item['tax_lines'][0].get('rate', 0.23))
        
        tax_symbol = str(int(rate * 100))
        unit_net_grosze = int(round((unit_gross / (1 + rate)) * 100))
        
        line_net_total = unit_net_grosze * qty
        line_gross_in_infakt = int(round(line_net_total * (1 + rate))) / 100.0
        calculated_invoice_total_gross += line_gross_in_infakt

        services.append({
            'name': item['title'],
            'tax_symbol': tax_symbol,
            'quantity': qty,
            'unit_net_price': unit_net_grosze,
            'flat_rate_tax_symbol': '3'
        })

    for shipping in order.get('shipping_lines', []):
        ship_gross = float(shipping.get('price', 0))
        if ship_gross > 0:
            ship_rate = 0.23
            if shipping.get('tax_lines'):
                ship_rate = float(shipping['tax_lines'][0].get('rate', 0.23))
            ship_tax_symbol = str(int(ship_rate * 100))
            ship_net_grosze = int(round((ship_gross / (1 + ship_rate)) * 100))
            ship_gross_in_infakt = int(round(ship_net_grosze * (1 + ship_rate))) / 100.0
            calculated_invoice_total_gross += ship_gross_in_infakt
            
            services.append({
                'name': f"Wysyłka - {shipping.get('title')}",
                'tax_symbol': ship_tax_symbol,
                'quantity': 1,
                'unit_net_price': ship_net_grosze,
                'flat_rate_tax_symbol': '3'
            })

    total_paid_gross = float(order.get('total_price', 0))
    diff_gross = calculated_invoice_total_gross - total_paid_gross

    if diff_gross > 0.02:
        discount_rate = 0.23 
        discount_net_grosze = int(round((diff_gross / (1 + discount_rate)) * 100))
        services.append({
            'name': 'Rabat',
            'tax_symbol': '23',
            'quantity': 1,
            'unit_net_price': -discount_net_grosze,
            'flat_rate_tax_symbol': '3'
        })
    return services

def create_invoice(order):
    """Oryginalna funkcja wystawiania faktury."""
    billing = order.get('billing_address') or order.get('customer', {}).get('default_address', {})
    company = billing.get('company', '')
    nip = "".join(filter(str.isdigit, company)) if company else None
    if nip and len(nip) < 10: nip = None

    activity = 'other_business' if nip else 'private_person'
    client = {
        'client_first_name': billing.get('first_name', ''),
        'client_last_name': billing.get('last_name', ''),
        'client_company_name': company if company else f"{billing.get('first_name')} {billing.get('last_name')}",
        'client_street': billing.get('address1', ''),
        'client_city': billing.get('city', ''),
        'client_post_code': billing.get('zip', ''),
        'client_business_activity_kind': activity,
    }
    if nip:
        client['client_tax_code'] = nip

    sell_date = order['created_at'].split('T')[0]
    payload = {'invoice': {
        'kind': 'vat',
        'series': os.getenv('INFAKT_SERIES','A'),
        'status': 'issued',
        'sell_date': sell_date,
        'issue_date': sell_date,
        'payment_due_date': sell_date,
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
    if uuid and order.get('financial_status') in ['paid', 'partially_paid']:
        requests.post(f'https://{HOST}/api/v3/async/invoices/{uuid}/paid.json', headers=HEADERS)
    return uuid

# --- ROUTY ---

@app.route('/', methods=['GET'])
def healthcheck():
    return 'OK', 200

@app.route('/webhook/orders/create', methods=['POST'])
def orders_create():
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256','')
    if not verify_shopify_webhook(raw, sig): abort(401)
    order = request.get_json()
    create_invoice(order)
    return '', 200

@app.route('/webhook/refunds/create', methods=['POST'])
def refunds_create():
    """Nowy endpoint do obsługi zwrotów."""
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256','')
    if not verify_shopify_webhook(raw, sig): abort(401)
    
    refund = request.get_json()
    create_correction(refund)
    return '', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
