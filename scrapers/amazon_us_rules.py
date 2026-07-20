"""
Amazon US business rules for processing scraped product data.
Converts raw page data to final price and inventory for VendorPrice/Scrape.
"""
import re
import logging

logger = logging.getLogger(__name__)


class AmazonUSBusinessRules:
    """Process scraped Amazon US data into price and inventory."""

    @staticmethod
    def _parse_price(text: str):
        """Extract float from price string like '$12.99' or '12.99'."""
        if not text or text == 'N/A':
            return None
        match = re.search(r'[\d,]+\.?\d*', str(text).replace(',', ''))
        if match:
            try:
                return float(match.group().replace(',', ''))
            except ValueError:
                return None
        return None

    @staticmethod
    def _parse_inventory(inventory_text: str, unavailable_text: str) -> int | None:
        """
        Derive stock from inventory and unavailable strings.
        - "Currently unavailable" / "out of stock" -> 0
        - "Only X left" -> X
        - "In Stock" -> 99
        """
        combined = f"{inventory_text or ''} {unavailable_text or ''}".lower()

        if "unavailable" in combined or "out of stock" in combined or "sold out" in combined:
            return 0

        only_match = re.search(r'only\s+(\d+)\s+left', combined)
        if only_match:
            return int(only_match.group(1))

        left_match = re.search(r'(\d+)\s+left', combined)
        if left_match:
            return int(left_match.group(1))

        if "in stock" in combined:
            return 99

        return None

    @classmethod
    def process_scraped_data(cls, data: dict) -> dict:
        """
        Process raw scraped data into final price and inventory.
        Input: Main Price, Inventory, Currently Unavailable, etc.
        Output: final_price, final_inventory, raw_*, error_details, needs_rescrape
        """
        main_price = data.get('Main Price') or data.get('main_price') or ''
        inventory = data.get('Inventory') or data.get('inventory') or ''
        currently_unavailable = data.get('Currently Unavailable') or data.get('currently_unavailable') or ''

        raw_price = cls._parse_price(main_price)
        raw_quantity = cls._parse_inventory(inventory, currently_unavailable)

        error_details = ''
        if not main_price or main_price == 'N/A':
            error_details = error_details or 'Price not found'
        if raw_quantity is None and 'unavailable' not in (inventory + currently_unavailable).lower():
            error_details = (error_details + '; Inventory unclear').strip('; ')

        final_price = raw_price
        final_inventory = raw_quantity if raw_quantity is not None else 0
        needs_rescrape = bool(error_details)

        return {
            'raw_price': raw_price,
            'raw_shipping': None,
            'raw_quantity': raw_quantity,
            'raw_handling_time': data.get('Handling Time'),
            'raw_seller_away': None,
            'raw_ended_listings': None,
            'calculated_shipping_price': None,
            'final_price': final_price,
            'final_inventory': final_inventory,
            'needs_rescrape': needs_rescrape,
            'error_details': error_details,
        }
