#!/usr/bin/env python3
"""
Bicep @ Royal Albert Hall – Twickets Resale Monitor
Sends a WhatsApp alert via Twilio when tickets appear on Twickets.

Uses Selenium with headless Chrome to bypass CloudFront WAF blocking.
"""

import os
import time
import logging
import json
from datetime import datetime
from twilio.rest import Client
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

# ─────────────────────────────────────────
# CONFIG  (set these as environment vars or edit directly)
# ─────────────────────────────────────────
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN",  "your_auth_token_here")

# WhatsApp numbers must be in E.164 format, e.g. +447911123456
WHATSAPP_FROM = os.getenv("WHATSAPP_FROM", "whatsapp:+14155238886")   # Twilio sandbox number
WHATSAPP_TO   = os.getenv("WHATSAPP_TO",   "whatsapp:+447911123456")  # YOUR number

# How often to check (seconds). 300 = every 5 minutes.
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))

# ─────────────────────────────────────────
# TWICKETS SEARCH  (official RAH resale partner)
# ─────────────────────────────────────────
TWICKETS_URL = "https://www.twickets.live/search/bicep?regionId=gb&lang=en_GB"
TWICKETS_EVENT_URL = "https://www.twickets.live/app/block"

# Keywords that must appear in an event title/venue to be a match
REQUIRED_KEYWORDS = ["bicep"]
VENUE_KEYWORDS    = ["royal albert", "rah"]
TARGET_YEAR_MONTH = ("2026", "11")   # November 2026

# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("monitor.log"),
    ],
)
log = logging.getLogger(__name__)


def fetch_twickets_listings() -> list[dict]:
    """Use Selenium to fetch Twickets listings (bypasses CloudFront WAF)."""
    driver = None
    try:
        log.info("Starting headless Chrome browser...")
        
        # Configure Chrome options for headless mode
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        
        driver = webdriver.Chrome(options=chrome_options)
        
        log.info("Loading Twickets search page: %s", TWICKETS_URL)
        driver.get(TWICKETS_URL)
        
        # Wait for the page to load and JavaScript to render listings
        log.info("Waiting for listings to load...")
        WebDriverWait(driver, 10).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "[data-listing-id], .listing-item, [class*='listing']"))
        )
        
        time.sleep(2)  # Extra wait for any AJAX to complete
        
        # Try to extract listings from window.__data or similar
        log.info("Extracting listings data from page...")
        listings = driver.execute_script("""
            // Try multiple ways to find listings data
            
            // Method 1: Check window.__data or __INITIAL_STATE__
            if (window.__data && window.__data.listings) {
                return window.__data.listings;
            }
            if (window.__INITIAL_STATE__ && window.__INITIAL_STATE__.listings) {
                return window.__INITIAL_STATE__.listings;
            }
            
            // Method 2: Parse from DOM elements
            const results = [];
            document.querySelectorAll('[data-listing-id], .listing-item').forEach(el => {
                const listing = {
                    id: el.getAttribute('data-listing-id') || el.id,
                    title: el.querySelector('[class*="title"], h3')?.textContent || '',
                    eventName: el.querySelector('[class*="event"], h2')?.textContent || '',
                    venueName: el.querySelector('[class*="venue"]')?.textContent || '',
                    date: el.querySelector('[class*="date"]')?.textContent || '',
                    price: el.querySelector('[class*="price"]')?.textContent || '',
                    quantity: el.querySelector('[class*="quantity"]')?.textContent || '',
                };
                if (listing.id || listing.title || listing.eventName) {
                    results.push(listing);
                }
            });
            
            return results.length > 0 ? results : null;
        """)
        
        if listings:
            log.info("Successfully extracted %d listings from page", len(listings))
            return listings if isinstance(listings, list) else []
        
        log.warning("Could not extract listings from page - checking network responses...")
        return []
        
    except Exception as exc:
        log.error("Selenium error: %s", exc, exc_info=True)
        return []
    finally:
        if driver:
            driver.quit()
            log.info("Browser closed")


def is_target_event(listing: dict) -> bool:
    """Return True if this listing looks like Bicep @ RAH in Nov 2026."""
    # Flatten all text fields for easy searching
    blob = " ".join([
        str(listing.get("eventName", "")),
        str(listing.get("name", "")),
        str(listing.get("venueName", "")),
        str(listing.get("venue", "")),
        str(listing.get("title", "")),
        str(listing.get("event", "")),
    ]).lower()

    # Must mention Bicep
    if not any(kw in blob for kw in REQUIRED_KEYWORDS):
        return False

    # Must mention Royal Albert Hall
    if not any(kw in blob for kw in VENUE_KEYWORDS):
        return False

    # Must be in November 2026
    date_str = str(
        listing.get("eventDate") or
        listing.get("date") or
        listing.get("startDate") or ""
    )
    if TARGET_YEAR_MONTH[0] not in date_str or TARGET_YEAR_MONTH[1] not in date_str:
        # Try a looser check: just look for "2026" in the blob
        if "2026" not in blob and "2026" not in date_str:
            return False

    return True


def build_alert_message(listings: list[dict]) -> str:
    """Format a WhatsApp message summarising available tickets."""
    lines = [
        "🎉 *Bicep @ Royal Albert Hall* tickets just appeared on Twickets!\n"
    ]
    for lst in listings[:5]:   # cap at 5 to keep message readable
        name  = lst.get("eventName") or lst.get("name") or lst.get("title") or "Bicep RAH"
        date  = lst.get("eventDate") or lst.get("date") or lst.get("startDate") or lst.get("date") or "Nov 2026"
        qty   = lst.get("quantity") or lst.get("ticketCount") or "?"
        price = lst.get("price") or lst.get("faceValue") or "?"
        eid   = lst.get("id") or lst.get("eventId") or ""
        url   = f"{TWICKETS_EVENT_URL};eventId={eid}" if eid else "https://www.twickets.live"

        lines.append(
            f"📅 {date}\n"
            f"🎟 {qty} ticket(s) · {price}\n"
            f"🔗 {url}\n"
        )

    if len(listings) > 5:
        lines.append(f"…and {len(listings) - 5} more listing(s). Check Twickets now!")

    lines.append("\n_Only buy via Twickets – the official RAH resale partner._")
    return "\n".join(lines)


def send_whatsapp(message: str) -> None:
    """Send a WhatsApp message via Twilio."""
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    msg = client.messages.create(
        body=message,
        from_=WHATSAPP_FROM,
        to=WHATSAPP_TO,
    )
    log.info("WhatsApp sent – SID: %s", msg.sid)


def run_once() -> None:
    """Single check cycle."""
    log.info("Checking Twickets…")
    listings = fetch_twickets_listings()
    log.info("Total listings returned: %d", len(listings))

    matches = [lst for lst in listings if is_target_event(lst)]
    log.info("Matching listings: %d", len(matches))

    if matches:
        message = build_alert_message(matches)
        log.info("Sending WhatsApp alert…")
        send_whatsapp(message)
    else:
        log.info("No tickets found yet. Next check in %ds.", POLL_INTERVAL)


def main() -> None:
    log.info("=== Bicep RAH Ticket Bot started ===")
    log.info("Polling every %d seconds. Press Ctrl+C to stop.", POLL_INTERVAL)
    while True:
        try:
            run_once()
        except Exception as exc:
            log.error("Unexpected error: %s", exc, exc_info=True)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    import sys
    if "--once" in sys.argv:
        run_once()          # single check – used by GitHub Actions
    else:
        main()              # continuous loop – used when self-hosting
