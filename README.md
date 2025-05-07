# Shopify - inFakt Integration

This project listens to Shopify `orders/create` webhooks and automatically creates invoices in inFakt via their async API.

## Setup

1. Clone the repo and navigate to the directory.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Define environment variables (see `.env.example`):
   - `SHOPIFY_WEBHOOK_SECRET` – Shopify webhook secret (used to verify incoming webhooks).
   - `INFAKT_API_KEY` – API key from inFakt.
   - `INFAKT_HOST` – (optional) inFakt API host, default `api.infakt.pl`.
4. Run the app:
   ```bash
   flask run --host=0.0.0.0 --port=5000
   ```
5. Configure Shopify webhook:
   - URL: `https://your-domain.com/webhook/orders/create`
   - Event: **Order creation**
   - Format: `application/json`
   - Shared secret: same as `SHOPIFY_WEBHOOK_SECRET`

## Environment Variables

- `SHOPIFY_WEBHOOK_SECRET`
- `INFAKT_API_KEY`
- `INFAKT_HOST` (optional, defaults to `api.infakt.pl`)
