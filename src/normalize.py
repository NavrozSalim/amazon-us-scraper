"""Normalize legacy scraper dicts to DataHarvest / Apify output shape."""
from __future__ import annotations


def normalize_result(raw: dict | None, *, vendor: str, region: str, url: str) -> dict:
    raw = raw or {}
    price = raw.get("price")
    stock = raw.get("stock")
    title = raw.get("title")
    err = (raw.get("error_code") or "").strip()
    msg = (raw.get("error_message") or "").strip()
    success = bool(
        not err
        and (price is not None or stock is not None or (title and not msg))
    )
    # Treat explicit error fields as failure even if title present
    if err:
        success = False
    if price is None and stock is None and not title and not err:
        success = False
        err = err or "scrape_empty"
        msg = msg or "No price/stock/title returned"
    return {
        "success": success,
        "price": price,
        "stock": stock,
        "title": title,
        "error_code": err if not success else "",
        "error_message": msg if not success else "",
        "vendor": vendor,
        "region": region,
        "url": url,
    }
