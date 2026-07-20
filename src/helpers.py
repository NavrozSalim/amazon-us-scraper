"""Shared HTTP helpers for Apify scrapers."""
from __future__ import annotations

import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]


def ok(price, stock, title=None, **meta) -> dict:
    return {
        "success": True,
        "price": price,
        "stock": stock,
        "title": title,
        "error_code": "",
        "error_message": "",
        **meta,
    }


def fail(code: str, message: str, **meta) -> dict:
    return {
        "success": False,
        "price": None,
        "stock": None,
        "title": meta.pop("title", None),
        "error_code": code,
        "error_message": message,
        **meta,
    }


def parse_money(text: str | None) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.,]", "", str(text))
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        cleaned = cleaned.replace(",", ".") if len(parts[-1]) <= 2 else cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def fetch_html(
    url: str,
    *,
    proxy_urls: list[str] | None = None,
    timeout_secs: int = 45,
    headers: dict | None = None,
) -> tuple[str, int]:
    proxy = (proxy_urls or [None])[0]
    hdrs = {
        "User-Agent": USER_AGENTS[0],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        **(headers or {}),
    }
    with httpx.Client(
        follow_redirects=True,
        timeout=timeout_secs,
        proxy=proxy,
        headers=hdrs,
    ) as client:
        resp = client.get(url)
        return resp.text, resp.status_code


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")
