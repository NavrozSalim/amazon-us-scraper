# Seller Pilot — Amazon US Scraper

Production Amazon.com scraper for Seller Pilot Hub (price, stock, title).

Production scraper ported from Seller Pilot Hub (`saas-store-sync`).

## Output

```json
{
  "success": true,
  "price": 29.99,
  "stock": 10,
  "title": "Product title",
  "error_code": "",
  "error_message": "",
  "vendor": "amazonus",
  "region": "USA",
  "url": "https://..."
}
```

## Local run

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
apify run -p
```

Set a real product URL in `storage/key_value_stores/default/INPUT.json`.

## AliExpress secrets (AliExpress actors only)

- `ALIEXPRESS_APP_KEY`
- `ALIEXPRESS_APP_SECRET`
- `ALIEXPRESS_ACCESS_TOKEN` (for Drop Shipping API)

## Deploy / import

Push this repo to GitHub, then in Apify Console: **Import from Git repository**.
