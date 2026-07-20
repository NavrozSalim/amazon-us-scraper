"""
Shared scraper infrastructure: structured response, rotating headers,
block/captcha detection, debug HTML saving, delay & retry helpers.

All vendor scrapers import from here — never duplicate this logic.
"""
import os
import re
import time
import random
import logging
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("scrapers")
MAX_DEBUG_HTML_BYTES = int(os.getenv("SCRAPER_DEBUG_HTML_MAX_BYTES", "200000"))

# ---------------------------------------------------------------------------
# Structured scrape result
# ---------------------------------------------------------------------------

@dataclass
class ScrapeResult:
    success: bool = False
    price: Optional[float] = None
    stock: Optional[int] = None
    title: Optional[str] = None
    error_code: str = ""
    error_message: str = ""
    raw_html_saved: bool = False
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "price": self.price,
            "stock": self.stock,
            "title": self.title,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "raw_html_saved": self.raw_html_saved,
        }

    def to_legacy(self) -> dict:
        """Backward-compatible dict consumed by sync/catalog tasks. Includes title when present."""
        out = {"price": self.price, "stock": self.stock}
        if self.title:
            out["title"] = self.title
        if not self.success and self.error_code:
            out["error_code"] = self.error_code
            out["error_message"] = (self.error_message or "")[:500]
        return out

    @classmethod
    def ok(cls, price: Optional[float], stock: Optional[int], title: Optional[str] = None, **meta) -> "ScrapeResult":
        return cls(success=True, price=price, stock=stock, title=title, metadata=meta)

    @classmethod
    def fail(cls, code: str, message: str, html: str = "", vendor: str = "", url: str = "") -> "ScrapeResult":
        saved = False
        if html:
            saved = save_debug_html(html, vendor, url, code)
        logger.warning("Scrape failed [%s] %s — %s", vendor, code, message)
        return cls(success=False, error_code=code, error_message=message, raw_html_saved=saved)


# ---------------------------------------------------------------------------
# Rotating user-agents & headers
# ---------------------------------------------------------------------------

USER_AGENTS = [
    # Chrome 131 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 130 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Chrome 131 – Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 130 – Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Chrome 131 – Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Firefox 133 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    # Firefox 133 – Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
    # Edge 131 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    # Safari 17 – Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    # Chrome 129 – Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    # Chrome 128 – Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    # Firefox 132 – Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
]

ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-US,en;q=0.9,es;q=0.8",
    "en-GB,en;q=0.9,en-US;q=0.8",
    "en-US,en;q=0.9,fr;q=0.8",
    "en-US,en;q=0.9,de;q=0.7",
]


