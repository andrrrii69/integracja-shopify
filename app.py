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

# --- FUNKCJE DLA FAKTURY ZWYKŁEJ ---

def prepare_services(order):
    services = []
    calculated_invoice_total_gross = 0
    
    # 1. Produkty
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

    # 2. Wysyłka
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

    # 3. Rabat
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

# --- FUNKCJE DLA KOREKT (NOWOŚĆ) ---

def find_invoice_by_order_id(order_id):
    """Szuka faktury w inFakt po External ID (ID z Shopify)"""
    url = f'https://{HOST}/api/v3/invoices.json?external_id={order_id}'
    r = requests.get(url, headers=HEADERS)
    if r.ok:
        data = r.json()
        if data.get('entities'):
            return data['entities'][0]['uuid']
    return None

def prepare_refund_services(refund):
    """Przygotowuje pozycje korekty na podstawie danych o zwrocie"""
    services = []
    
    # 1. Zwracane produkty
    for r_item in refund.get('refund_line_items', []):
        line_item = r_item.get('line_item', {})
        qty = r_item['quantity'] # Ilość zwracana
        
        # Pobieramy cenę z linii (Shopify zwraca tu subtotal zwrotu dla danej linii)
        # Używamy subtotal, aby uwzględnić ewentualne rabaty, które były na produkcie
        refund_line_gross = float(r_item.get('subtotal', 0))
        
        # Jeśli ilość > 0, wyliczamy cenę jednostkową brutto tego zwrotu
        unit_gross_refunded = refund_line_gross / qty if qty > 0 else 0
        
        # Stawka VAT z oryginalnego produktu
        rate = 0.23
        if line_item.get('tax_lines'):
            rate = float(line_item['tax_lines'][0].get('rate', 0.23))
        
        tax_symbol = str(int(rate * 100))
        
        # Wyliczamy netto tak samo jak przy fakturze: Brutto / (1+VAT)
        unit_net_grosze = int(round((unit_gross_refunded / (1 + rate)) * 100))
        
        services.append({
            'name': line_item.get('title', 'Zwrot towaru'),
            'tax_symbol': tax_symbol,
            'quantity': -qty, # Ujemna ilość dla korekty
            'unit_net_price': unit_net_grosze,
            'flat_rate_tax_symbol': '3'
        })

    # 2. Zwrot kosztów wysyłki (Order Adjustments)
    for adjustment in refund.get('order_adjustments', []):
        if adjustment.get('kind') == 'shipping_refund':
            amount_gross = float(adjustment.get('amount', 0))
            if amount_gross > 0:
                # Zakładamy VAT 23% dla wysyłki lub trzeba by pobrać z orderu, 
                # ale w webhooku zwrotu adjustment nie ma info o tax_lines wysyłki.
                # Bezpieczne założenie 23% (standard) lub ręczne 8% jeśli sprzedajesz leki.
                ship_rate = 0.23 
                ship_net_grosze = int(round((amount_gross / (1 + ship_rate)) * 100))
                
                services.append({
                    'name': 'Korekta kosztów wysyłki',
                    'tax_symbol': '23',
                    'quantity': -1,
                    'unit_net_price': ship_net_grosze,
                    'flat_rate_tax_symbol': '3'
                })
                
    return services

def create_correction(refund):
    order_id = refund.get('order_id')
    invoice_uuid = find_invoice_by_order_id(order_id)
    
    if not invoice_uuid:
        app.logger.warning(f"Nie znaleziono faktury dla zamówienia {order_id}, pomijam korektę.")
        return None

    correction_date = refund.get('created_at', datetime.now().isoformat()).split('T')[0]
    
    payload = {'invoice': {
        'kind': 'vat', # inFakt sam ogarnie że to korekta, jeśli podamy uuid korygowanej? 
                       # W v3 endpoint jest dedykowany do korekt.
        'recipients_invoice_date': correction_date, # Data otrzymania korekty
        'sell_date': correction_date,
        'issue_date': correction_date,
        'payment_due_date': correction_date,
        'services': prepare_refund_services(refund)
    }}
    
    # Endpoint dla korekty konkretnej faktury
    url = f'https://{HOST}/api/v3/invoices/{invoice_uuid}/correction.json'
    
    r = requests.post(url, json=payload, headers=HEADERS)
    if not r.ok:
        app.logger.error(f"[CORRECTION ERROR] {r.status_code} {r.text}")
        return None
        
    return r.json().get('uuid')

# --- HANDLERY ---

@app.route('/webhook/orders/create', methods=['POST'])
def orders_create():
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256','')
    if not verify_shopify_webhook(raw, sig): abort(401)
    order = request.get_json()
    create_invoice(order)
    return '', 200

# NOWY WEBHOOK DO ZWROTÓW
@app.route('/webhook/refunds/create', methods=['POST'])
def refunds_create():
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256','')
    if not verify_shopify_webhook(raw, sig): abort(401)
    refund = request.get_json()
    create_correction(refund)
    return '', 200

@app.route('/', methods=['GET'])
def healthcheck():
    return 'OK', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
