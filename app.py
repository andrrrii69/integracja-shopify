import os
import hmac
import hashlib
import base64
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, abort, request

app = Flask(__name__)

SHOPIFY_WEBHOOK_SECRET = os.getenv('SHOPIFY_WEBHOOK_SECRET', '').encode('utf-8')
INFAKT_API_KEY = os.getenv('INFAKT_API_KEY', '')
HOST = os.getenv('INFAKT_HOST', 'api.infakt.pl')
VAT_ENDPOINT = f'https://{HOST}/api/v3/invoices.json'
DEFAULT_VAT_RATE = Decimal(os.getenv('DEFAULT_VAT_RATE', '0.23'))
FLAT_RATE_TAX_SYMBOL = os.getenv('INFAKT_FLAT_RATE_TAX_SYMBOL', '3')
INFAKT_SERIES = os.getenv('INFAKT_SERIES', 'A')
INFAKT_PAYMENT_METHOD = os.getenv('INFAKT_PAYMENT_METHOD', 'transfer')
INFAKT_SALE_TYPE = os.getenv('INFAKT_SALE_TYPE', '').strip().lower()
INFAKT_TIMEOUT = int(os.getenv('INFAKT_TIMEOUT', '30'))
SHOPIFY_TAX_CODE_KEYS = [
    key.strip().lower()
    for key in os.getenv('SHOPIFY_TAX_CODE_KEYS', 'nip,tax_id,taxid,vat_id,vatid,vat_number')
    .split(',')
    if key.strip()
]

HEADERS = {
    'X-inFakt-ApiKey': INFAKT_API_KEY,
    'Content-Type': 'application/json',
    'Accept': 'application/json',
}

ZERO = Decimal('0')
ONE = Decimal('1')
HUNDRED = Decimal('100')
PENNY = Decimal('0.01')
DISCOUNT_PRECISION = Decimal('0.01')


class ServiceEntry(Dict[str, Any]):
    pass


def D(value: Any, default: str = '0') -> Decimal:
    if value in (None, '', False):
        return Decimal(default)
    return Decimal(str(value))


def round_money(value: Decimal) -> Decimal:
    return value.quantize(PENNY, rounding=ROUND_HALF_UP)


