# Vendor record contract (Trust & Compliance → Payables Desk)

The Trust & Compliance service writes vendor records, and the Payables Desk only reads them.
Deliver them as either `GET {TRUST_API_URL}/vendors` (a JSON list) or a file at `data/vendors.json`.

```json
{
  "vendor_id": "V1",
  "legal_name": "Blue Dart Express Limited",
  "gstin": "27AAACB0446L1ZS",
  "domain": "bluedart.com",
  "crustdata_company_id": 736,
  "status": "cleared",
  "verified_at": "2026-11-22T10:30:00+05:30",
  "verified_bank_account": { "ifsc": "HDFC0000001", "account_no": "50100012345678", "holder_name": "BLUE DART EXPRESS LTD" },
  "reasons": [ { "text": "Active company, registry record ...", "source_ids": [1] } ],
  "sources": [ { "fact": "...", "url": "...", "field": "...", "value": "...", "record_date": "...", "retrieved_at": "..." } ]
}
```

`status` takes one of these values:
- `cleared`
- `held`
- `cannot_verify`

The Payables Desk pays **only** when:
- `status == "cleared"`,
- and the payment goes to `verified_bank_account`,
- and the company behind the invoice's domain has the same `crustdata_company_id` as the cleared vendor.

If an invoice asks for a different bank account, the desk holds the invoice and sends the vendor back to Trust & Compliance to be verified again.
