import os
import hmac
import hashlib
import base64
import json
from datetime import datetime
from flask import Flask, request, abort
import requests

app = Flask(__name__)

# --- KONFIGURACJA ---
SHOPIFY_WEBHOOK_SECRET = os.getenv('SHOPIFY_WEBHOOK_SECRET', '').encode('utf-8')
INFAKT_API_KEY = os.getenv('INFAKT_API_KEY')
HOST = os.getenv('INFAKT_HOST', 'api.infakt.pl')
VAT_ENDPOINT = f'https://{HOST}/api/v3/invoices.json'
# NOWY ENDPOINT ZGODNIE Z DOKUMENTACJĄ
ASYNC_CORRECTION_ENDPOINT = f'https://{HOST}/api/v3/async/corrective_invoices.json'

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

# --- OBSŁUGA KOREKT (ZWROTÓW) ---

def find_invoice_by_order_id(order_id):
    """Szuka faktury w inFakt po external_id."""
    params = {'q[external_id_eq]': str(order_id)}
    r = requests.get(VAT_ENDPOINT, headers=HEADERS, params=params)
    if r.ok:
        data = r.json()
        if data.get('entities'):
            return data['entities'][0]
    return None

def prepare_services_for_async_correction(refund, original_invoice):
    """
    Przygotowuje pozycje zgodnie z dokumentacją asynchroniczną.
    Wymaga podania stanu PRZED (correction: false) i PO (correction: true).
    """
    services = []
    # Pobieramy pozycje z faktury pierwotnej, aby wiedzieć jak wyglądały wcześniej
    original_services = original_invoice.get('services', [])
    
    # Mapujemy refund_line_items po tytule, aby dopasować je do pozycji na fakturze
    refunded_items = {item['line_item']['title']: item for item in refund.get('refund_line_items', [])}

    group_id = 1
    for orig_srv in original_services:
        title = orig_srv['name']
        
        # 1. Dodajemy pozycję "PRZED" (kopia z oryginału)
        services.append({
            "name": title,
            "unit_net_price": orig_srv['unit_net_price'],
            "tax_symbol": orig_srv['tax_symbol'],
            "quantity": orig_srv['quantity'],
            "correction": False,
            "group": str(group_id),
            "flat_rate_tax_symbol": "3"
        })

        # 2. Obliczamy nową ilość "PO"
        new_qty = float(orig_srv['quantity'])
        if title in refunded_items:
            refund_qty = float(refunded_items[title]['quantity'])
            new_qty = max(0, new_qty - refund_qty)

        # 3. Dodajemy pozycję "PO"
        services.append({
            "name": title if new_qty > 0 else f"{title} - zwrot",
            "unit_net_price": orig_srv['unit_net_price'],
            "tax_symbol": orig_srv['tax_symbol'],
            "quantity": str(new_qty),
            "correction": True,
            "group": str(group_id),
            "flat_rate_tax_symbol": "3"
        })
        group_id += 1

    return services

def create_correction(refund):
    """Tworzy asynchroniczną korektę na podstawie dokumentacji."""
    order_id = refund.get('order_id')
    original_invoice = find_invoice_by_order_id(order_id)
    
    if not original_invoice:
        app.logger.error(f"[CORRECTION ERROR] Nie znaleziono faktury dla zamówienia: {order_id}")
        return None

    # Przygotowanie danych zgodnie z modelem asynchronicznym
    payload = {
        "corrective_invoice": {
            "payment_method": original_invoice.get("payment_method", "transfer"),
            "client_id": original_invoice.get("client_id"),
            "corrected_invoice_number": original_invoice.get("number"),
            "correction_reason": "Zwrot towaru (Shopify Refund)",
            "status": "printed", # Automatycznie zatwierdza korektę
            "services": prepare_services_for_async_correction(refund, original_invoice)
        }
    }
    
    app.logger.info(f"[DEBUG] Wysyłam asynchroniczną korektę dla faktury: {original_invoice.get('number')}")
    
    r = requests.post(ASYNC_CORRECTION_ENDPOINT, json=payload, headers=HEADERS)
    
    if not r.ok:
        app.logger.error(f"!!! BŁĄD ASYNC KOREKTY !!! Status: {r.status_code}")
        app.logger.error(f"Response: {r.text}")
        return None
        
    task_info = r.json()
    app.logger.info(f"[CORRECTION TASK] Przyjęto: {task_info.get('invoice_task_reference_number')}")
    return task_info.get('invoice_task_reference_number')

# --- ORYGINALNA LOGIKA FAKTUR (BEZ ZMIAN) ---

def prepare_services(order):
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
            'name': item['title'], 'tax_symbol': tax_symbol, 'quantity': qty,
            'unit_net_price': unit_net_grosze, 'flat_rate_tax_symbol': '3'
        })
    for shipping in order.get('shipping_lines', []):
        ship_gross = float(shipping.get('price', 0))
        if ship_gross > 0:
            ship_rate = 0.23
            if shipping.get('tax_lines'): ship_rate = float(shipping['tax_lines'][0].get('rate', 0.23))
            ship_tax_symbol = str(int(ship_rate * 100))
            ship_net_grosze = int(round((ship_gross / (1 + ship_rate)) * 100))
            ship_gross_in_infakt = int(round(ship_net_grosze * (1 + ship_rate))) / 100.0
            calculated_invoice_total_gross += ship_gross_in_infakt
            services.append({
                'name': f"Wysyłka - {shipping.get('title')}", 'tax_symbol': ship_tax_symbol,
                'quantity': 1, 'unit_net_price': ship_net_grosze, 'flat_rate_tax_symbol': '3'
            })
    total_paid_gross = float(order.get('total_price', 0))
    diff_gross = calculated_invoice_total_gross - total_paid_gross
    if diff_gross > 0.02:
        discount_rate = 0.23 
        discount_net_grosze = int(round((diff_gross / (1 + discount_rate)) * 100))
        services.append({
            'name': 'Rabat', 'tax_symbol': '23', 'quantity': 1,
            'unit_net_price': -discount_net_grosze, 'flat_rate_tax_symbol': '3'
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
    if nip: client['client_tax_code'] = nip
    sell_date = order['created_at'].split('T')[0]
    payload = {'invoice': {
        'kind': 'vat', 'series': os.getenv('INFAKT_SERIES','A'), 'status': 'issued',
        'sell_date': sell_date, 'issue_date': sell_date, 'payment_due_date': sell_date,
        'payment_method': 'transfer', 'currency': order.get('currency', 'PLN'),
        'external_id': str(order['id']), **client, 'services': prepare_services(order)
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
    raw, sig = request.get_data(), request.headers.get('X-Shopify-Hmac-Sha256','')
    if not verify_shopify_webhook(raw, sig): abort(401)
    refund = request.get_json()
    create_correction(refund)
    return '', 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
