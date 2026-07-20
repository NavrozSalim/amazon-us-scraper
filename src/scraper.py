"""Amazon product page scraper (amazon.com)."""
from __future__ import annotations

import re
from typing import Any

from .helpers import fail, fetch_html, ok, parse_money, soup


def scrape_product(
    *,
    url: str,
    region: str,
    vendor: str,
    proxy_urls: list[str],
    timeout_secs: int,
    max_retries: int,
    actor_input: dict[str, Any],
) -> dict:
    last_err = "unknown"
    for attempt in range(max(1, max_retries + 1)):
        try:
            html, status = fetch_html(url, proxy_urls=proxy_urls, timeout_secs=timeout_secs)
            if status >= 400:
                last_err = f"HTTP {status}"
                continue
            low = html.lower()
            if "captcha" in low or "robot check" in low or "api-services-support@amazon" in low:
                last_err = "blocked_or_captcha"
                continue
            doc = soup(html)
            title_el = doc.select_one("#productTitle") or doc.select_one("span#title") or doc.select_one("h1")
            title = title_el.get_text(strip=True) if title_el else None

            price = None
            for sel in (
                "span.a-price span.a-offscreen",
                "#priceblock_ourprice",
                "#priceblock_dealprice",
                "#corePrice_feature_div span.a-offscreen",
                "#corePriceDisplay_desktop_feature_div span.a-offscreen",
            ):
                el = doc.select_one(sel)
                if el:
                    price = parse_money(el.get_text())
                    if price is not None:
                        break

            if price is None:
                m = re.search(r'"priceAmount"\s*:\s*([0-9.]+)', html)
                if m:
                    price = float(m.group(1))

            stock = None
            avail = doc.select_one("#availability") or doc.select_one("#availability span")
            avail_text = (avail.get_text(" ", strip=True) if avail else "").lower()
            if "in stock" in avail_text or ("only" in avail_text and "left" in avail_text):
                stock = 10
                m = re.search(r"only\s+(\d+)\s+left", avail_text)
                if m:
                    stock = int(m.group(1))
            elif "unavailable" in avail_text or "out of stock" in avail_text:
                stock = 0

            if price is None and stock is None and not title:
                last_err = "parse_failed"
                continue

            return ok(
                price,
                stock,
                title,
                vendor=vendor,
                region=region,
                url=url,
                host="amazon.com",
                attempt=attempt + 1,
            )
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
    return fail("amazon_scrape_failed", last_err, vendor=vendor, region=region, url=url)
