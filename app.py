import os
import hmac
import hashlib
import base64
import re
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from flask import Flask, request, abort
import requests

app = Flask(__name__)

# -----------------------------
# Config
# -----------------------------
SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "").encode("utf-8")

INFAKT_API_KEY = os.getenv("INFAKT_API_KEY")
INFAKT_HOST = (os.getenv("INFAKT_HOST", "api.infakt.pl") or "api.infakt.pl").strip()

# Use async by default for webhook flows
INFAKT_ASYNC = (os.getenv("INFAKT_ASYNC", "true").lower() in ("1", "true", "yes", "y"))

INFAKT_SERIES = os.getenv("INFAKT_SERIES", "A")
INFAKT_INVOICE_KIND = os.getenv("INFAKT_INVOICE_KIND", "vat")
INFAKT_DEFAULT_PAYMENT_METHOD = os.getenv("INFAKT_DEFAULT_PAYMENT_METHOD", "transfer")
INFAKT_PAYMENT_TERMS_DAYS = int(os.getenv("INFAKT_PAYMENT_TERMS_DAYS", "7"))

# Optional: if you're on ryczałt and need flat_rate_tax_symbol on each service
INFAKT_FLAT_RATE_TAX_SYMBOL = (os.getenv("INFAKT_FLAT_RATE_TAX_SYMBOL", "").strip() or None)

# Optional: create tiny adjustment line if rounding differs
INFAKT_ADJUSTMENT_TAX_SYMBOL = int(os.getenv("INFAKT_ADJUSTMENT_TAX_SYMBOL", "23"))
INFAKT_ADJUSTMENT_NAME = os.getenv("INFAKT_ADJUSTMENT_NAME", "Wyrównanie (Shopify)")

# Skip Shopify test orders by default
ALLOW_TEST_ORDERS = (os.getenv("ALLOW_TEST_ORDERS", "false").lower() in ("1", "true", "yes", "y"))

BASE_URL = f"https://{INFAKT_HOST}/api/v3"
INVOICE_ENDPOINT = f"{BASE_URL}/{'async/' if INFAKT_ASYNC else ''}invoices.json"

