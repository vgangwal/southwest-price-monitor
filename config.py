ROUTES = [
    {
        "label": "outbound",
        "origin": "LAX",
        "destination": "PHX",
        "date": "2026-03-26",  # YYYY-MM-DD format
        "target_flights": ["2416", "1571", "2008"],
    },
    {
        "label": "return",
        "origin": "PHX",
        "destination": "LAX",
        "date": "2026-03-29",
        "target_flights": ["1218", "3688", "2658"],
    },
]

PRICE_THRESHOLD = 10000  # points per leg
