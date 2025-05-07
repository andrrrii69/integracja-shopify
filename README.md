# Shopify - inFakt Integration (Buyer Only)

This version uses direct `buyer` payload per inFakt docs, bypassing client resource.

## Setup
- Copy `.env.example` to `.env`
- Fill env vars
- pip install -r requirements.txt
- Production: gunicorn app:app --bind 0.0.0.0:$PORT
