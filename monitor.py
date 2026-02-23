"""
Southwest price monitor — checks every hour, emails on price drops.

Setup
-----
1. Edit flights.json with the routes and flights you want to watch.
2. Ensure .env exists with your Gmail credentials.
3. Start the monitor:

   python3 monitor.py            # foreground
   nohup python3 monitor.py &    # background, survives closing terminal
   tail -f monitor.log           # check background logs

flights.json format
-------------------
[
  {
    "from": "LAX",
    "to":   "PHX",
    "date": "2026-03-26",
    "flights": [
      {"number": "2416", "departs": "11:15 AM"},
      {"number": "1571", "departs": "4:05 PM"}
    ]
  }
]

  "departs" must match the departure time shown on Google Flights
  (e.g. "8:20 AM", "4:05 PM"). The monitor only tracks and alerts
  on the exact flights you list — nothing else.
"""

import asyncio
import json
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from google_flights import scrape_google_flights

FLIGHTS_FILE   = Path("flights.json")
ENV_FILE       = Path(".env")
CHECK_INTERVAL = 3600  # seconds

# On Railway set DATA_DIR=/data and mount a volume there so price history
# survives deploys. Locally defaults to the current directory.
_DATA_DIR    = Path(os.environ.get("DATA_DIR", "."))
HISTORY_FILE = _DATA_DIR / "price_history.json"


# ---------------------------------------------------------------------------
# Config / persistence
# ---------------------------------------------------------------------------

def _load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


def _load_flights() -> List[Dict]:
    if not FLIGHTS_FILE.exists():
        raise FileNotFoundError(f"{FLIGHTS_FILE} not found.")
    return json.loads(FLIGHTS_FILE.read_text())


def _load_history() -> Dict:
    return json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else {}


def _save_history(h: Dict) -> None:
    HISTORY_FILE.write_text(json.dumps(h, indent=2))


# ---------------------------------------------------------------------------
# Time normalization
# ---------------------------------------------------------------------------

def _norm_time(t: str) -> str:
    """
    Normalize a time string to '%-I:%M %p' for reliable comparison.
    Handles '11:15 AM', '11:15AM', '11:15 am', '13:15', etc.
    Returns the original string on parse failure.
    """
    t = t.strip()
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            return datetime.strptime(t.upper(), fmt).strftime("%-I:%M %p")
        except ValueError:
            pass
    return t


def _fmt_date(d: str) -> str:
    return datetime.strptime(d, "%Y-%m-%d").strftime("%b %-d, %Y")


def _leg_key(origin: str, dest: str, date: str) -> str:
    return f"{origin}-{dest}-{date}"


def _flight_key(depart: str, arrive: str) -> str:
    return f"{depart}|{arrive}"


# ---------------------------------------------------------------------------
# Price checking
# ---------------------------------------------------------------------------

