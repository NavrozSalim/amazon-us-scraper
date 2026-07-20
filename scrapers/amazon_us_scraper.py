"""
Amazon US product scraper.

Primary method: requests + BeautifulSoup (fast, no browser needed).
Fallback: Selenium headless Chrome (for pages that require JS rendering).

Architecture:
  AmazonParser   — stateless HTML→data extraction (price, stock, title)
  AmazonHTTP     — requests-based fetch with USD cookie, retry, block detection
  AmazonDriver   — creates & manages a stealth Chrome instance (fallback only)
  AmazonScraper  — Selenium orchestrator (fallback only)

Public API (consumed by scrapers/__init__.py):
  scrape_amazon_us(vendor_url, region, session) -> {"price": float|None, "stock": int|None, "title": str|None}
  close_amazon_us_session(session)
"""
import re
import json
import time
import random
import logging
from datetime import date, datetime
from typing import Dict, Any, Optional
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from .core import (
    ScrapeResult, detect_block, is_amazon_captcha_page, is_amazon_dog_page,
    save_debug_html, random_delay, backoff_delay, parse_price_text, classify_failure, should_retry_failure,
    get_random_headers, USER_AGENTS, logger as _parent_logger,
)
from .amazon_us_rules import AmazonUSBusinessRules

logger = logging.getLogger("scrapers.amazon_us")

RETRY_LIMIT = 3
AMAZON_ZIP = "10001"
MAX_DELIVERY_DAYS = 7
_US_EASTERN = ZoneInfo("America/New_York")

_MONTH_NAME_TO_NUM = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

_MAP_PRICE_PHRASES = (
    "see price in cart",
    "see price at checkout",
    "see price in checkout",
)

_BUYBOX_ROOT_SELECTORS = (
    "#buybox",
    "#desktop_buybox",
    "#apex_desktop",
    "#rightCol",
)

_BUYBOX_FORM_SELECTORS = (
    "form#addToCart",
    "form[action*='handle-buy-box']",
    "form[action*='add-to-cart']",
)

_PRIMARY_DELIVERY_DATE_SELECTORS = (
    "#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE span.a-text-bold",
    "#deliveryBlockMessage span.a-text-bold",
    "#mir-layout-DELIVERY_BLOCK span.a-text-bold",
)

_DATE_WITH_WEEKDAY_RE = re.compile(
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+"
    r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+"
    r"(\d{1,2})(?:,?\s+(\d{4}))?",
    re.IGNORECASE,
)
_DATE_MONTH_DAY_RE = re.compile(
    r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+"
    r"(\d{1,2})(?:,?\s+(\d{4}))?",
    re.IGNORECASE,
)


def _today_us_eastern() -> date:
    return datetime.now(_US_EASTERN).date()


def _parse_delivery_days_from_text(text: str, today: date) -> Optional[int]:
    """Return calendar days from ``today`` until the Amazon delivery date in ``text``."""
    if not text:
        return None
    normalized = " ".join(str(text).split()).lower()
    if not normalized:
        return None

    if "today" in normalized:
        return 0
    if "tomorrow" in normalized:
        return 1

    match = _DATE_WITH_WEEKDAY_RE.search(normalized) or _DATE_MONTH_DAY_RE.search(normalized)
    if not match:
        return None

    month = _MONTH_NAME_TO_NUM.get(match.group(1).lower())
    if not month:
        return None
    try:
        day = int(match.group(2))
    except (TypeError, ValueError):
        return None

    year = today.year
    if match.lastindex and match.lastindex >= 3 and match.group(3):
        try:
            year = int(match.group(3))
        except (TypeError, ValueError):
            year = today.year

    try:
        delivery = date(year, month, day)
    except ValueError:
        return None

    if delivery < today and year == today.year:
        try:
            delivery = date(today.year + 1, month, day)
        except ValueError:
            return None

    return (delivery - today).days


# ═══════════════════════════════════════════════════════════════════════════
# HTML parser — stateless extraction from BeautifulSoup / raw HTML
# ═══════════════════════════════════════════════════════════════════════════

