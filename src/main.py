"""Apify Actor entry — Seller Pilot — Amazon US Scraper (production scraper)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow `import scrapers...` and django/vendor stubs at actor root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apify import Actor

from scrapers.amazon_us_scraper import scrape_amazon_us as _scrape
from .normalize import normalize_result


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}
        url = str(actor_input.get("url") or actor_input.get("productId") or "").strip()
        region = str(actor_input.get("region") or "USA").strip() or "USA"
        proxy_urls = actor_input.get("proxyUrls") or []
        if isinstance(proxy_urls, str):
            proxy_urls = [p.strip() for p in proxy_urls.split(",") if p.strip()]
        timeout_secs = int(actor_input.get("timeoutSecs") or 90)

        if not url:
            await Actor.fail(status_message="Input field `url` is required.")
            return

        # Optional env overrides from input
        for key in (
            "ALIEXPRESS_APP_KEY",
            "ALIEXPRESS_APP_SECRET",
            "ALIEXPRESS_ACCESS_TOKEN",
            "ALIEXPRESS_API_URL",
        ):
            val = actor_input.get(key) or actor_input.get(key.lower())
            if val:
                os.environ[key] = str(val)

        if proxy_urls:
            # Shared proxy env knobs used by Costco/eBay helpers
            os.environ.setdefault("HTTP_PROXY", proxy_urls[0])
            os.environ.setdefault("HTTPS_PROXY", proxy_urls[0])
            os.environ["COSTCO_AU_PROXY_URLS"] = ",".join(proxy_urls)
            os.environ["EBAY_AU_PROXY_URLS"] = ",".join(proxy_urls)

        session: dict = {"timeout": timeout_secs}
        token = (actor_input.get("accessToken") or os.getenv("ALIEXPRESS_ACCESS_TOKEN") or "").strip()
        if token:
            os.environ["ALIEXPRESS_ACCESS_TOKEN"] = token
            session["aliexpress_access_token"] = token

        Actor.log.info("Scraping vendor=amazonus region=%s url=%s", region, url[:120])
        try:
            raw = _scrape(url, region, session)
        except Exception as exc:  # noqa: BLE001
            Actor.log.exception("Scraper raised")
            raw = {
                "price": None,
                "stock": None,
                "title": None,
                "error_code": "scraper_exception",
                "error_message": str(exc)[:500],
            }

        result = normalize_result(raw, vendor="amazonus", region=region, url=url)
        await Actor.push_data(result)
        await Actor.set_value("OUTPUT", result)
        if not result.get("success"):
            Actor.log.warning(
                "Scrape failed [%s] %s",
                result.get("error_code"),
                result.get("error_message"),
            )