def to_cents(value: Decimal) -> int:
    return int((value * HUNDRED).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def cents_to_decimal(value: int) -> Decimal:
    return Decimal(value) / HUNDRED


def verify_shopify_webhook(data: bytes, hmac_header: str) -> bool:
    if not SHOPIFY_WEBHOOK_SECRET:
        app.logger.error('Brak SHOPIFY_WEBHOOK_SECRET')
        return False
    computed = base64.b64encode(
        hmac.new(SHOPIFY_WEBHOOK_SECRET, data, hashlib.sha256).digest()
    ).decode('utf-8')
    return hmac.compare_digest(computed, hmac_header or '')


def get_tax_rate(payload: Dict[str, Any], fallback: Optional[Decimal] = None) -> Decimal:
    tax_lines = payload.get('tax_lines') or []
    total_rate = sum((D(line.get('rate')) for line in tax_lines), ZERO)
    if total_rate > ZERO:
        return total_rate
    if payload.get('taxable') is False:
        return ZERO
    return fallback if fallback is not None else DEFAULT_VAT_RATE


def tax_symbol_from_rate(rate: Decimal) -> str:
    if rate <= ZERO:
        return 'zw'
    return str(int((rate * HUNDRED).quantize(Decimal('1'), rounding=ROUND_HALF_UP)))


def amount_from_shop_money(maybe_set: Any) -> Optional[Decimal]:
    if not isinstance(maybe_set, dict):
        return None
    shop_money = maybe_set.get('shop_money') or {}
    amount = shop_money.get('amount')
    if amount in (None, ''):
        return None
    return D(amount)


def sum_discount_allocations(payload: Dict[str, Any], fallback_field: Optional[str] = 'total_discount') -> Decimal:
    allocations = payload.get('discount_allocations') or []
    total = ZERO
    for allocation in allocations:
        if allocation.get('amount') not in (None, ''):
            total += D(allocation.get('amount'))
            continue
        amount = amount_from_shop_money(allocation.get('amount_set'))
        if amount is not None:
            total += amount
    if total > ZERO:
        return total
    if fallback_field and payload.get(fallback_field) not in (None, ''):
        return D(payload.get(fallback_field))
    return ZERO


def amount_to_net_cents(amount: Decimal, rate: Decimal, taxes_included: bool) -> int:
    if taxes_included and rate > ZERO:
        return to_cents(amount / (ONE + rate))
    return to_cents(amount)


def gross_from_unit_net_cents(unit_net_price_cents: int, quantity: int, rate: Decimal) -> Decimal:
    return round_money(cents_to_decimal(unit_net_price_cents * quantity) * (ONE + rate))


def resolve_line_discount(line_item: Dict[str, Any]) -> Decimal:
    return round_money(sum_discount_allocations(line_item, fallback_field='total_discount'))


def resolve_shipping_gross_before(shipping_line: Dict[str, Any]) -> Decimal:
    for key in ('price', 'original_price'):
        if shipping_line.get(key) not in (None, ''):
            return D(shipping_line.get(key))
    amount = amount_from_shop_money(shipping_line.get('price_set'))
    if amount is not None:
        return amount
    return ZERO


def resolve_shipping_gross_after(shipping_line: Dict[str, Any]) -> Decimal:
    for key in ('discounted_price', 'current_price'):
        if shipping_line.get(key) not in (None, ''):
            return D(shipping_line.get(key))

    discounted_set = amount_from_shop_money(shipping_line.get('discounted_price_set'))
    if discounted_set is not None:
        return discounted_set

    gross_before = resolve_shipping_gross_before(shipping_line)
    discount_total = sum_discount_allocations(shipping_line, fallback_field=None)
    return max(ZERO, gross_before - discount_total)


def percent_discount(before_amount: Decimal, after_amount: Decimal) -> Decimal:
    if before_amount <= ZERO or after_amount >= before_amount:
        return ZERO
    discount_pct = ((before_amount - after_amount) / before_amount) * HUNDRED
    return discount_pct.quantize(DISCOUNT_PRECISION, rounding=ROUND_HALF_UP)


def update_discount_fields(service: Dict[str, Any]) -> None:
    before_cents = service.get('unit_net_price_before_discount')
    after_cents = service.get('unit_net_price')

    if before_cents is None or after_cents is None:
        service.pop('discount', None)
        service.pop('unit_net_price_before_discount', None)
        return

    before = cents_to_decimal(int(before_cents))
    after = cents_to_decimal(int(after_cents))
    if before <= ZERO or after >= before:
        service.pop('discount', None)
        service.pop('unit_net_price_before_discount', None)
        return

    service['discount'] = float(percent_discount(before, after))


def build_product_service(line_item: Dict[str, Any], taxes_included: bool) -> ServiceEntry:
    quantity = int(line_item.get('quantity', 0) or 0)
    if quantity <= 0:
        raise ValueError('Pozycja ma quantity <= 0')

    rate = get_tax_rate(line_item)
    tax_symbol = tax_symbol_from_rate(rate)

    unit_price_before = D(line_item.get('price'))
    line_gross_before = round_money(unit_price_before * quantity)
    line_discount = resolve_line_discount(line_item)
    line_gross_after = max(ZERO, round_money(line_gross_before - line_discount))

    unit_net_before_cents = amount_to_net_cents(unit_price_before, rate, taxes_included)
    unit_gross_after = line_gross_after / quantity
    unit_net_after_cents = amount_to_net_cents(unit_gross_after, rate, taxes_included)

    service: Dict[str, Any] = {
        'name': line_item.get('title', 'Produkt'),
        'tax_symbol': tax_symbol,
        'quantity': quantity,
        'unit_net_price': unit_net_after_cents,
        'flat_rate_tax_symbol': FLAT_RATE_TAX_SYMBOL,
    }

    if unit_net_after_cents < unit_net_before_cents:
        service['unit_net_price_before_discount'] = unit_net_before_cents
        update_discount_fields(service)

    return ServiceEntry(
        payload=service,
        rate=rate,
        quantity=quantity,
        gross_value=gross_from_unit_net_cents(unit_net_after_cents, quantity, rate),
        entry_kind='product',
        can_rebalance=(line_gross_after > ZERO),
    )


def build_shipping_service(shipping_line: Dict[str, Any], taxes_included: bool) -> Optional[ServiceEntry]:
    gross_before = resolve_shipping_gross_before(shipping_line)
    gross_after = resolve_shipping_gross_after(shipping_line)
    if gross_after <= ZERO:
        return None

    rate = get_tax_rate(shipping_line)
    tax_symbol = tax_symbol_from_rate(rate)
    unit_net_after_cents = amount_to_net_cents(gross_after, rate, taxes_included)

    service: Dict[str, Any] = {
        'name': f"Wysyłka - {shipping_line.get('title', 'Dostawa')}",
        'tax_symbol': tax_symbol,
        'quantity': 1,
        'unit_net_price': unit_net_after_cents,
        'flat_rate_tax_symbol': FLAT_RATE_TAX_SYMBOL,
    }

    unit_net_before_cents = amount_to_net_cents(gross_before, rate, taxes_included)
    if gross_before > gross_after and unit_net_after_cents < unit_net_before_cents:
        service['unit_net_price_before_discount'] = unit_net_before_cents
        update_discount_fields(service)

    return ServiceEntry(
        payload=service,
        rate=rate,
        quantity=1,
        gross_value=gross_from_unit_net_cents(unit_net_after_cents, 1, rate),
        entry_kind='shipping',
        can_rebalance=(gross_after > ZERO),
    )


def total_entries_gross(entries: List[ServiceEntry]) -> Decimal:
    return round_money(sum((entry['gross_value'] for entry in entries), ZERO))


def candidate_sort_key(entry: ServiceEntry) -> Tuple[int, int, Decimal]:
    service = entry['payload']
    quantity = int(service.get('quantity', 0) or 0)
    is_product = entry.get('entry_kind') == 'product'
    return (
        0 if is_product else 1,
        0 if quantity == 1 else 1,
        -entry['gross_value'],
    )


def find_best_unit_net_delta(entry: ServiceEntry, diff: Decimal, max_search_cents: Optional[int] = None) -> int:
    service = entry['payload']
    quantity = int(service.get('quantity', 0) or 0)
    current_unit_net = int(service.get('unit_net_price', 0) or 0)
    current_gross = entry['gross_value']
    rate = entry['rate']

    if max_search_cents is None:
        # Przy większych różnicach (np. rabat koszykowy bez alokacji na line_items)
        # szukamy szerzej, żeby nie wpadać od razu w sztuczną pozycję wyrównującą.
        max_search_cents = max(20, int((abs(diff) * HUNDRED).to_integral_value(rounding=ROUND_HALF_UP)) + 10)

    best_delta = 0
    best_remaining = abs(diff)

    for delta in range(-max_search_cents, max_search_cents + 1):
        if delta == 0:
            continue
        new_unit_net = current_unit_net + delta
        if new_unit_net < 0:
            continue

        new_gross = gross_from_unit_net_cents(new_unit_net, quantity, rate)
        gross_delta = round_money(new_gross - current_gross)
        remaining = abs(round_money(diff - gross_delta))

        if remaining < best_remaining:
            best_remaining = remaining
            best_delta = delta
            if remaining == ZERO:
                break

    return best_delta


def apply_unit_net_delta(entry: ServiceEntry, delta: int) -> Decimal:
    if delta == 0:
        return ZERO

    service = entry['payload']
    quantity = int(service.get('quantity', 0) or 0)
    current_unit_net = int(service.get('unit_net_price', 0) or 0)
    current_gross = entry['gross_value']

    if delta < 0 and 'unit_net_price_before_discount' not in service:
        service['unit_net_price_before_discount'] = current_unit_net

    new_unit_net = current_unit_net + delta
    service['unit_net_price'] = new_unit_net
    update_discount_fields(service)

    new_gross = gross_from_unit_net_cents(new_unit_net, quantity, entry['rate'])
    gross_delta = round_money(new_gross - current_gross)
    entry['gross_value'] = new_gross
    return gross_delta


def estimate_delta_for_target(entry: ServiceEntry, target_gross_change: Decimal) -> int:
    service = entry['payload']
    quantity = int(service.get('quantity', 0) or 0)
    if quantity <= 0:
        return 0

    rate = entry['rate']
    unit_step_gross = (ONE + rate) * Decimal(quantity)
    approx_delta = int(((target_gross_change * HUNDRED) / unit_step_gross).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    search_span = max(20, abs(approx_delta) + 8)
    return find_best_unit_net_delta(entry, target_gross_change, max_search_cents=search_span)


def distribute_order_level_diff(entries: List[ServiceEntry], diff: Decimal) -> Decimal:
    """
    Rozrzuca większą ujemną różnicę proporcjonalnie po produktach.
    To pomaga przy rabatach koszykowych Shopify, które czasem nie trafiają
    do line_item.discount_allocations w webhooku.
    """
    if diff >= ZERO:
        return diff

    candidates = [
        entry for entry in sorted(entries, key=candidate_sort_key)
        if entry.get('can_rebalance') and entry.get('entry_kind') == 'product' and entry['gross_value'] > ZERO
    ]
    if not candidates:
        return diff

    total_gross = round_money(sum((entry['gross_value'] for entry in candidates), ZERO))
    if total_gross <= ZERO:
        return diff

    remaining = diff

    for index, entry in enumerate(candidates):
        if remaining == ZERO:
            break

        if index == len(candidates) - 1:
            target_for_entry = remaining
        else:
            proportional = round_money(abs(diff) * (entry['gross_value'] / total_gross))
            target_for_entry = -min(proportional, abs(remaining))

        delta = estimate_delta_for_target(entry, target_for_entry)
        if delta == 0:
            continue

        gross_delta = apply_unit_net_delta(entry, delta)
        if gross_delta == ZERO:
            continue

        remaining = round_money(remaining - gross_delta)

    return remaining


def rebalance_services(entries: List[ServiceEntry], target_gross: Decimal) -> None:
    diff = round_money(target_gross - total_entries_gross(entries))
    if diff == ZERO:
        return

    if abs(diff) >= Decimal('0.05'):
        diff = distribute_order_level_diff(entries, diff)

    candidates = [
        entry for entry in sorted(entries, key=candidate_sort_key)
        if entry.get('can_rebalance')
    ]

    for entry in candidates:
        if diff == ZERO:
            break
        delta = find_best_unit_net_delta(entry, diff)
        if delta == 0:
            continue
        gross_delta = apply_unit_net_delta(entry, delta)
        if gross_delta == ZERO:
            continue
        diff = round_money(diff - gross_delta)

    if diff == ZERO:
        return

    rate = DEFAULT_VAT_RATE
    rounding_service = {
        'name': 'Wyrównanie zaokrągleń',
        'tax_symbol': tax_symbol_from_rate(rate),
        'quantity': 1,
        'unit_net_price': amount_to_net_cents(abs(diff), rate, taxes_included=True),
        'flat_rate_tax_symbol': FLAT_RATE_TAX_SYMBOL,
    }

    if rounding_service['unit_net_price'] == 0:
        return

    if diff < ZERO:
        rounding_service['unit_net_price'] = -rounding_service['unit_net_price']

    entries.append(ServiceEntry(
        payload=rounding_service,
        rate=rate,
        quantity=1,
        gross_value=gross_from_unit_net_cents(rounding_service['unit_net_price'], 1, rate),
        entry_kind='rounding',
        can_rebalance=False,
    ))


def prepare_services(order: Dict[str, Any]) -> List[Dict[str, Any]]:
    taxes_included = bool(order.get('taxes_included', True))
    entries: List[ServiceEntry] = []

    for line_item in order.get('line_items', []):
        entries.append(build_product_service(line_item, taxes_included))

    for shipping_line in order.get('shipping_lines', []):
        result = build_shipping_service(shipping_line, taxes_included)
        if result is not None:
            entries.append(result)

    target_total_gross = round_money(D(order.get('current_total_price') or order.get('total_price', '0')))
    rebalance_services(entries, target_total_gross)
    return [entry['payload'] for entry in entries]


def normalize_tax_code(raw_value: Optional[str]) -> Optional[str]:
    if not raw_value:
        return None
    digits = ''.join(filter(str.isdigit, raw_value))
    if len(digits) < 10:
        return None
    return digits


def extract_tax_code(order: Dict[str, Any], billing: Dict[str, Any]) -> Optional[str]:
    candidates: List[Any] = [
        billing.get('tax_code'),
        billing.get('vat_id'),
        billing.get('vat_number'),
        billing.get('company'),
        order.get('tax_code'),
        order.get('vat_id'),
        order.get('vat_number'),
    ]

    for attribute in order.get('note_attributes', []) or []:
        name = (attribute.get('name') or '').strip().lower()
        if name in SHOPIFY_TAX_CODE_KEYS:
            candidates.append(attribute.get('value'))

    for candidate in candidates:
        tax_code = normalize_tax_code(candidate)
        if tax_code:
            return tax_code
    return None


def build_client(order: Dict[str, Any]) -> Dict[str, Any]:
    billing = order.get('billing_address') or order.get('customer', {}).get('default_address', {}) or {}
    tax_code = extract_tax_code(order, billing)

    first_name = billing.get('first_name', '')
    last_name = billing.get('last_name', '')
    company = billing.get('company', '')
    company_name = company or f"{first_name} {last_name}".strip() or 'Klient detaliczny'

    activity = 'other_business' if tax_code else 'private_person'

    client: Dict[str, Any] = {
        'client_first_name': first_name,
        'client_last_name': last_name,
        'client_company_name': company_name,
        'client_street': billing.get('address1', ''),
        'client_city': billing.get('city', ''),
        'client_post_code': billing.get('zip', ''),
        'client_country': billing.get('country_code') or billing.get('country') or 'PL',
        'client_business_activity_kind': activity,
    }

    if billing.get('address2'):
        client['client_street'] = f"{client['client_street']} {billing['address2']}".strip()
    if tax_code:
        client['client_tax_code'] = tax_code

    return client


def determine_invoice_type(order: Dict[str, Any]) -> str:
    """
    inFakt wymaga pola invoice.sale_type: service | merchandise.
    Dla typowego sklepu Shopify z fizycznymi produktami powinno to być
    'merchandise'. Zostawiamy możliwość nadpisania przez env.
    """
    if INFAKT_SALE_TYPE in {'service', 'merchandise'}:
        return INFAKT_SALE_TYPE

    line_items = order.get('line_items', []) or []
    if any(item.get('requires_shipping') for item in line_items):
        return 'merchandise'

    # Gdy Shopify nie poda requires_shipping, dla e-commerce bezpieczniej
    # założyć sprzedaż towarów niż usług.
    return 'merchandise'


def mark_invoice_paid(uuid: str) -> None:
    paid_endpoint = f'https://{HOST}/api/v3/async/invoices/{uuid}/paid.json'
    response = requests.post(paid_endpoint, headers=HEADERS, timeout=INFAKT_TIMEOUT)
    if not response.ok:
        app.logger.warning(
            'Nie udało się oznaczyć faktury jako opłaconej: %s %s',
            response.status_code,
            response.text,
        )


def create_invoice(order: Dict[str, Any]) -> Optional[str]:
    if not INFAKT_API_KEY:
        app.logger.error('Brak INFAKT_API_KEY')
        return None

    sell_date = str(order.get('created_at', '')).split('T')[0]
    payload = {
        'invoice': {
            'kind': 'vat',
            'sale_type': determine_invoice_type(order),
            'series': INFAKT_SERIES,
            'status': 'issued',
            'sell_date': sell_date,
            'issue_date': sell_date,
            'payment_due_date': sell_date,
            'payment_method': INFAKT_PAYMENT_METHOD,
            'currency': order.get('currency', 'PLN'),
            'external_id': str(order.get('id')),
            **build_client(order),
            'services': prepare_services(order),
        }
    }

    response = requests.post(
        VAT_ENDPOINT,
        json=payload,
        headers=HEADERS,
        timeout=INFAKT_TIMEOUT,
    )

    if not response.ok:
        app.logger.error('[inFakt ERROR] %s %s | payload=%s', response.status_code, response.text, payload)
        return None

    uuid = response.json().get('uuid')
    if uuid and order.get('financial_status') == 'paid':
        mark_invoice_paid(uuid)
    return uuid


@app.route('/', methods=['GET'])
def healthcheck() -> Tuple[str, int]:
    return 'OK', 200


@app.route('/webhook/orders/create', methods=['POST'])
def orders_create() -> Tuple[str, int]:
    raw = request.get_data()
    signature = request.headers.get('X-Shopify-Hmac-Sha256', '')
    if not verify_shopify_webhook(raw, signature):
        abort(401)

    order = request.get_json(silent=True) or {}
    create_invoice(order)
    return '', 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')))