class AmazonParser:
    """Extract price, stock, and other data from an Amazon product page."""

    PRICE_SELECTORS = [
        "div.a-section.aok-hidden.twister-plus-buying-options-price-data",  # hidden JSON blob
        "#corePrice_feature_div span.a-offscreen",
        "span.priceToPay span.a-offscreen",
        ".apexPriceToPay span.a-offscreen",
        "span.a-price span.a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        "#priceblock_saleprice",
        ".a-price.a-text-price span.a-offscreen",
        "span.a-price-whole",
        "#corePriceDisplay_desktop_feature_div .a-offscreen",
    ]

    PRICE_JSON_PATTERNS = [
        r'"priceAmount":\s*([\d.]+)',
        r'"displayAmount"\s*:\s*"\$?([\d,.]+)"',
        r'"lowPrice"\s*:\s*"([\d.]+)"',
        r'"price"\s*:\s*"([\d.]+)"',
        r'"currentPrice"\s*:\s*{\s*"value"\s*:\s*"([\d.]+)"',
    ]

    AVAILABILITY_SELECTORS = [
        "#availability span",
        "span.a-color-price.a-text-bold",
        "div.a-spacing-base.a-spacing-top-micro",
        "#availability",
    ]

    TITLE_SELECTORS = [
        "#productTitle",
        "span#productTitle",
        "#title",
        "h1.a-size-large.product-title-word-break",
        "h1 span#productTitle",
    ]

    @classmethod
    def _buybox_root(cls, soup: BeautifulSoup):
        for sel in _BUYBOX_ROOT_SELECTORS:
            el = soup.select_one(sel)
            if el:
                return el
        return None

    @classmethod
    def is_map_price_page(cls, soup: BeautifulSoup, page_html: str = "") -> bool:
        """True when Amazon hides the advertised price (MAP / see-price-in-cart)."""
        if page_html:
            low_html = page_html.lower()
            if any(phrase in low_html for phrase in _MAP_PRICE_PHRASES):
                return True
        buybox = cls._buybox_root(soup)
        if buybox:
            low_box = buybox.get_text(" ", strip=True).lower()
            if any(phrase in low_box for phrase in _MAP_PRICE_PHRASES):
                return True
        return False

    @classmethod
    def extract_buybox_form_price(cls, soup: BeautifulSoup) -> Optional[float]:
        """Read MAP price from hidden add-to-cart form fields on the buy box."""
        forms = []
        for sel in _BUYBOX_FORM_SELECTORS:
            forms.extend(soup.select(sel))
        if not forms:
            buybox = cls._buybox_root(soup)
            if buybox:
                forms = buybox.select("form")

        seen = set()
        for form in forms:
            form_id = id(form)
            if form_id in seen:
                continue
            seen.add(form_id)

            amount_inp = form.select_one('input[name="items[0.base][customerVisiblePrice][amount]"]')
            if amount_inp and amount_inp.get("value"):
                p = parse_price_text(amount_inp["value"])
                if p:
                    return p

            display_inp = form.select_one('input[name="items[0.base][customerVisiblePrice][displayString]"]')
            if display_inp and display_inp.get("value"):
                p = parse_price_text(display_inp["value"])
                if p:
                    return p

        buybox = cls._buybox_root(soup)
        if buybox:
            box_html = str(buybox)
            m = re.search(
                r'customerVisiblePrice\]\[amount\]"[^>]*value="([\d.]+)"',
                box_html,
                re.IGNORECASE,
            )
            if m:
                p = parse_price_text(m.group(1))
                if p:
                    return p
        return None

    @classmethod
    def _extract_price_from_scope(cls, scope, page_html: str = "", *, allow_regex: bool = True) -> Optional[float]:
        if scope is None:
            return None

        json_div = scope.select_one("div.a-section.aok-hidden.twister-plus-buying-options-price-data")
        if json_div:
            try:
                data = json.loads(json_div.get_text(strip=True))
                group = data.get("desktop_buybox_group_1", [{}])[0]
                for key in ("priceAmount", "displayPrice"):
                    val = group.get(key)
                    if val is None:
                        continue
                    if isinstance(val, (int, float)):
                        if 0.01 <= float(val) < 999_999:
                            return float(val)
                    text = str(val)
                    if any(p in text.lower() for p in _MAP_PRICE_PHRASES):
                        continue
                    p = parse_price_text(text)
                    if p:
                        return p
            except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
                pass

        for sel in cls.PRICE_SELECTORS[1:]:
            elem = scope.select_one(sel)
            if elem:
                text = elem.get_text(strip=True)
                if any(p in text.lower() for p in _MAP_PRICE_PHRASES):
                    continue
                if _is_non_usd(text):
                    continue
                p = parse_price_text(text)
                if p:
                    return p

        if allow_regex and page_html:
            scoped_html = str(scope)
            for pat in cls.PRICE_JSON_PATTERNS:
                m = re.search(pat, scoped_html)
                if m:
                    p = parse_price_text(m.group(1))
                    if p:
                        return p
        return None

    @classmethod
    def extract_title(cls, soup: BeautifulSoup) -> Optional[str]:
        for sel in cls.TITLE_SELECTORS:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(separator=" ", strip=True)
                if t and len(t) > 2:
                    return t[:500]
        return None

    @classmethod
    def extract_price(cls, soup: BeautifulSoup, page_html: str = "") -> Optional[float]:
        """Try buybox-scoped selectors; MAP pages use hidden customerVisiblePrice fields."""
        form_price = cls.extract_buybox_form_price(soup)
        if form_price is not None:
            return form_price

        is_map = cls.is_map_price_page(soup, page_html)
        if is_map:
            return None

        buybox = cls._buybox_root(soup)
        if buybox:
            price = cls._extract_price_from_scope(
                buybox,
                page_html,
                allow_regex=not is_map,
            )
            if price is not None:
                return price

        return cls._extract_price_from_scope(soup, page_html, allow_regex=True)

    @classmethod
    def extract_delivery_days(cls, soup: BeautifulSoup, *, today: Optional[date] = None) -> Optional[int]:
        """Days until primary delivery date (US Eastern calendar days)."""
        today = today or _today_us_eastern()
        days_found = []
        for sel in _PRIMARY_DELIVERY_DATE_SELECTORS:
            for elem in soup.select(sel):
                text = elem.get_text(" ", strip=True)
                days = _parse_delivery_days_from_text(text, today)
                if days is not None:
                    days_found.append(days)
            if days_found:
                break
        if not days_found:
            return None
        return min(days_found)

    @classmethod
    def _availability_stock(cls, soup: BeautifulSoup) -> Optional[int]:
        """Derive stock from availability text only (before delivery-day gate)."""
        texts = []
        for sel in cls.AVAILABILITY_SELECTORS:
            elem = soup.select_one(sel)
            if elem:
                texts.append(elem.get_text(strip=True).lower())

        combined = " ".join(texts)

        if any(kw in combined for kw in ("unavailable", "out of stock", "sold out", "not available")):
            return 0

        m = re.search(r"only\s+(\d+)\s+left", combined)
        if m:
            return int(m.group(1))

        m = re.search(r"(\d+)\s+left", combined)
        if m:
            return int(m.group(1))

        if "in stock" in combined:
            return 99

        return None

    @classmethod
    def extract_stock(cls, soup: BeautifulSoup, *, today: Optional[date] = None) -> Optional[int]:
        """Derive stock from availability; zero when primary delivery is more than 7 days out."""
        stock = cls._availability_stock(soup)
        if stock is None or stock <= 0:
            return stock

        delivery_days = cls.extract_delivery_days(soup, today=today)
        if delivery_days is not None and delivery_days > MAX_DELIVERY_DAYS:
            logger.info(
                "Amazon US stock zeroed: delivery in %s days (> %s)",
                delivery_days,
                MAX_DELIVERY_DAYS,
            )
            return 0

        return stock

    @classmethod
    def is_valid_product_page(cls, soup: BeautifulSoup) -> bool:
        return bool(
            soup.select_one("#productTitle")
            or soup.select_one("#title")
            or soup.select_one("span#productTitle")
        )

    @classmethod
    def parse_full(cls, soup: BeautifulSoup, url: str, page_html: str = "") -> Dict[str, Any]:
        main_price_raw = "N/A"
        price = cls.extract_price(soup, page_html)
        if price is not None:
            main_price_raw = str(price)

        inv_el = soup.select_one("#availability span")
        inventory_text = inv_el.get_text(strip=True) if inv_el else "N/A"

        cu_el = soup.select_one("span.a-color-price.a-text-bold")
        cu_text = cu_el.get_text(strip=True) if cu_el else ""

        handling_el = soup.find(string=re.compile(r"Usually (?:ships|dispatched) within", re.IGNORECASE))
        handling_time = handling_el.strip() if handling_el else ""

        title = cls.extract_title(soup)

        return {
            "URL": url,
            "Title": title or "",
            "Main Price": main_price_raw,
            "Inventory": inventory_text,
            "Currently Unavailable": cu_text,
            "Handling Time": handling_time,
            "Scrape Time": datetime.now().strftime("%m-%d-%Y / %I:%M %p"),
        }


