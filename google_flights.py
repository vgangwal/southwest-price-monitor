"""
Google Flights scraper — fetches Southwest Airlines dollar prices.

Navigates directly to the GF search-results URL using a protobuf-encoded
`tfs` parameter (no form filling, no fragile selectors).

Public API:
    flights = await scrape_google_flights(origin, dest, date)
    # Returns a list of dicts:
    #   flight_number : str | None
    #   depart_time   : str   e.g. "11:15 AM"
    #   arrive_time   : str   e.g. "12:40 PM"
    #   stops         : str   e.g. "Nonstop" / "1 stop(s)"
    #   price_usd     : float | None
"""

import asyncio
import base64
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

SCREENSHOTS_DIR = Path("screenshots")
SCREENSHOTS_DIR.mkdir(exist_ok=True)

VIEWPORT = {"width": 1440, "height": 900}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# URL builder
# ---------------------------------------------------------------------------

def _build_tfs(origin: str, dest: str, date: str) -> str:
    """
    Encode a one-way flight search into the Google Flights `tfs` protobuf
    URL parameter.

    Protobuf layout (verified by decoding live GF URLs):
      outer {
        field 1  (varint)  : 28          ← one-way trip marker
        field 2  (varint)  : 2           ← 1 adult passenger
        field 3  (bytes)   : leg {
          field 2  (bytes) : "YYYY-MM-DD"
          field 13 (bytes) : airport { field 1: 1, field 2: "LAX" }
          field 14 (bytes) : airport { field 1: 1, field 2: "PHX" }
        }
      }
    """
    date_b = date.encode()  # always 10 bytes for YYYY-MM-DD

    def airport(code: str) -> bytes:
        b = code.encode()
        return b"\x08\x01\x12" + bytes([len(b)]) + b

    o = airport(origin)
    d = airport(dest)
    leg = (
        b"\x12" + bytes([len(date_b)]) + date_b   # field 2: date
        + b"\x6a" + bytes([len(o)]) + o            # field 13: origin
        + b"\x72" + bytes([len(d)]) + d            # field 14: destination
    )
    outer = b"\x08\x1c\x10\x02\x1a" + bytes([len(leg)]) + leg
    return base64.b64encode(outer).decode()


def _build_url(origin: str, dest: str, date: str) -> str:
    tfs = _build_tfs(origin, dest, date)
    return (
        "https://www.google.com/travel/flights/search"
        f"?tfs={tfs}&hl=en&gl=us&curr=USD"
    )


def _ss(label: str, tag: str) -> Path:
    ts = datetime.now().strftime("%H%M%S")
    return SCREENSHOTS_DIR / f"gf_{label}_{ts}_{tag}.png"


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------

