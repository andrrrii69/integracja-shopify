import os
import hmac
import hashlib
import base64
import requests
from flask import Flask, request, abort

app = Flask(__name__)

# Konfiguracja
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
    if not hmac_header:
        return False
    computed = base64.b64encode(
        hmac.new(SHOPIFY_WEBHOOK_SECRET, data, hashlib.sha256).digest()
    )
    return hmac.compare_digest(computed.decode('utf-8'), hmac_header)

def prepare_services(order):
    services = []
    calculated_total_gross_grosze = 0
    
    for item in order.get('line_items', []):
        qty = int(item['quantity'])
        unit_gross_float = float(item['price'])
        
        rate = 0.23
        if item.get('tax_lines'):
            rate = float(item['tax_lines'][0].get('rate', 0.23))
        
        tax_symbol = str(int(rate * 100))
        unit_net_grosze = int(round((unit_gross_float / (1 + rate)) * 100))
        
        line_gross_grosze = int(round(unit_net_grosze * qty * (1 + rate)))
        calculated_total_gross_grosze += line_gross_grosze

        services.append({
            'name': item['title'],
            'tax_symbol': tax_symbol,
            'quantity': qty,
            'unit_net_price': unit_net_grosze,
            'flat_rate_tax_symbol': '3'
        })

    for shipping in order.get('shipping_lines', []):
        ship_gross_float = float(shipping.get('price', 0))
        if ship_gross_float > 0:
            ship_rate = 0.23
            if shipping.get('tax_lines'):
                ship_rate = float(shipping['tax_lines'][0].get('rate', 0.23))
            
            ship_tax_symbol = str(int(ship_rate * 100))
            ship_net_grosze = int(round((ship_gross_float / (1 + ship_rate)) * 100))
            calculated_total_gross_grosze += int(round(ship_net_grosze * 1 * (1 + ship_rate)))
            
            services.append({
                'name': f"Wysyłka: {shipping.get('title')}",
                'tax_symbol': ship_tax_symbol,
                'quantity': 1,
                'unit_net_price': ship_net_grosze,
                'flat_rate_tax_symbol': '3'
            })

    target_total_gross_grosze = int(round(float(order.get('total_price', 0)) * 100))
    diff_grosze = calculated_total_gross_grosze - target_total_gross_grosze

    if diff_grosze != 0:
        services.append({
            'name': 'Korekta zaokrągleń',
            'tax_symbol': '23',
            'quantity': 1,
            'unit_net_price': int(round(-diff_grosze / 1.23)),
            'flat_rate_tax_symbol': '3'
        })
        
    return services

def create_invoice(order):
    billing = order.get('billing_address') or {}
    
    # NAPRAWA BŁĘDU: Dodanie or '' zapewnia, że company_field to zawsze string, a nie None
    company_field = billing.get('company') or ''
    nip = "".join(filter(str.isdigit, company_field))
    
    is_business = len(nip) == 10
    activity = 'other_business' if is_business else 'private_person'
    
    # Jeśli company_field jest puste, używamy imienia i nazwiska
    client_name = company_field if company_field.strip() else f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip()

    payload = {
        'invoice': {
            'kind': 'vat',
            'series': os.getenv('INFAKT_SERIES', 'A'),
            'status': 'issued',
            'sell_date': order['created_at'].split('T')[0],
            'issue_date': order['created_at'].split('T')[0],
            'payment_due_date': order['created_at'].split('T')[0],
            'payment_method': 'transfer',
            'currency': order.get('currency', 'PLN'),
            'external_id': str(order['id']),
            'recipient_first_name': billing.get('first_name'),
            'recipient_last_name': billing.get('last_name'),
            'client_company_name': client_name,
            'client_street': f"{billing.get('address1', '')} {billing.get('address2', '')}".strip(),
            'client_city': billing.get('city'),
            'client_post_code': billing.get('zip'),
            'client_country': billing.get('country_code', 'PL'),
            'client_business_activity_kind': activity,
            'services': prepare_services(order)
        }
    }
    
    if is_business:
        payload['invoice']['client_tax_code'] = nip

    r = requests.post(VAT_ENDPOINT, json=payload, headers=HEADERS)
    if not r.ok:
        app.logger.error(f"[INFAKT ERROR] {r.status_code}: {r.text}")
        return None
        
    invoice_uuid = r.json().get('uuid')
    
    if invoice_uuid and order.get('financial_status') == 'paid':
        requests.post(f'https://{HOST}/api/v3/invoices/{invoice_uuid}/paid.json', headers=HEADERS)
        
    return invoice_uuid

@app.route('/webhook/orders/create', methods=['POST'])
def orders_create():
    raw_data = request.get_data()
    hmac_header = request.headers.get('X-Shopify-Hmac-Sha256', '')
    
    if not verify_shopify_webhook(raw_data, hmac_header):
        abort(401)
        
    order = request.get_json()
    uuid = create_invoice(order)
    
    return {'status': 'success', 'infakt_uuid': uuid}, 200

@app.route('/', methods=['GET'])
def health():
    return 'OK', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 10000)))