def _extract_asin(url: str, soup: BeautifulSoup = None) -> Optional[str]:
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url or "", re.IGNORECASE)
    if m:
        return m.group(1).upper()
    if soup is not None:
        for sel in ('input#ASIN', 'input[name="ASIN"]'):
            el = soup.select_one(sel)
            if el and el.get("value"):
                return str(el["value"]).upper()
    return None


def _is_non_usd(price_text: str) -> bool:
    """Detect non-USD currency prefixes so we can skip mis-geolocated prices."""
    if not price_text:
        return False
    prefixes = ("PKR", "INR", "AED", "EUR", "GBP", "CAD", "AUD", "JPY", "CNY", "₹", "€", "£", "¥")
    stripped = price_text.strip()
    for pfx in prefixes:
        if stripped.upper().startswith(pfx):
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# HTTP-based scraper (primary, fast) — requests + BeautifulSoup
# ═══════════════════════════════════════════════════════════════════════════

_USD_COOKIES = {
    "i18n-prefs": "USD",
    "lc-main": "en_US",
}

FETCH_TIMEOUT = 30


class AmazonHTTP:
    """Fast requests-based Amazon scraper. No browser needed."""

    _ZIP_CHANGE_URL = "https://www.amazon.com/gp/delivery/ajax/address-change.html"

    @classmethod
    def _get_session(cls, session_dict: dict) -> requests.Session:
        """Reuse or create a requests.Session, with delivery ZIP set to 10001."""
        if session_dict is not None and "amazon_http_session" in session_dict:
            return session_dict["amazon_http_session"]
        s = requests.Session()
        s.headers.update(get_random_headers("https://www.amazon.com/"))
        s.cookies.update(_USD_COOKIES)
        if session_dict is not None:
            session_dict["amazon_http_session"] = s
        return s

    @classmethod
    def _ensure_zip(cls, s: requests.Session, seed_url: str, session_dict: dict):
        """Set delivery location to US ZIP 10001 via Amazon's address-change API."""
        if session_dict is not None and session_dict.get("amazon_http_zip_set"):
            return
        try:
            s.get(seed_url, timeout=FETCH_TIMEOUT)
            resp = s.post(
                cls._ZIP_CHANGE_URL,
                data={
                    "locationType": "LOCATION_INPUT",
                    "zipCode": AMAZON_ZIP,
                    "storeContext": "generic",
                    "deviceType": "web",
                    "pageType": "Detail",
                    "actionSource": "glow",
                },
                headers={
                    "x-requested-with": "XMLHttpRequest",
                    "referer": seed_url,
                },
                timeout=FETCH_TIMEOUT,
            )
            ok = resp.status_code == 200 and resp.json().get("isAddressUpdated")
            if ok:
                logger.info("HTTP session ZIP set to %s", AMAZON_ZIP)
            else:
                logger.warning("ZIP update response: %s", resp.text[:200])
        except Exception as exc:
            logger.warning("Failed to set ZIP via HTTP: %s", exc)
            ok = False
        if session_dict is not None:
            session_dict["amazon_http_zip_set"] = ok

    @classmethod
    def fetch(cls, url: str, session_dict: dict = None) -> ScrapeResult:
        """Fetch an Amazon product page via HTTP and parse price/stock/title."""
        s = cls._get_session(session_dict)
        cls._ensure_zip(s, url, session_dict)
        try:
            resp = s.get(url, timeout=FETCH_TIMEOUT, allow_redirects=True)
        except requests.Timeout:
            return ScrapeResult.fail("timeout", "HTTP request timed out", "", "amazon_us", url)
        except requests.ConnectionError as exc:
            return ScrapeResult.fail("connection_error", str(exc), "", "amazon_us", url)
        except requests.RequestException as exc:
            return ScrapeResult.fail("request_error", str(exc), "", "amazon_us", url)

        html = resp.text
        if resp.status_code != 200:
            failure_code = classify_failure(resp.status_code, html, parse_failed=False)
            return ScrapeResult.fail(
                f"http_{resp.status_code}" if failure_code == "unknown" else failure_code,
                f"HTTP {resp.status_code}",
                html,
                "amazon_us",
                url,
            )

        blocked, reason = detect_block(html)
        if blocked:
            return ScrapeResult.fail(f"blocked_{reason}", f"Blocked: {reason}", html, "amazon_us", url)

        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            soup = BeautifulSoup(html, "html.parser")

        if not AmazonParser.is_valid_product_page(soup):
            return ScrapeResult.fail("not_product_page", "Not a product page", html, "amazon_us", url)

        price = AmazonParser.extract_price(soup, html)
        stock = AmazonParser.extract_stock(soup)
        title = AmazonParser.extract_title(soup)

        if price is None and AmazonParser.is_map_price_page(soup, html):
            return ScrapeResult.fail(
                "map_price_unavailable",
                "MAP price hidden and customerVisiblePrice not found via HTTP",
                html,
                "amazon_us",
                url,
            )

        if price is None:
            return ScrapeResult.fail("no_price", "Price not found via HTTP", html, "amazon_us", url)

        if stock is None and price is not None:
            stock = 2

        return ScrapeResult.ok(price=price, stock=stock, title=title)

    @classmethod
    def scrape_with_retry(cls, url: str, session_dict: dict = None) -> ScrapeResult:
        """Fetch + parse with retries and backoff."""
        last_result = None
        for attempt in range(RETRY_LIMIT):
            if attempt > 0:
                backoff_delay(attempt, base=2.0, jitter=1.5)
                logger.info("HTTP retry %d/%d for %s", attempt + 1, RETRY_LIMIT, url)

            result = cls.fetch(url, session_dict)
            if result.success:
                return result
            last_result = result

            if result.error_code in ("http_404", "not_found", "not_product_page"):
                break
            if not should_retry_failure(result.error_code):
                break
            if result.error_code.startswith("blocked") or result.error_code in {"blocked", "captcha"}:
                s = cls._get_session(session_dict)
                s.headers.update(get_random_headers("https://www.amazon.com/"))

        return last_result or ScrapeResult.fail("max_retries", "All HTTP attempts failed", "", "amazon_us", url)


