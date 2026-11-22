"""Payment providers. The idempotency key is created and committed in SQLite
BEFORE the provider is called; on restart the same key is reused, so the
provider returns the first payout instead of creating a second one."""
import os

import httpx


class ProviderNotConfigured(Exception):
    pass


class RazorpayXTest:
    """RazorpayX composite payout (test mode keys only: rzp_test_...)."""

    name = "razorpayx_test"

    def __init__(self):
        self.key_id = os.environ.get("RAZORPAY_KEY_ID", "")
        self.key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
        self.account_number = os.environ.get("RAZORPAYX_ACCOUNT_NUMBER", "")
        if not (self.key_id and self.key_secret and self.account_number):
            raise ProviderNotConfigured("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET / RAZORPAYX_ACCOUNT_NUMBER not set")
        if not self.key_id.startswith("rzp_test_"):
            raise ProviderNotConfigured("refusing to run with a non-test Razorpay key")

    def pay(self, idempotency_key: str, amount_paise: int, payee_name: str, account: dict, reference: str) -> str:
        body = {
            "account_number": self.account_number,
            "amount": amount_paise,
            "currency": "INR",
            "mode": "IMPS",
            "purpose": "vendor bill",
            "queue_if_low_balance": True,
            "reference_id": reference[:40],
            "fund_account": {
                "account_type": "bank_account",
                "bank_account": {
                    "name": payee_name[:120],
                    "ifsc": account["ifsc"],
                    "account_number": str(account["account_no"]),
                },
                "contact": {"name": payee_name[:120], "type": "vendor", "reference_id": reference[:40]},
            },
        }
        r = httpx.post(
            "https://api.razorpay.com/v1/payouts",
            json=body,
            auth=(self.key_id, self.key_secret),
            headers={"X-Payout-Idempotency": idempotency_key},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["id"]


def get_provider():
    return RazorpayXTest()
