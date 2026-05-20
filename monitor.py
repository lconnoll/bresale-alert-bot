#!/usr/bin/env python3
"""
Bicep @ Royal Albert Hall – Twickets Resale Monitor
Sends a WhatsApp alert via Twilio when tickets appear on Twickets.
"""

import os
import time
import logging
import requests
from datetime import datetime
from twilio.rest import Client

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
TWICKETS_API = "https://www.twickets.live/services/catalogue"
TWICKETS_PARAMS = {
    "q":        "bicep",
    "regionId": "gb",
    "lang":     "en_GB",
}
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

# Global session to maintain cookies/state
session = requests.Session()


def fetch_twickets_listings() -> list[dict]:
    """Call the Twickets catalogue API and return raw listing dicts."""
    try:
        # First, visit the main page to get session cookies
        log.info("Initializing Twickets session...")
        session.get(
            "https://www.twickets.live",
            timeout=15,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
        )
        
        # Add delay to avoid rate limiting
        time.sleep(2)
        
        # Now make the actual API request with cookies
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.twickets.live/search/bicep?regionId=gb&lang=en_GB",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        
        log.info("Fetching Twickets listings from: %s", TWICKETS_API)
        resp = session.get(
            TWICKETS_API,
            params=TWICKETS_PARAMS,
            timeout=15,
            headers=headers,
        )
        
        log.info("Response status: %d", resp.status_code)
        log.debug("Response headers: %s", dict(resp.headers))
        
        resp.raise_for_status()
        data = resp.json()
        
        # Twickets wraps results under different keys depending on version
        listings = data.get("listings") or data.get("events") or data.get("results") or []
        log.info("Successfully retrieved %d listings", len(listings))
        return listings
        
    except requests.exceptions.HTTPError as exc:
        if exc.response.status_code == 403:
            log.error("403 Forbidden - Twickets is blocking automated requests")
            log.error("Response text: %s", exc.response.text[:500])
            log.error("Response headers: %s", dict(exc.response.headers))
            log.info("Potential solutions:")
            log.info("1. Try accessing https://www.twickets.live in a browser first")
            log.info("2. Twickets may require JavaScript/browser rendering (try Selenium)")
            log.info("3. IP may be blocked - try from different network")
            log.info("4. Check if Twickets requires authentication")
        else:
            log.error("HTTP Error %d: %s", exc.response.status_code, exc)
        return []
    except requests.RequestException as exc:
        log.error("Request failed: %s", exc)
        return []
    except ValueError as exc:
        log.error("JSON parse error: %s", exc)
        return []


def is_target_event(listing: dict) -> bool:
    """Return True if this listing looks like Bicep @ RAH in Nov 2026."""
    # Flatten all text fields for easy searching
    blob = " ".join([
        str(listing.get("eventName", "")),
        str(listing.get("name", "")),
        str(listing.get("venueName", "")),
        str(listing.get("venue", "")),
        str(listing.get("title", "")),
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
        date  = lst.get("eventDate") or lst.get("date") or lst.get("startDate") or "Nov 2026"
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