# ═══════════════════════════════════════════════════════════════════════════
# Selenium-based scraper (fallback only)
# ═══════════════════════════════════════════════════════════════════════════

class AmazonDriver:
    """Create and configure a stealth headless Chrome for Amazon."""

    @staticmethod
    def create():
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--lang=en-US")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--disable-infobars")
        opts.add_argument("--disable-popup-blocking")
        opts.add_argument(f"--user-agent={random.choice(USER_AGENTS)}")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)

        try:
            from webdriver_manager.chrome import ChromeDriverManager
            from selenium.webdriver.chrome.service import Service
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=opts)
        except ImportError:
            driver = webdriver.Chrome(options=opts)

        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                window.chrome = {runtime: {}};
            """},
        )

        logger.info("Chrome driver created (stealth mode, fallback)")
        return driver

    @staticmethod
    def quit_safe(driver):
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    @staticmethod
    def set_zoom(driver, scale: float = 0.5):
        try:
            size = driver.get_window_size()
            driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                "mobile": False,
                "width": size.get("width", 1920),
                "height": size.get("height", 1080),
                "deviceScaleFactor": 1,
                "screenWidth": size.get("width", 1920),
                "screenHeight": size.get("height", 1080),
                "positionX": 0, "positionY": 0,
            })
            driver.execute_cdp_cmd("Emulation.setPageScaleFactor", {"pageScaleFactor": scale})
        except Exception as exc:
            logger.debug("CDP zoom failed, using CSS fallback: %s", exc)
            try:
                driver.execute_script(
                    "document.body.style.transformOrigin='0 0';"
                    f"document.body.style.transform='scale({scale})';"
                    f"document.body.style.width='{int(100/scale)}%';"
                )
            except Exception:
                pass


class AmazonScraper:
    """Selenium orchestrator — used as fallback when HTTP scrape fails."""

    @staticmethod
    def solve_captcha(driver) -> bool:
        try:
            from amazoncaptcha import AmazonCaptcha
        except ImportError:
            logger.warning("amazoncaptcha not installed")
            return False
        try:
            from selenium.webdriver.common.by import By
            img = driver.find_element(By.XPATH, "//div[@class='a-row a-text-center']//img")
            link = img.get_attribute("src")
            captcha = AmazonCaptcha.fromlink(link)
            value = captcha.solve()
            inp = driver.find_element(By.ID, "captchacharacters")
            inp.clear()
            inp.send_keys(value)
            driver.find_element(By.CLASS_NAME, "a-button-text").click()
            time.sleep(random.uniform(2, 4))
            logger.info("CAPTCHA solved")
            return True
        except Exception as exc:
            logger.debug("CAPTCHA solve failed: %s", exc)
            return False

    @classmethod
    def _safe_click(cls, driver, elem):
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
            try:
                elem.click()
            except Exception:
                driver.execute_script("arguments[0].click();", elem)
            return True
        except Exception:
            return False

    @classmethod
    def set_zip_on_product_page(cls, driver, product_url: str) -> bool:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        try:
            driver.get(product_url)
            AmazonDriver.set_zoom(driver, 0.5)
            random_delay(2, 4)

            html = driver.page_source
            if is_amazon_captcha_page(html):
                cls.solve_captcha(driver)
                random_delay(1, 2)
            if is_amazon_dog_page(html):
                save_debug_html(html, "amazon_us", product_url, "dog_page_zip")
                return False

            wait = WebDriverWait(driver, 8)
            try:
                consent = wait.until(EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "#sp-cc-accept, input#sp-cc-accept, button#sp-cc-accept")
                ))
                cls._safe_click(driver, consent)
            except Exception:
                pass

            random_delay(1.5, 3)
            loc_selectors = [
                "#contextualIngressPt", "#glow-ingress-block",
                "a[data-csa-c-content-id='nav_cs_gb_td_address']",
                "#nav-global-location-popover-link", "span#nav-global-location-slot",
            ]
            clicked = False
            for sel in loc_selectors:
                try:
                    elem = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
                    cls._safe_click(driver, elem)
                    random_delay(2, 4)
                    clicked = True
                    break
                except Exception:
                    continue

            if not clicked:
                return True

            if is_amazon_captcha_page(driver.page_source):
                cls.solve_captcha(driver)

            zip_input = None
            for sel in [
                "input.GLUX_Full_Width", "#GLUXZipUpdateInput",
                "#GLUXPostalCodeWithCity_PostalCodeInput",
                "input[placeholder*='ZIP']", "input[placeholder*='zip']",
            ]:
                try:
                    zip_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                    break
                except Exception:
                    continue

            if not zip_input:
                return True

            zip_input.clear()
            zip_input.send_keys(AMAZON_ZIP)
            random_delay(1, 2)

            for sel in [
                "#GLUXZipUpdate input",
                "#GLUXPostalCodeWithCityApplyButton input",
                "#GLUXPostalCodeWithCityApplyButton .a-button-input",
            ]:
                try:
                    btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
                    cls._safe_click(driver, btn)
                    break
                except Exception:
                    continue
            else:
                zip_input.send_keys(Keys.RETURN)

            random_delay(2, 4)
            driver.refresh()
            AmazonDriver.set_zoom(driver, 0.5)
            random_delay(2, 4)
            if is_amazon_captcha_page(driver.page_source):
                cls.solve_captcha(driver)

            logger.info("ZIP set to %s", AMAZON_ZIP)
            return True

        except Exception as exc:
            logger.error("ZIP setup failed: %s", exc)
            return False

    @classmethod
    def _parse_product_page(cls, soup: BeautifulSoup, url: str, html: str) -> ScrapeResult:
        if not AmazonParser.is_valid_product_page(soup):
            return ScrapeResult.fail("not_product_page", "Not a product page", html, "amazon_us", url)

        price = AmazonParser.extract_price(soup, html)
        stock = AmazonParser.extract_stock(soup)
        title = AmazonParser.extract_title(soup)

        if price is None and AmazonParser.is_map_price_page(soup, html):
            return ScrapeResult.fail(
                "map_price_unavailable",
                "MAP price hidden and customerVisiblePrice not found",
                html,
                "amazon_us",
                url,
            )

        if price is None:
            data = AmazonParser.parse_full(soup, url, html)
            processed = AmazonUSBusinessRules.process_scraped_data(data)
            if processed.get("error_details"):
                return ScrapeResult.fail("parse_failed", processed["error_details"], html, "amazon_us", url)
            price = processed.get("final_price")
            stock = processed.get("final_inventory")

        if price is None:
            return ScrapeResult.fail("no_price", "Price not found", html, "amazon_us", url)

        return ScrapeResult.ok(
            price=float(price),
            stock=int(stock) if stock is not None else None,
            title=title,
        )

    @classmethod
    def extract_cart_price(cls, soup: BeautifulSoup, asin: str) -> Optional[float]:
        if not asin:
            return None
        row = soup.select_one(f'[data-asin="{asin}"], div.sc-list-item[data-asin="{asin}"]')
        if row:
            for sel in (
                "span.a-price span.a-offscreen",
                ".sc-product-price",
                ".sc-price",
                "span.sc-product-price",
            ):
                el = row.select_one(sel)
                if el:
                    p = parse_price_text(el.get_text(strip=True))
                    if p:
                        return p
        for sel in (
            "#sc-active-cart .sc-product-price",
            "#activeCartViewForm span.a-price span.a-offscreen",
        ):
            el = soup.select_one(sel)
            if el:
                p = parse_price_text(el.get_text(strip=True))
                if p:
                    return p
        return None

    @classmethod
    def fetch_map_price_via_cart(cls, url: str, driver, soup: BeautifulSoup = None) -> Optional[float]:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        asin = _extract_asin(url, soup)
        if not asin:
            return None

        add_selectors = [
            "#add-to-cart-button",
            "input#add-to-cart-button",
            "input[name='submit.add-to-cart']",
            "#submit.add-to-cart input",
            "#addToCart input[name='submit.add-to-cart']",
        ]
        clicked = False
        for sel in add_selectors:
            try:
                btn = WebDriverWait(driver, 6).until(EC.element_to_be_clickable((By.CSS_SELECTOR, sel)))
                cls._safe_click(driver, btn)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            logger.warning("MAP cart flow: add-to-cart button not found for %s", url[:80])
            return None

        random_delay(2, 4)
        current = (driver.current_url or "").lower()
        if "cart" not in current:
            try:
                driver.get("https://www.amazon.com/gp/cart/view.html")
                random_delay(2, 3)
            except Exception:
                pass

        html = driver.page_source
        try:
            cart_soup = BeautifulSoup(html, "lxml")
        except Exception:
            cart_soup = BeautifulSoup(html, "html.parser")
        price = cls.extract_cart_price(cart_soup, asin)
        if price is not None:
            logger.info("MAP cart price for %s: %s", asin, price)
        return price

    @classmethod
    def fetch_and_parse(cls, url: str, driver) -> ScrapeResult:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        try:
            driver.get(url)
            random_delay(2, 4)
            AmazonDriver.set_zoom(driver, 0.6)

            html = driver.page_source

            if is_amazon_captcha_page(html):
                solved = cls.solve_captcha(driver)
                if not solved:
                    return ScrapeResult.fail("captcha", "CAPTCHA unsolvable", html, "amazon_us", url)
                html = driver.page_source
                if is_amazon_captcha_page(html):
                    return ScrapeResult.fail("captcha_unsolved", "CAPTCHA persisted", html, "amazon_us", url)

            if is_amazon_dog_page(html):
                return ScrapeResult.fail("dog_page", "Amazon block page", html, "amazon_us", url)

            blocked, reason = detect_block(html)
            if blocked:
                return ScrapeResult.fail(f"blocked_{reason}", f"Blocked: {reason}", html, "amazon_us", url)

            for sel in ["span.a-price", "#corePrice_feature_div", ".apexPriceToPay", "#availability"]:
                    try:
                        WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                        break
                    except Exception:
                        continue
            time.sleep(1)

            html = driver.page_source
            try:
                soup = BeautifulSoup(html, "lxml")
            except Exception:
                soup = BeautifulSoup(html, "html.parser")

            result = cls._parse_product_page(soup, url, html)
            if result.success:
                return result

            if (
                result.error_code in ("map_price_unavailable", "no_price", "parse_failed")
                and AmazonParser.is_map_price_page(soup, html)
            ):
                cart_price = cls.fetch_map_price_via_cart(url, driver, soup)
                if cart_price is not None:
                    stock = AmazonParser.extract_stock(soup)
                    title = AmazonParser.extract_title(soup)
                    return ScrapeResult.ok(price=float(cart_price), stock=stock, title=title)

            return result

        except Exception as exc:
            logger.exception("Selenium scrape error for %s", url)
            try:
                html = driver.page_source
            except Exception:
                html = ""
            return ScrapeResult.fail("exception", str(exc), html, "amazon_us", url)

    @classmethod
    def scrape_with_retry(cls, url: str, driver) -> ScrapeResult:
        last_result = None
        for attempt in range(RETRY_LIMIT):
            if attempt > 0:
                backoff_delay(attempt, base=3.0, jitter=2.0)
            result = cls.fetch_and_parse(url, driver)
            if result.success:
                return result
            last_result = result
            if result.error_code in ("captcha_unsolved", "dog_page"):
                break
            if result.error_code in ("not_product_page", "parse_failed") and attempt >= 1:
                break
        return last_result or ScrapeResult.fail("max_retries", "All Selenium retries exhausted", "", "amazon_us", url)


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

def scrape_amazon_us(vendor_url: str, region: str, session: dict = None) -> dict:
    """
    Scrape Amazon US product page for price, stock, and title.

    Strategy: try fast HTTP scrape first; fall back to Selenium if blocked or
    price not found (Amazon sometimes requires JS for pricing).
    """
    if session is None:
        session = {}

    # --- Primary: HTTP ---
    http_result = AmazonHTTP.scrape_with_retry(vendor_url, session)
    if http_result.success:
        logger.info("HTTP scrape OK for %s (price=%s)", vendor_url[:60], http_result.price)
        return http_result.to_legacy()

    logger.info(
        "HTTP scrape failed [%s], trying Selenium fallback for %s",
        http_result.error_code, vendor_url[:60],
    )

    # --- Fallback: Selenium ---
    use_selenium = True
    try:
        from selenium import webdriver  # noqa: F401
    except ImportError:
        logger.warning("Selenium not installed — cannot fall back to browser scrape")
        use_selenium = False

    if not use_selenium:
        return http_result.to_legacy()

    driver = None
    created_driver = False
    try:
        if session.get("amazon_us_driver"):
            driver = session["amazon_us_driver"]
        else:
            driver = AmazonDriver.create()
            created_driver = True
            session["amazon_us_driver"] = driver

        zip_set = session.get("amazon_us_location_set", False)
        if not zip_set:
            success = AmazonScraper.set_zip_on_product_page(driver, vendor_url)
            if success:
                session["amazon_us_location_set"] = True
            try:
                html = driver.page_source
                soup = BeautifulSoup(html, "html.parser")
                if AmazonParser.is_valid_product_page(soup):
                    price = AmazonParser.extract_price(soup, html)
                    stock = AmazonParser.extract_stock(soup)
                    tit = AmazonParser.extract_title(soup)
                    if price is None and AmazonParser.is_map_price_page(soup, html):
                        price = AmazonScraper.fetch_map_price_via_cart(vendor_url, driver, soup)
                    if price is not None:
                        out = {"price": float(price), "stock": int(stock) if stock is not None else None}
                        if tit:
                            out["title"] = tit
                        return out
            except Exception:
                pass

        result = AmazonScraper.scrape_with_retry(vendor_url, driver)
        if not result.success:
            logger.warning(
                "Selenium fallback also failed: url=%s code=%s msg=%s",
                vendor_url, result.error_code, result.error_message,
            )
        return result.to_legacy()

    except Exception as exc:
        logger.exception("Selenium fallback exception for %s: %s", vendor_url, exc)
        return http_result.to_legacy()

    finally:
        if created_driver and driver and not session.get("amazon_us_driver"):
            AmazonDriver.quit_safe(driver)


def close_amazon_us_session(session):
    """Close and cleanup Amazon US driver if present in session."""
    if session is None:
        return
    driver = session.pop("amazon_us_driver", None)
    AmazonDriver.quit_safe(driver)
    session.pop("amazon_us_location_set", None)
    http_sess = session.pop("amazon_http_session", None)
    if http_sess:
        try:
            http_sess.close()
        except Exception:
            pass