HEADERS = {
    "X-inFakt-ApiKey": INFAKT_API_KEY or "",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

SESSION = requests.Session()

_NIP_RE = re.compile(r"\b(\d{10})\b")


# -----------------------------
# Shopify webhook verification
# -----------------------------

def verify_shopify_webhook(data: bytes, hmac_header: str) -> bool:
    """Verify Shopify webhook signature (X-Shopify-Hmac-Sha256)."""
    if not SHOPIFY_WEBHOOK_SECRET:
        app.logger.error("SHOPIFY_WEBHOOK_SECRET is not set")
        return False

    computed = base64.b64encode(hmac.new(SHOPIFY_WEBHOOK_SECRET, data, hashlib.sha256).digest())
    return hmac.compare_digest(computed.decode("utf-8"), (hmac_header or ""))


# -----------------------------
# Money helpers
# -----------------------------

def d(value) -> Decimal:
    return Decimal(str(value))


def money_to_grosze(value) -> int:
    """Convert money value to integer grosze."""
    return int((d(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) * 100)


def pick_vat_rate(tax_lines, fallback: Decimal = Decimal("0.23")) -> Decimal:
    if tax_lines and isinstance(tax_lines, list):
        rate = tax_lines[0].get("rate")
        if rate is not None:
            try:
                return d(rate)
            except Exception:
                pass
    return fallback


def rate_to_tax_symbol(rate: Decimal) -> int:
    return int((rate * 100).to_integral_value(rounding=ROUND_HALF_UP))


# -----------------------------
# Mapping helpers
# -----------------------------

def extract_nip(order: dict, billing: dict) -> str | None:
    """Try to find NIP (10 digits) in note_attributes / note / billing.company."""
    candidates = []

    for attr in (order.get("note_attributes") or []):
        key = (attr.get("name") or attr.get("key") or "").strip().lower()
        if key in ("nip", "vat", "vat_id", "vatid", "nip_number", "numer_nip"):
            candidates.append(str(attr.get("value", "")))

    if order.get("note"):
        candidates.append(str(order["note"]))

    if billing.get("company"):
        candidates.append(str(billing["company"]))

    for text in candidates:
        m = _NIP_RE.search(text)
        if m:
            return m.group(1)

    return None


def infer_payment_method(order: dict) -> str:
    """Very simple mapping Shopify gateways -> inFakt payment_method."""
    gateways = order.get("payment_gateway_names") or []
    if isinstance(gateways, str):
        gateways = [gateways]

    hay = " ".join([str(x).lower() for x in gateways])

    if any(s in hay for s in ("cod", "cash", "pobran", "pobranie")):
        return "cash"

    if any(s in hay for s in ("stripe", "paypal", "card", "przelewy24", "p24", "payu", "tpay", "paypro", "dotpay", "visa", "mastercard")):
        return "card"

    # Shopify draft orders often show "manual" – treat as transfer unless you want otherwise
    if "manual" in hay:
        return "transfer"

    return INFAKT_DEFAULT_PAYMENT_METHOD


def should_skip_order(order: dict) -> tuple[bool, str]:
    if order.get("cancelled_at"):
        return True, "order_cancelled"

    financial_status = (order.get("financial_status") or "").lower()
    if financial_status in ("voided",):
        return True, "financial_voided"

    if order.get("test") and not ALLOW_TEST_ORDERS:
        return True, "test_order"

    return False, "ok"


# -----------------------------
# inFakt API helpers
# -----------------------------

def infakt_post(url: str, payload: dict, timeout: int = 20) -> requests.Response:
    if not INFAKT_API_KEY:
        raise RuntimeError("INFAKT_API_KEY is not set")
    return SESSION.post(url, json=payload, headers=HEADERS, timeout=timeout)


def mark_invoice_paid(uuid: str, paid_date: str) -> None:
    """Mark invoice as paid (async first, fallback to sync)."""
    body = {"paid_date": paid_date}

    async_url = f"{BASE_URL}/async/invoices/{uuid}/paid.json"
    try:
        r = infakt_post(async_url, body)
        if r.ok:
            return

        if r.status_code == 404:
            sync_url = f"{BASE_URL}/invoices/{uuid}/paid.json"
            r2 = infakt_post(sync_url, body)
            if not r2.ok:
                app.logger.error(f"[INFAKT PAID ERROR] {r2.status_code} {r2.text}")
            return

        app.logger.error(f"[INFAKT PAID ERROR] {r.status_code} {r.text}")
    except Exception as e:
        app.logger.error(f"[INFAKT PAID EXCEPTION] {e}")


# -----------------------------
# Core: build invoice services
# -----------------------------

def prepare_services(order: dict) -> list[dict]:
    taxes_included = bool(order.get("taxes_included", False))

    services: list[dict] = []
    calc_total_gross_grosze = 0

    # 1) Products
    for item in (order.get("line_items") or []):
        qty = int(item.get("quantity") or 0)
        if qty <= 0:
            continue

        unit_price_grosze = money_to_grosze(item.get("price", "0"))
        rate = pick_vat_rate(item.get("tax_lines"), fallback=Decimal("0.23"))
        tax_symbol = rate_to_tax_symbol(rate)

        if taxes_included:
            unit_net_grosze = int((d(unit_price_grosze) / (Decimal(1) + rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        else:
            unit_net_grosze = unit_price_grosze

        gross_line = int((d(unit_net_grosze) * qty * (Decimal(1) + rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        calc_total_gross_grosze += gross_line

        line = {
            "name": item.get("title") or item.get("name") or "Produkt",
            "tax_symbol": tax_symbol,
            "quantity": qty,
            "unit_net_price": unit_net_grosze,
        }
        if INFAKT_FLAT_RATE_TAX_SYMBOL:
            line["flat_rate_tax_symbol"] = INFAKT_FLAT_RATE_TAX_SYMBOL
        services.append(line)

    # 2) Shipping
    for shipping in (order.get("shipping_lines") or []):
        ship_price_grosze = money_to_grosze(shipping.get("price", "0"))
        if ship_price_grosze <= 0:
            continue

        ship_rate = pick_vat_rate(shipping.get("tax_lines"), fallback=Decimal("0.23"))
        ship_tax_symbol = rate_to_tax_symbol(ship_rate)

        if taxes_included:
            ship_net_grosze = int((d(ship_price_grosze) / (Decimal(1) + ship_rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        else:
            ship_net_grosze = ship_price_grosze

        ship_gross = int((d(ship_net_grosze) * (Decimal(1) + ship_rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        calc_total_gross_grosze += ship_gross

        line = {
            "name": f"Wysyłka - {shipping.get('title') or 'Dostawa'}",
            "tax_symbol": ship_tax_symbol,
            "quantity": 1,
            "unit_net_price": ship_net_grosze,
        }
        if INFAKT_FLAT_RATE_TAX_SYMBOL:
            line["flat_rate_tax_symbol"] = INFAKT_FLAT_RATE_TAX_SYMBOL
        services.append(line)

    # 3) Adjustment line if needed (rounding / discounts not allocated per-item)
    total_gross_grosze = money_to_grosze(order.get("total_price", "0"))
    diff_grosze = calc_total_gross_grosze - total_gross_grosze

    # Add only if at least 2 grosze difference (avoid noise)
    if abs(diff_grosze) >= 2:
        adj_rate = d(INFAKT_ADJUSTMENT_TAX_SYMBOL) / 100
        adj_net = int((d(abs(diff_grosze)) / (Decimal(1) + adj_rate)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

        # If calc > total -> need discount (negative net)
        if diff_grosze > 0:
            adj_net = -adj_net

        line = {
            "name": INFAKT_ADJUSTMENT_NAME,
            "tax_symbol": INFAKT_ADJUSTMENT_TAX_SYMBOL,
            "quantity": 1,
            "unit_net_price": adj_net,
        }
        if INFAKT_FLAT_RATE_TAX_SYMBOL:
            line["flat_rate_tax_symbol"] = INFAKT_FLAT_RATE_TAX_SYMBOL
        services.append(line)

    return services


# -----------------------------
# Core: create invoice
# -----------------------------

def create_invoice(order: dict) -> str | None:
    skip, reason = should_skip_order(order)
    if skip:
        app.logger.info(f"Skipping invoice: {reason} order_id={order.get('id')}")
        return None

    financial_status = (order.get("financial_status") or "").lower()
    if financial_status not in ("paid", "partially_paid"):
        # This route should normally receive paid orders, but guard anyway.
        app.logger.info(f"Skipping invoice: not paid (financial_status={financial_status}) order_id={order.get('id')}")
        return None

    billing = order.get("billing_address") or order.get("customer", {}).get("default_address") or {}

    nip = extract_nip(order, billing)
    activity = "other_business" if nip else "private_person"

    # company can be real name, but sometimes people put NIP there — if so, don't use it as company name
    company_raw = (billing.get("company") or "").strip()
    if _NIP_RE.fullmatch(company_raw or ""):
        company_raw = ""
    # If it's only digits but not a valid NIP, treat it as empty (common when a field is misused)
    if company_raw.isdigit() and len(company_raw) != 10:
        company_raw = ""

    client_company_name = company_raw or f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip()

    # Dates
    raw_date = order.get("processed_at") or order.get("created_at") or ""
    sell_date = raw_date.split("T")[0] if "T" in raw_date else datetime.utcnow().date().isoformat()

    payload = {
        "invoice": {
            "kind": INFAKT_INVOICE_KIND,
            "series": INFAKT_SERIES,
            "status": "issued",
            "sell_date": sell_date,
            "issue_date": sell_date,
            "payment_due_date": sell_date,
            "payment_method": infer_payment_method(order),
            "currency": order.get("currency", "PLN"),
            "external_id": str(order.get("id", "")),
            "client_first_name": billing.get("first_name", "") or "",
            "client_last_name": billing.get("last_name", "") or "",
            "client_company_name": client_company_name or "",
            "client_street": (f"{billing.get('address1','') or ''} {billing.get('address2','') or ''}").strip(),
            "client_city": billing.get("city", "") or "",
            "client_post_code": billing.get("zip", "") or "",
            "client_business_activity_kind": activity,
            "services": prepare_services(order),
        }
    }

    if nip:
        payload["invoice"]["client_tax_code"] = nip

    try:
        r = infakt_post(INVOICE_ENDPOINT, payload)
    except Exception as e:
        app.logger.error(f"[INFAKT EXCEPTION] {e}")
        return None

    if not r.ok:
        app.logger.error(f"[INFAKT INVOICE ERROR] {r.status_code} {r.text}")
        return None

    try:
        data = r.json() or {}
    except Exception:
        data = {}

    uuid = data.get("uuid") or (data.get("invoice") or {}).get("uuid") or data.get("id")

    if uuid:
        mark_invoice_paid(str(uuid), paid_date=sell_date)

    return str(uuid) if uuid else None


# -----------------------------
# Routes
# -----------------------------

@app.route("/", methods=["GET"])
def healthcheck():
    return "OK", 200


@app.route("/webhook/orders/create", methods=["POST"])
def orders_create():
    """We keep this endpoint only to acknowledge the event.

    IMPORTANT: do NOT invoice on orders/create — it fires for unpaid, test, or later-cancelled orders.
    """
    raw = request.get_data()
    sig = request.headers.get("X-Shopify-Hmac-Sha256", "")
    if not verify_shopify_webhook(raw, sig):
        abort(401)
    return "", 200


@app.route("/webhook/orders/paid", methods=["POST"])
def orders_paid():
    """Create invoice when the order is paid."""
    raw = request.get_data()
    sig = request.headers.get("X-Shopify-Hmac-Sha256", "")
    if not verify_shopify_webhook(raw, sig):
        abort(401)

    order = request.get_json(silent=True) or {}
    create_invoice(order)
    return "", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