def get_random_headers(referer: str = "") -> dict:
    """Build a realistic browser-like header set with rotation."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": random.choice(ACCEPT_LANGUAGES),
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none" if not referer else "same-origin",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        **({"Referer": referer} if referer else {}),
    }


# ---------------------------------------------------------------------------
# Block / CAPTCHA detection
# ---------------------------------------------------------------------------

_BLOCK_INDICATORS = [
    "captcha",
    "recaptcha",
    "verify you are human",
    "robot check",
    "security page",
    "access denied",
    "you have been blocked",
    "suspicious activity",
    "automated access",
    "please verify",
    "unusual traffic",
    "sorry, we just need to make sure you",
    "to discuss automated access",
    "enter the characters you see",
    "type the characters",
    "api-services-support@amazon.com",
]


def detect_block(html: str) -> tuple:
    """
    Returns (is_blocked: bool, reason: str).
    reason is one of: '', 'captcha', 'blocked', 'empty_response', 'truncated'.
    """
    if not html:
        return True, "empty_response"
    lower = html.lower()
    for indicator in _BLOCK_INDICATORS:
        if indicator in lower:
            if any(kw in indicator for kw in ("captcha", "robot check", "characters")):
                return True, "captcha"
            return True, "blocked"
    if len(html) < 500 and "<html" not in lower:
        return True, "truncated"
    return False, ""


def classify_failure(status_code: Optional[int], html: str, parse_failed: bool = False) -> str:
    if status_code == 404:
        return "not_found"
    if status_code in (401, 403):
        return "blocked"
    if status_code and status_code >= 500:
        return "upstream_error"
    blocked, reason = detect_block(html or "")
    if blocked:
        if reason == "captcha":
            return "captcha"
        if reason in {"blocked", "truncated"}:
            return "blocked"
        return "response_invalid"
    if parse_failed:
        return "parse_error"
    return "unknown"


def should_retry_failure(code: str) -> bool:
    retryable = {
        "timeout",
        "connection_error",
        "request_error",
        "challenge",
        "captcha",
        "blocked",
        "upstream_error",
        "response_invalid",
        "http_429",
        "http_500",
        "http_502",
        "http_503",
        "http_504",
    }
    return code in retryable or code.startswith("blocked_")


def is_amazon_captcha_page(html: str) -> bool:
    """Specific Amazon CAPTCHA check."""
    if not html:
        return False
    return "captchacharacters" in html.lower() or "robot check" in html.lower()


def is_amazon_dog_page(html: str) -> bool:
    """Amazon 'sorry' dog page — means IP/session is flagged."""
    if not html:
        return False
    lower = html.lower()
    return "sorry, we just need to make sure" in lower or "api-services-support@amazon.com" in lower


# ---------------------------------------------------------------------------
# Debug HTML saving
# ---------------------------------------------------------------------------

DEBUG_HTML_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_html")


def save_debug_html(html: str, vendor: str, url: str, error_code: str = "") -> bool:
    """Save bounded HTML + structured metadata for post-mortem analysis."""
    try:
        os.makedirs(DEBUG_HTML_DIR, exist_ok=True)
        _cleanup_old_debug_files(max_files=100)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = re.sub(r"[^\w]", "_", (url or "unknown").split("/")[-1][:40])
        filename = f"{vendor}_{error_code}_{ts}_{slug}.html"
        filepath = os.path.join(DEBUG_HTML_DIR, filename)
        metadata_path = filepath.replace(".html", ".json")
        html_bounded = (html or "")[:MAX_DEBUG_HTML_BYTES]
        truncated = len(html or "") > len(html_bounded)

        with open(filepath, "w", encoding="utf-8", errors="replace") as f:
            f.write(f"<!-- URL: {url} -->\n")
            f.write(f"<!-- Error: {error_code} -->\n")
            f.write(f"<!-- Time: {datetime.now().isoformat()} -->\n\n")
            f.write(html_bounded)

        with open(metadata_path, "w", encoding="utf-8") as meta:
            json.dump(
                {
                    "vendor": vendor,
                    "url": url,
                    "error_code": error_code,
                    "captured_at": datetime.now().isoformat(),
                    "stored_bytes": len(html_bounded.encode("utf-8", errors="replace")),
                    "truncated": truncated,
                },
                meta,
                indent=2,
            )

        logger.debug("Debug HTML saved: %s", filepath)
        return True
    except Exception as exc:
        logger.warning("Failed to save debug HTML: %s", exc)
        return False


def _cleanup_old_debug_files(max_files: int = 200):
    try:
        files = sorted(Path(DEBUG_HTML_DIR).glob("*.html"), key=lambda f: f.stat().st_mtime)
        if len(files) > max_files:
            for f in files[: len(files) - max_files]:
                f.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Delay & retry helpers
# ---------------------------------------------------------------------------

def random_delay(min_sec: float = 1.0, max_sec: float = 3.0):
    """Human-like random sleep."""
    time.sleep(random.uniform(min_sec, max_sec))


def backoff_delay(retry_num: int, base: float = 2.0, jitter: float = 1.5):
    """Exponential backoff: base^retry + random jitter."""
    delay = (base ** retry_num) + random.uniform(0, jitter)
    logger.debug("Backoff delay: %.2fs (retry #%d)", delay, retry_num)
    time.sleep(delay)


# ---------------------------------------------------------------------------
# Price / stock parsing helpers (shared across vendors)
# ---------------------------------------------------------------------------

def parse_price_text(text: str) -> Optional[float]:
    """Extract a float from price text like '$12.99', 'AUD 15.00', etc."""
    if not text:
        return None
    cleaned = str(text).replace(",", "").replace("\xa0", " ").strip()
    match = re.search(r"(\d+\.?\d*)", cleaned)
    if match:
        try:
            val = float(match.group(1))
            if 0.01 <= val < 999_999:
                return val
        except ValueError:
            pass
    return None