async def _check_all(
    watchlist: List[Dict], history: Dict
) -> Tuple[List[Dict], Dict]:
    drops: List[Dict] = []

    for leg in watchlist:
        origin = leg["from"].upper().strip()
        dest   = leg["to"].upper().strip()
        date   = leg["date"].strip()
        lk     = _leg_key(origin, dest, date)

        # Build a map: normalized_depart_time → flight number
        # Skip entries where departs is still a placeholder
        target_map: Dict[str, str] = {}
        skipped: List[str] = []
        for f in leg.get("flights", []):
            departs = f.get("departs", "").strip()
            number  = str(f.get("number", "?"))
            if not departs or departs.upper() == "FILL_IN":
                skipped.append(f"#{number}")
                continue
            target_map[_norm_time(departs)] = number

        if skipped:
            print(f"  Skipping {', '.join(skipped)} — departure time not set in flights.json")

        if not target_map:
            print(f"  {origin} → {dest}: no flights with departure times — skipping.")
            continue

        print(f"  Checking {origin} → {dest}  {_fmt_date(date)}...")
        print(f"    Watching: {', '.join(f'#{n} ({t})' for t, n in target_map.items())}")

        sw_flights = await scrape_google_flights(
            origin, dest, date, label=f"{origin}-{dest}"
        )

        if not sw_flights:
            print(f"    No Southwest flights found on Google Flights.")
            continue

        if lk not in history:
            history[lk] = {"prices": {}}

        for f in sw_flights:
            norm_dep = _norm_time(f["depart_time"])

            # Only process flights the user is watching
            if norm_dep not in target_map:
                continue

            flight_number = target_map[norm_dep]
            price = f.get("price_usd")
            if price is None:
                continue

            fk   = _flight_key(f["depart_time"], f["arrive_time"])
            prev = history[lk]["prices"].get(fk)

            if prev is not None:
                old = prev["price"]
                if price < old:
                    drop = old - price
                    print(f"    ↓ DROP  #{flight_number} {f['depart_time']}  ${old:.0f} → ${price:.0f}  (−${drop:.0f})")
                    drops.append({
                        "origin":        origin,
                        "dest":          dest,
                        "date":          date,
                        "flight_number": flight_number,
                        "depart_time":   f["depart_time"],
                        "arrive_time":   f["arrive_time"],
                        "stops":         f["stops"],
                        "old_price":     old,
                        "new_price":     price,
                    })
                else:
                    arrow = "=" if price == old else "↑"
                    print(f"    {arrow}  #{flight_number} {f['depart_time']}  ${price:.0f}")
            else:
                print(f"    ·  #{flight_number} {f['depart_time']}  ${price:.0f}  (baseline)")

            history[lk]["prices"][fk] = {
                "price":         price,
                "flight_number": flight_number,
                "updated":       datetime.now().isoformat(),
            }

    return drops, history


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _send_email(
    drops: List[Dict], gmail_user: str, gmail_pass: str, to_email: str
) -> None:
    n = len(drops)
    subject = f"✈ Southwest price drop — {n} flight{'s' if n > 1 else ''} cheaper"

    lines = ["Southwest prices dropped since the last check:\n"]
    for d in drops:
        drop_amt = d["old_price"] - d["new_price"]
        lines.append(
            f"{'─' * 48}\n"
            f"Flight #{d['flight_number']}   "
            f"{d['origin']} → {d['dest']}   {_fmt_date(d['date'])}\n"
            f"{d['depart_time']} → {d['arrive_time']}   {d['stops']}\n"
            f"${d['old_price']:.0f}  →  ${d['new_price']:.0f}   (↓ ${drop_amt:.0f})\n"
        )
    lines += ["─" * 48, "Southwest Price Monitor"]

    msg = MIMEMultipart()
    msg["From"]    = gmail_user
    msg["To"]      = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText("\n".join(lines), "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_user, gmail_pass)
        smtp.sendmail(gmail_user, to_email, msg.as_string())

    print(f"  ✉  Email sent to {to_email}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def _run_once() -> None:
    _load_env()

    gmail_user  = os.environ.get("GMAIL_USER", "").strip()
    gmail_pass  = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    alert_email = os.environ.get("ALERT_EMAIL", "").strip()

    if not gmail_user or not gmail_pass or not alert_email:
        raise ValueError("GMAIL_USER, GMAIL_APP_PASSWORD, ALERT_EMAIL must be set in .env")

    watchlist = _load_flights()
    history   = _load_history()

    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]  Checking {len(watchlist)} route(s)...")

    drops, history = await _check_all(watchlist, history)
    _save_history(history)

    if drops:
        print(f"  {len(drops)} price drop(s) — sending email...")
        _send_email(drops, gmail_user, gmail_pass, alert_email)
    else:
        print("  No price drops — no email sent.")


async def main() -> None:
    while True:
        try:
            await _run_once()
        except Exception as exc:
            print(f"  ERROR: {exc}")
        print(f"  Next check in 1 hour.\n")
        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
