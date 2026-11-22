# Payables Desk: the Buying and paying half of "Cleared to Pay"

ContextCon 05, BITS Pilani. Team Humble Fumblers.

Tracks: **Buying and paying** (this repo) plus **Trust and compliance** (the companion service).

## The service
- **The job:** pay each vendor invoice exactly once, to the right account, and hold anything unusual for a person with the reason.
- **Who does it today:** in-house payables teams, or outsourced accounting firms paid to run a company's payables and close its books.
- **Who pays:** the finance head of the client, a mid-size Indian 3PL company.

Today, one firm clears vendors and another pays them. Fraud slips through between the two, usually as a polite "we've changed our bank details" email. This desk closes that gap:
- No invoice is paid unless the vendor was **cleared** by the Trust and compliance service.
- No invoice is paid to an account that service did not **verify**.

## Input → finished result
**Input, usually:** the accounts inbox, a folder with one file per message:
- `.eml` emails, with any PDF attachments
- `.pdf` invoices
- `.txt` pasted emails
- `.json` messages
- `purchase_orders.json` (optional), holding the client's purchase orders

```bash
.venv/bin/python -m app.cli inbox path/to/inbox/
```
Claude (`claude-opus-5-5`, structured output) only **transcribes** each message: invoice fields, each with the exact quote that states it, plus any "please pay to new bank details" request. It never decides anything. Every pay or hold decision is made by the fixed rules below, so the same invoice gets the same answer however it's worded. A bank-change email is never acted on: every invoice from that vendor is held, and the vendor goes back to Trust & Compliance.

**Input, also:** a JSON batch of invoices, the client's purchase orders and run limits, via `app.cli run`.
```json
{ "invoices": [ { "invoice_no": "INV-0042", "vendor_name": "...", "vendor_gstin": "...", "vendor_domain": "...",
                  "amount": "₹18,500", "invoice_date": "2026-10-07", "po_number": "PO-1",
                  "bank_account": { "ifsc": "...", "account_no": "..." } } ],
  "purchase_orders": [ { "po_number": "PO-1", "amount": 18500 } ],
  "limits": { "cost_credits": 10, "time_s": 600 } }
```
**Finished result:** each invoice ends as one of:
- **Paid:** payee, masked account, amount and payout reference, with the sources behind each.
- **Held for a person:** with a reason code and text.
- **Cannot verify:** with what is missing.

The audit trail (`/invoices/{id}/audit`) replays every step.

## Pass mark
- No invoice is paid twice: a duplicate in a new format, a resubmitted batch, or a kill and restart mid-payment still produces one payment.
- No rupee goes to an account that wasn't verified. A bank-details change is held and goes back for re-verification.
- A lookalike payee is held: the invoice domain must resolve, through Crustdata, to the same company that was cleared.
- An unknown payee is marked **cannot verify**, never guessed.
- Every fact has a source with its field and date, and every call has a cost line in the ledger.

## Where a person approves
Every payment waits for a person to **approve** or **reject** it. The person cannot edit results. Even after approval, the desk refuses to pay a vendor that isn't cleared or an account that isn't verified.

## Data sources
| Source | What for | Where |
|---|---|---|
| Anthropic Messages API (`claude-opus-5-5`) | Transcribes inbox emails and PDFs into invoice fields, with a quote for each; results cached by message content | `app/inbox.py` |
| Crustdata `POST /company/identify` | Resolves the invoice's domain to a company and checks it is the cleared vendor (lookalike check) | `app/crustdata.py`, `app/pipeline.py::step_identity` |
| Trust and compliance vendor records | Vendor status, verified bank account, Crustdata company ID | `app/vendors.py`, [docs/vendor_contract.md](docs/vendor_contract.md) |
| Client purchase orders (from the request) | Matching invoices to POs | `app/checks.py` |
| RazorpayX, test mode only | Payouts with `X-Payout-Idempotency` | `app/payments.py` |

## How it is built
See [docs/architecture.md](docs/architecture.md).
- **Resumable state machine:** SQLite in WAL mode, one transaction per step.
- **Idempotency key:** written before the provider is called.
- **Crustdata responses:** cached, so a restart never pays for the same lookup twice.
- **Cost ledger:** records every Crustdata, model and payment call, with Crustdata's `x-credits-used`.

## How to run it
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # add CRUSTDATA_API_KEY, Razorpay test keys
.venv/bin/python -m pytest -q   # tests (mock data lives only in tests/)
.venv/bin/uvicorn app.server:app --port 8000   # approval page at http://localhost:8000
.venv/bin/python -m app.cli run requests/<batch>.json
.venv/bin/python -m app.cli resume              # after a kill
```

## Approaches we considered
_To be filled in during the build._

## What it does now and what's next
_To be filled in during the build._

## Open-source credits
FastAPI, Uvicorn, HTTPX, python-dotenv and pytest, all under the MIT or BSD licences.
