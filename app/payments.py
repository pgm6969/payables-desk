"""Payment provider: RazorpayX Payouts, TEST MODE ONLY.

Exactly-once:
  1. the idempotency key is created and committed in SQLite BEFORE the provider is called;
  2. the call carries it as X-Payout-Idempotency;
  3. after a crash or timeout the SAME key is sent again, so RazorpayX treats it as a retry
     of the same payout, not a new one.

Outcomes of a create call:
  - accepted  -> returns the payout (id, status: queued / processing / processed ...)
  - rejected  -> PayoutRejected (4xx: bad IFSC, invalid account...). Final; a person decides.
  - unknown   -> PayoutUnknown (timeout / 5xx). The payout may or may not exist; keep the
                 key, retry later with the same key. Never create a new key for it.
"""
import os
import re

import httpx

BASE = "https://api.razorpay.com/v1"
FINAL_OK = {"processed"}
FINAL_BAD = {"failed", "reversed", "cancelled", "rejected"}
IN_FLIGHT = {"queued", "pending", "processing"}


class ProviderNotConfigured(Exception):
    pass


class PayoutRejected(Exception):
    """The provider refused the payout (4xx). Nothing was paid."""


class PayoutUnknown(Exception):
    """Timeout or 5xx: outcome unknown. Retry with the same idempotency key."""


def _clean(s: str, allowed: str, lo: int, hi: int, fallback: str) -> str:
    s = re.sub(allowed, " ", s or "")
    s = re.sub(r"\s+", " ", s).strip()[:hi]
    return s if len(s) >= lo else fallback


def bank_name(s):      # 4-120 chars: letters digits space ’ - _ / ( ) .
    return _clean(s, r"[^A-Za-z0-9 ’\-_/().]", 4, 120, "Vendor account")


def contact_name(s):   # 3-50 chars, same set
    return _clean(s, r"[^A-Za-z0-9 ’\-_/().]", 3, 50, "Vendor")


def narration(s):      # max 30 chars, letters digits space
    return _clean(s, r"[^A-Za-z0-9 ]", 1, 30, "Vendor bill")


def mode_for(amount_paise: int) -> str:
    return "IMPS" if amount_paise <= 5_00_000_00 else "NEFT"  # IMPS up to ₹5 lakh


class RazorpayXTest:
    name = "razorpayx_test"

    def __init__(self, client: httpx.Client | None = None):
        self.key_id = os.environ.get("RAZORPAY_KEY_ID", "").strip()
        self.key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
        self.account_number = os.environ.get("RAZORPAYX_ACCOUNT_NUMBER", "").strip()
        if not (self.key_id and self.key_secret and self.account_number):
            raise ProviderNotConfigured("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET / RAZORPAYX_ACCOUNT_NUMBER not set")
        if self.key_id.startswith("rzp_live_"):
            raise ProviderNotConfigured("refusing to run with a LIVE Razorpay key: test mode only")
        self.http = client or httpx.Client(timeout=30)

    def _req(self, method, path, **kw):
        try:
            r = self.http.request(method, BASE + path, auth=(self.key_id, self.key_secret), **kw)
        except httpx.TransportError as e:
            raise PayoutUnknown(f"no answer from RazorpayX ({e.__class__.__name__})")
        if r.status_code >= 500:
            raise PayoutUnknown(f"RazorpayX {r.status_code}")
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
                msg = f"{err.get('code')}: {err.get('description')}" + (f" (field {err['field']})" if err.get("field") else "")
            except ValueError:
                msg = r.text[:200]
            raise PayoutRejected(f"RazorpayX {r.status_code} {msg}")
        return r.json()

    def pay(self, idempotency_key: str, amount_paise: int, payee_name: str, account: dict, reference: str,
            contact: dict | None = None, memo: str = "") -> dict:
        contact = contact or {}
        c = {"name": contact_name(payee_name), "type": "vendor", "reference_id": reference[:40]}
        if contact.get("email"):
            c["email"] = contact["email"]
        if contact.get("phone"):
            c["contact"] = re.sub(r"[^\d+]", "", contact["phone"])
        body = {
            "account_number": self.account_number,
            "amount": int(amount_paise),
            "currency": "INR",
            "mode": mode_for(amount_paise),
            "purpose": "vendor bill",
            "queue_if_low_balance": True,
            "reference_id": reference[:40],
            "narration": narration(memo or "Vendor bill"),
            "fund_account": {
                "account_type": "bank_account",
                "bank_account": {"name": bank_name(payee_name), "ifsc": str(account["ifsc"]).upper(),
                                 "account_number": re.sub(r"\s", "", str(account["account_no"]))},
                "contact": c,
            },
        }
        p = self._req("POST", "/payouts", json=body, headers={"X-Payout-Idempotency": idempotency_key})
        return {"id": p.get("id"), "status": p.get("status"), "utr": p.get("utr"),
                "detail": (p.get("status_details") or {}).get("description") or (p.get("error") or {}).get("description")}

    def fetch(self, payout_id: str) -> dict:
        p = self._req("GET", f"/payouts/{payout_id}")
        return {"id": p.get("id"), "status": p.get("status"), "utr": p.get("utr"),
                "detail": (p.get("status_details") or {}).get("description") or (p.get("error") or {}).get("description"),
                "reason": (p.get("status_details") or {}).get("reason")}


def get_provider():
    return RazorpayXTest()
