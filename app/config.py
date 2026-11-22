"""Every rule and limit in one place. Override any key in data/rules.json (or the file named
by PAYDESK_RULES) without touching code; this is where a changed requirement goes."""
import json
import os

DEFAULTS = {
    # limits
    "run_cost_limit_credits": 30,          # per invoice batch
    "vendor_check_limit_credits": 12,      # per vendor check
    "bank_cost_limit_credits": 5,          # per bank-statement batch
    "run_time_limit_s": 900,
    # payables rules
    "po_tolerance": 0.01,                  # invoice may differ from PO by 1%
    "lookalike_max_edit_distance": 3,      # sender domain this close to the vendor's = impersonation
    "two_person_threshold_inr": 100000,    # at or above this, a second, different person must approve
    # money
    "inr_per_credit": 25.2,                # 15,000 credits for $4,500 = $0.30; at ₹84/$
    "price_per_invoice_inr": 60,           # our price to the client (above the all-in first-batch cost)
    "price_per_new_vendor_check_inr": 250,
    # the client's operating account that payouts come out of. SAMPLE values: replace with the real
    # account (or connect RazorpayX, whose balance then applies). Labelled as sample on the page.
    "company_account": {"name": "Operating account", "bank": "HDFC Bank", "account_masked": "XXXXXXXX4321",
                        "ifsc": "HDFC0000123", "opening_balance_inr": 2500000, "opening_date": "2026-10-01",
                        "sample": True},
    # chart of accounts for bank-statement posting: account -> words in the payee's own description
    "chart_of_accounts": {
        "Staff meals": ["food", "restaurant", "meal", "dining", "grocery", "groceries"],
        "Cloud hosting": ["cloud", "hosting", "web services", "data center", "datacenter"],
        "Freight and logistics": ["logistics", "courier", "freight", "shipping", "supply chain", "express delivery"],
        "Telecom": ["telecom", "mobile network", "broadband", "telecommunication"],
        "Travel": ["airline", "travel", "hotel", "ride-hailing", "cab"],
        "Fuel": ["fuel", "petroleum", "petrol"],
        "Software subscriptions": ["software", "saas"],
    },
}

_cache = {"mtime": None, "data": DEFAULTS}


def _path():
    return os.environ.get("PAYDESK_RULES", os.path.join(os.path.dirname(__file__), "..", "data", "rules.json"))


def all_rules() -> dict:
    p = _path()
    try:
        m = os.path.getmtime(p)
    except OSError:
        return DEFAULTS
    if _cache["mtime"] != m:
        with open(p) as f:
            _cache.update(mtime=m, data={**DEFAULTS, **json.load(f)})
    return _cache["data"]


def get(key):
    return all_rules()[key]
