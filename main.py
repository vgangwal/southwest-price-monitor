#!/usr/bin/env python3
"""
Southwest flight price checker via Google Flights.

Looks up specific Southwest flights on Google Flights and shows the dollar
price. Flight numbers are matched when Google Flights includes them; if GF
doesn't show flight numbers, all Southwest flights found are displayed so
you can match by departure time.

──────────────────────────────────────────────────────────────────────────
USAGE
──────────────────────────────────────────────────────────────────────────

  One-way — single flight:
    python3 main.py --from LAX --to PHX --date 2026-03-26 --flights 2416

  One-way — multiple flights on the same leg:
    python3 main.py --from LAX --to PHX --date 2026-03-26 --flights 2416 1571 2008

  Round trip — outbound and return in one command:
    python3 main.py --type roundtrip \\
      --from LAX --to PHX \\
      --date 2026-03-26 --flights 2416 1571 \\
      --return-date 2026-03-29 --return-flights 1218 3688 2658

NOTES
  • AIRPORT  use 3-letter IATA codes  (LAX, PHX, ORD, DEN, …)
  • DATE     use YYYY-MM-DD format    (2026-03-26)
  • FLIGHTS  plain flight numbers, no "WN" prefix needed  (2416 not WN2416)
──────────────────────────────────────────────────────────────────────────
"""

import argparse
import asyncio
import sys
from datetime import datetime
from typing import Dict, List, Optional

from tabulate import tabulate

from google_flights import scrape_google_flights

BAR = "─" * 62


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main.py",
        description="Check Southwest flight prices on Google Flights.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  One-way, one flight:
    python3 main.py --from LAX --to PHX --date 2026-03-26 --flights 2416

  One-way, multiple flights:
    python3 main.py --from LAX --to PHX --date 2026-03-26 --flights 2416 1571 2008

  Round trip:
    python3 main.py --type roundtrip \\
      --from LAX --to PHX \\
      --date 2026-03-26 --flights 2416 1571 \\
      --return-date 2026-03-29 --return-flights 1218 3688
""",
    )

    p.add_argument(
        "--from", dest="origin", required=True, metavar="AIRPORT",
        help="Origin airport code (e.g. LAX)",
    )
    p.add_argument(
        "--to", dest="dest", required=True, metavar="AIRPORT",
        help="Destination airport code (e.g. PHX)",
    )
    p.add_argument(
        "--date", required=True, metavar="YYYY-MM-DD",
        help="Departure date",
    )
    p.add_argument(
        "--flights", nargs="+", required=True, metavar="NUM",
        help="Outbound flight number(s) to look for (e.g. 2416 1571 2008)",
    )
    p.add_argument(
        "--type", dest="trip_type", choices=["oneway", "roundtrip"],
        default="oneway", metavar="TYPE",
        help="Trip type: oneway (default) or roundtrip",
    )
    p.add_argument(
        "--return-date", dest="return_date", metavar="YYYY-MM-DD",
        help="Return departure date (required for --type roundtrip)",
    )
    p.add_argument(
        "--return-flights", dest="return_flights", nargs="+", metavar="NUM",
        help="Return leg flight number(s) (required for --type roundtrip)",
    )
    return p


def _validate(args) -> Optional[str]:
    for flag, val in [("--date", args.date), ("--return-date", args.return_date)]:
        if val:
            try:
                datetime.strptime(val, "%Y-%m-%d")
            except ValueError:
                return f"{flag} must be YYYY-MM-DD, got: {val!r}"
    if args.trip_type == "roundtrip":
        if not args.return_date:
            return "--return-date is required when --type is roundtrip"
        if not args.return_flights:
            return "--return-flights is required when --type is roundtrip"
    return None


def _normalize_fn(fn: str) -> str:
    """Strip optional 'WN' prefix so '2416' and 'WN2416' both work."""
    fn = fn.strip()
    if fn.upper().startswith("WN"):
        fn = fn[2:].lstrip()
    return fn


# ---------------------------------------------------------------------------
# Core: look up one leg, print results
# ---------------------------------------------------------------------------

async def check_leg(
    origin: str,
    dest: str,
    date: str,
    target_fns: List[str],
    label: str,
) -> None:
    date_pretty = datetime.strptime(date, "%Y-%m-%d").strftime("%b %-d, %Y")
    targets = [_normalize_fn(fn) for fn in target_fns]
    targets_display = "  ".join(f"#{fn}" for fn in targets)

    print(f"\n{BAR}")
    print(f"  {label}:  {origin} → {dest}   {date_pretty}")
    print(f"  Flights requested: {targets_display}")
    print(BAR)

    sw_flights = await scrape_google_flights(origin, dest, date, label=label.lower())

    # ── Case 1: Southwest not found at all ──────────────────────────────
    if not sw_flights:
        print(
            "\n  Southwest Airlines not found on Google Flights for this route.\n"
        )
        for fn in targets:
            print(f"  ✗  #{fn} — not found")
        return

    # ── Case 2: GF found SW flights but without flight numbers ──────────
    has_numbers = any(f["flight_number"] is not None for f in sw_flights)
    if not has_numbers:
        print(
            "\n  Note: Google Flights does not show Southwest flight numbers "
            "for this route.\n"
            "  Showing all Southwest flights found — match to your flight(s) "
            "by departure time:\n"
        )
        rows = [
            (
                f["depart_time"],
                f["arrive_time"],
                f["stops"],
                f"${f['price_usd']:.0f}" if f["price_usd"] is not None else "N/A",
            )
            for f in sw_flights
        ]
        print(
            tabulate(rows, headers=["Departs", "Arrives", "Stops", "Price (USD)"],
                     tablefmt="simple")
        )
        print(f"\n  ({len(sw_flights)} Southwest flight(s) found total)")
        return

    # ── Case 3: GF has flight numbers — match exactly ───────────────────
    rows = []
    for fn in targets:
        match = next(
            (f for f in sw_flights if str(f["flight_number"]) == fn), None
        )
        if match:
            price = (
                f"${match['price_usd']:.0f}"
                if match["price_usd"] is not None
                else "N/A"
            )
            rows.append([
                f"✓  #{fn}",
                match["depart_time"],
                match["arrive_time"],
                match["stops"],
                price,
            ])
        else:
            rows.append([f"✗  #{fn}", "—", "—", "—", "not found on Google Flights"])

    print()
    print(
        tabulate(
            rows,
            headers=["Flight", "Departs", "Arrives", "Stops", "Price (USD)"],
            tablefmt="simple",
        )
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run(args) -> None:
    await check_leg(
        origin=args.origin.upper(),
        dest=args.dest.upper(),
        date=args.date,
        target_fns=args.flights,
        label="Outbound",
    )
    if args.trip_type == "roundtrip":
        await check_leg(
            origin=args.dest.upper(),
            dest=args.origin.upper(),
            date=args.return_date,
            target_fns=args.return_flights,
            label="Return",
        )
    print(f"\n  Screenshots saved to ./screenshots/\n")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    err = _validate(args)
    if err:
        print(f"error: {err}", file=sys.stderr)
        parser.print_usage(sys.stderr)
        sys.exit(1)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
