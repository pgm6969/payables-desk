"""Local preview data. TEST DATA ONLY: sample vendor records and invoices so the
page has something to show before the Trust service and the real inbox are
connected. Uses its own database (data/demo.db). The page shows a "test data"
banner whenever PAYDESK_DEMO=1. Crustdata lookups are real (live API).

    PAYDESK_DB=data/demo.db .venv/bin/python scripts/seed_demo.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("PAYDESK_DB", os.path.join(os.path.dirname(__file__), "..", "data", "demo.db"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app import pipeline  # noqa: E402
from app.db import connect  # noqa: E402

# Sample vendor records in the Trust & Compliance contract format (test data).
VENDORS = [
    # No "status": the desk runs its own live vendor check (5.1 + 5.2) on each vendor.
    {"vendor_id": "V-BD", "legal_name": "Blue Dart Express Limited", "gstin": "27AAACB0446L1ZS", "domain": "bluedart.com",
     "director": "Balfour Manuel", "contact": {"name": "AP desk", "email": "ap-test@bluedart.example", "phone": "+91-22-0000-0000"},
     "verified_bank_account": {"ifsc": "HDFC0000001", "account_no": "50100012345678"}},
    {"vendor_id": "V-DL", "legal_name": "Delhivery Limited", "gstin": "06AAPCS9575E1ZR", "domain": "delhivery.com",
     "contact": {"name": "Vendor desk", "email": "vendors-test@delhivery.example", "phone": "+91-124-000-0000"},
     "verified_bank_account": {"ifsc": "ICIC0000104", "account_no": "010405001234"}},
    {"vendor_id": "V-SX", "legal_name": "Safexpress Private Limited", "gstin": "07AAECS4363H1ZA", "domain": "safexpress.com",
     "verified_bank_account": {"ifsc": "SBIN0000691", "account_no": "30987654321"}},
    {"vendor_id": "V-SC", "legal_name": "Shreeji Cargo Movers Private Limited", "gstin": "", "domain": "shreejicargomovers.in",
     "verified_bank_account": {"ifsc": "PUNB0000001", "account_no": "112233445566"}},
]

REQUEST = {
    "invoices": [
        {"invoice_no": "BD/2026/0042", "vendor_name": "Blue Dart Express Ltd", "vendor_gstin": "27AAACB0446L1ZS",
         "vendor_domain": "bluedart.com", "amount": "₹18,500.00", "invoice_date": "2026-10-07", "po_number": "PO-1001",
         "_message": "sample-01.eml"},
        {"invoice_no": "bd 2026 42", "vendor_name": "BLUE DART EXPRESS LIMITED", "vendor_gstin": "27AAACB0446L1ZS",
         "vendor_domain": "bluedart.com", "amount": "18500", "invoice_date": "07/10/2026", "po_number": "PO-1001",
         "_message": "sample-02.pdf"},
        {"invoice_no": "DLV-88231", "vendor_name": "Delhivery Limited", "vendor_gstin": "06AAPCS9575E1ZR",
         "vendor_domain": "delhivery.com", "amount": "₹42,300", "invoice_date": "2026-10-12", "po_number": "PO-1002",
         "_message": "sample-03.eml"},
        {"invoice_no": "SFX/7781", "vendor_name": "Safexpress Pvt Ltd", "vendor_gstin": "07AAECS4363H1ZA",
         "vendor_domain": "safexpress.com", "amount": "₹61,750", "invoice_date": "2026-10-14", "po_number": "PO-1003",
         "_message": "sample-04.eml"},
        {"invoice_no": "INV-5521", "vendor_name": "Blue Dart Express", "vendor_gstin": "",
         "vendor_domain": "bluedart-logistics.in", "amount": "₹9,800", "invoice_date": "2026-10-15", "po_number": "PO-1001",
         "_message": "sample-05.eml"},
        {"invoice_no": "SCM/118", "vendor_name": "Shreeji Cargo Movers Pvt Ltd", "vendor_gstin": "",
         "vendor_domain": "shreejicargomovers.in", "amount": "₹27,400", "invoice_date": "2026-10-16", "po_number": "PO-1004",
         "_message": "sample-08.txt"},
        {"invoice_no": "BD/2026/0057", "vendor_name": "Blue Dart Express Ltd", "vendor_gstin": "27AAACB0446L1ZS",
         "vendor_domain": "bluedartt.com", "amount": "₹12,250", "invoice_date": "2026-10-17", "po_number": "PO-1005",
         "_message": "sample-09.eml"},
        {"invoice_no": "", "vendor_name": "PAYTM*SHARMA TRD", "vendor_gstin": "", "vendor_domain": "",
         "amount": "₹4,000", "invoice_date": "2026-10-09", "po_number": "", "_message": "sample-06.txt"},
    ],
    "bank_change_requests": [
        {"vendor_name": "Delhivery Limited", "vendor_gstin": "06AAPCS9575E1ZR", "vendor_domain": "",
         "new_ifsc": "YESB0000999", "new_account_no": "999988887777", "message": "sample-07.eml",
         "from": "Delhivery Accounts <accounts@delhivery-payments.co>", "date": "2026-10-13"},
    ],
    "purchase_orders": [
        {"po_number": "PO-1001", "amount": 18500}, {"po_number": "PO-1002", "amount": 42300},
        {"po_number": "PO-1003", "amount": 55000}, {"po_number": "PO-1004", "amount": 27400},
        {"po_number": "PO-1005", "amount": 12250},
    ],
    "limits": {"cost_credits": 30, "time_s": 900},
}


def main():
    path = os.environ["PAYDESK_DB"]
    vfile = os.path.join(os.path.dirname(path), "demo_vendors.json")
    with open(vfile, "w") as f:
        json.dump(VENDORS, f, indent=2)
    os.environ["VENDORS_FILE"] = vfile
    conn = connect()
    run_id = pipeline.submit(REQUEST, conn)
    out = pipeline.process(run_id, conn, vendor_list=VENDORS)
    for inv in out["invoices"]:
        print(inv["line"], inv["state"], inv["outcome"], "|", inv["reasons"][-1]["text"] if inv["reasons"] else "")
    print("credits used:", out["credits_used"])
    print("vendors file:", vfile)


if __name__ == "__main__":
    main()