async def scrape_google_flights(
    origin: str, dest: str, date: str, label: str = "leg"
) -> List[Dict]:
    """
    Open the GF search-results page for origin→dest on date and return
    all Southwest Airlines flights found.  Each dict has:
        flight_number, depart_time, arrive_time, stops, price_usd
    """
    url = _build_url(origin, dest, date)
    print(f"  [GF] navigating to search results...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--window-size=1440,900",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport=VIEWPORT,
            locale="en-US",
            timezone_id="America/Los_Angeles",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(2)
            await _dismiss_dialogs(page)
            await asyncio.sleep(1)

            await page.screenshot(path=str(_ss(label, "loaded")))

            loaded = await _wait_for_flights(page, timeout=20)
            if not loaded:
                print("  [GF] Warning: page loaded but flight cards not detected")

            await page.screenshot(path=str(_ss(label, "results")), full_page=True)

            flights = await _parse_southwest(page)
            return flights

        except Exception as exc:
            err_path = _ss(label, "error")
            try:
                await page.screenshot(path=str(err_path), full_page=True)
            except Exception:
                pass
            print(f"  [GF] Error: {exc}  (screenshot → {err_path})")
            return []
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _dismiss_dialogs(page) -> None:
    for text in ["Accept all", "Reject all", "I agree", "Accept", "Agree"]:
        try:
            btn = page.get_by_role("button", name=text, exact=True)
            if await btn.is_visible(timeout=1_500):
                await btn.click()
                await asyncio.sleep(0.5)
                return
        except Exception:
            pass


async def _wait_for_flights(page, timeout: int = 20) -> bool:
    """Wait for flight list items to appear."""
    selectors = [
        "li[aria-label]",
        "ul[aria-label*='flight']",
        "ul[aria-label*='Flight']",
        "[jsname='IWWDBc']",
        "div[jscontroller] ul li",
    ]
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for sel in selectors:
            try:
                await page.wait_for_selector(sel, timeout=2_000)
                return True
            except PlaywrightTimeoutError:
                pass
        await asyncio.sleep(1)
    return False


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

async def _parse_southwest(page) -> List[Dict]:
    """Extract Southwest-only flights from the results page."""
    flights: List[Dict] = []

    # Strategy 1: aria-labeled <li> elements (most structured)
    for item in await page.query_selector_all("li[aria-label]"):
        aria = (await item.get_attribute("aria-label")) or ""
        if "southwest" in aria.lower():
            f = _parse_text(aria)
            if f:
                flights.append(f)

    if flights:
        return _dedup(flights)

    # Strategy 2: any <li> whose inner text mentions Southwest + a price
    for item in await page.query_selector_all("li"):
        try:
            text = await item.inner_text()
            if len(text) > 30 and "southwest" in text.lower() and "$" in text:
                f = _parse_text(text)
                if f:
                    flights.append(f)
        except Exception:
            pass

    if flights:
        return _dedup(flights)

    # Strategy 3: full page-text scan
    body = await page.evaluate("() => document.body.innerText")
    for i, line in enumerate(body.split("\n")):
        if "southwest" in line.lower():
            chunk = "\n".join(body.split("\n")[max(0, i - 3): i + 8])
            if "$" in chunk:
                f = _parse_text(chunk)
                if f and f.get("price_usd") is not None:
                    flights.append(f)

    return _dedup(flights)


def _parse_text(text: str) -> Optional[Dict]:
    """Parse a single flight's text (aria-label or innerText) into a dict."""
    # Times: "5:30 AM" or "11:15 PM"
    times = re.findall(r"\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b", text, re.IGNORECASE)
    depart = times[0].strip() if len(times) > 0 else None
    arrive = times[1].strip() if len(times) > 1 else None

    # Stops
    if re.search(r"\bnonstop\b", text, re.IGNORECASE):
        stops = "Nonstop"
    elif m := re.search(r"(\d+)\s*stops?", text, re.IGNORECASE):
        n = int(m.group(1))
        stops = "Nonstop" if n == 0 else f"{n} stop(s)"
    else:
        stops = None

    # Dollar price
    price_usd: Optional[float] = None
    if pm := re.search(r"\$\s*([\d,]+(?:\.\d+)?)", text):
        try:
            price_usd = float(pm.group(1).replace(",", ""))
        except ValueError:
            pass

    # Flight number — GF may include "WN 2416" or "Flight WN 2416"
    fn_m = re.search(
        r"(?:WN|(?:Flight\s+(?:WN\s+)?))\s*#?(\d{3,4})\b", text, re.IGNORECASE
    )
    flight_number = fn_m.group(1) if fn_m else None

    # Require at least a departure time — entries without it are price banners,
    # not real flight rows.
    if depart is None:
        return None

    return {
        "flight_number": flight_number,
        "depart_time": depart or "N/A",
        "arrive_time": arrive or "N/A",
        "stops": stops or "N/A",
        "price_usd": price_usd,
    }


def _dedup(flights: List[Dict]) -> List[Dict]:
    seen: set = set()
    out: List[Dict] = []
    for f in flights:
        key = (f["depart_time"], f["arrive_time"])
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out
