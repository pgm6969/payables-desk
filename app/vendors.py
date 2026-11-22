"""Vendor records come from the Trust & Compliance service (the other half of
this project). This module only READS them. Contract: docs/vendor_contract.md.

Source of records, in order:
  1. TRUST_API_URL  -> GET {url}/vendors   (JSON list)
  2. VENDORS_FILE   -> path to a JSON list (default data/vendors.json)
"""
import json
import os
import re

import httpx

DEFAULT_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "vendors.json")


def norm_name(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\b(private|pvt|limited|ltd|llp|inc|co|company|the)\b\.?", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def norm_gstin(s: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", (s or "").upper())


def norm_domain(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"^https?://", "", s)
    s = s.split("/")[0]
    return s[4:] if s.startswith("www.") else s


def load_vendors() -> list[dict]:
    url = os.environ.get("TRUST_API_URL")
    if url:
        r = httpx.get(url.rstrip("/") + "/vendors", timeout=15)
        r.raise_for_status()
        return r.json()
    path = os.environ.get("VENDORS_FILE", DEFAULT_FILE)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def find_vendor(invoice: dict, vendors: list[dict]):
    """Match an invoice to a vendor record. Returns (vendor, how) or (None, why).
    GSTIN is the strongest key; a name-only match is never enough to pay."""
    g = norm_gstin(invoice.get("vendor_gstin"))
    if g:
        for v in vendors:
            if norm_gstin(v.get("gstin")) == g:
                return v, "gstin"
    d = norm_domain(invoice.get("vendor_domain") or "")
    if d:
        for v in vendors:
            if norm_domain(v.get("domain") or "") == d:
                return v, "domain"
    n = norm_name(invoice.get("vendor_name"))
    named = [v for v in vendors if n and norm_name(v.get("legal_name")) == n]
    if len(named) == 1:
        return named[0], "name_only"
    if len(named) > 1:
        return None, "several vendor records share this name"
    return None, "no vendor record matches this GSTIN, domain or name"
