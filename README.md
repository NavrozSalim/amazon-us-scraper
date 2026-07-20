# DataHarvest — Amazon US Scraper

Scrapes Amazon.com product pages for price, stock, and title. Built for DataHarvest.

## Features

- Price, stock, and title extraction
- Structured dataset output for the Apify Console
- Optional Apify Proxy / custom proxies (where supported)

## Input

Provide a product `url` (and/or `urls` for bulk, where supported).

## Output

Each dataset item includes:

| Field | Description |
|-------|-------------|
| `success` | Scrape succeeded |
| `price` | Product price |
| `stock` | Stock estimate |
| `title` | Product title |
| `error_code` / `error_message` | Present when failed |
| `vendor` / `region` / `url` | Context |

## Local run

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
apify run -p
```

## Deploy

```bash
apify push
```

Or import this GitHub repo in the Apify Console, then complete **Publication** (display info, monetization, output schema is shipped in-repo).
