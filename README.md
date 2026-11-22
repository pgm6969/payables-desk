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

## Invoice clearance overview and payment mail
- **Invoice clearance** (first page under Work):
  - a "waiting for you" callout with the rupee total to decide on
  - tiles: paid once, duplicates stopped, bank changes blocked, look-alikes caught, cannot verify, invoices read
  - four charts with hover tooltips: where every invoice stands (donut), why invoices are held, money by outcome, and how far invoices got through the six steps

  The status colours were checked with a palette validator for colour-blind separation and contrast, in light and dark mode. Every chart also has a legend and value labels, so no chart relies on colour alone.
- **Worklist:** a scan or upload bar sits at the top, and every bill has **Add document**. **Approve payment** is green, **Approve anyway** (a held item) is green with a warning, and **Reject** is red.
- **Payment mail (Gmail, read-only):**
  1. Connect a Google account with `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. That's a Web OAuth client with redirect URI `http://localhost:8000/gmail/callback`, and the scope is `gmail.readonly`.
  2. The desk lists only payment-related mail (invoices, bills, payments, remittances, bank-detail changes, purchase orders, GSTIN). The search query is `gmail_query` in the rules file.
  3. **Read into desk** pulls one message, attachments included, through the same reader and checks as an upload.

  The refresh token is kept in `data/gmail_token.json` (git-ignored, owner-only). Disconnecting revokes it at Google and deletes the file.
- **Inbox reader:** always on. It uses fixed patterns plus OCR, and Claude when `ANTHROPIC_API_KEY` is set.

## Scan a bill, documents and verification
- **Scan or upload a bill** (the **Scan bill** page, or `POST /bills/upload`): a photo, PDF, `.txt` or `.eml`, dragged in or taken with the phone camera.
  - **With `ANTHROPIC_API_KEY`:** Claude reads PDFs and photos and quotes each field.
  - **Without it:** text PDFs are read with pypdf, photos with Tesseract OCR, and then the fixed-pattern reader runs.

  Each bill then goes through every check and lands in the worklist. The original file is kept by content hash. Anything unreadable is reported as unreadable, never guessed.
- **The bill as a document** (`/invoices/{id}/doc`): printable, with the decision stamped on it. It shows the scanned image or original PDF, who uploaded it and how it was read, and a **payment verification checklist**:
  - cleared vendor
  - valid GSTIN
  - genuine sender
  - verified bank account
  - not a duplicate
  - matches its PO
  - goods receipt on file
  - approved by a person
  - paid once
- **Supporting documents:** attach a PO, goods receipt or delivery proof, contract, or cancelled cheque to any bill. Each attachment is recorded with who added it, and a goods receipt turns that checklist line green.
- **Administrator:** one admin login (username and password) sees users, every approval and rejection, recent activity and the rules in force. Admins cannot edit results.
  - The password is stored only as a salted scrypt hash in `data/admin.json`, which git ignores.
  - On first start a random password is written to `data/admin_credentials.txt` (git-ignored, owner-only).
  - Five wrong tries lock the login for 5 minutes.
  - Rotate the password with `.venv/bin/python scripts/admin_password.py`, or set `ADMIN_USERNAME`/`ADMIN_PASSWORD` before first start.

## What else it does
- **Bank-statement posting** (`app/bank.py`, the **Bank lines** page, `python -m app.cli bank lines.json`). Each bank line gets a payee and an account:
  1. The payee comes from the client's vendor master (legal name or a recorded bank descriptor).
  2. Otherwise from Crustdata, but only when one large company clearly owns the name.
  3. Otherwise the line is **cannot verify**.

  Duplicates are held, and the account comes from the client's chart of accounts. On live data it reproduces the problem statement's example:
  - `RAZ*SWIGGY BLR` → Swiggy, Staff meals
  - `AWS EMEA` → Amazon Web Services EMEA SARL, Cloud hosting
  - the second identical AWS line → held as a possible duplicate
  - `PAYTM*SHARMA TRD` → cannot verify (several businesses share the name)
- **Sign-in:**
  - **Google** (Google Identity Services): set `GOOGLE_CLIENT_ID`, and optionally `GOOGLE_ALLOWED_DOMAIN`. The ID token is verified with Google before a session starts.
  - Without it, there's a local demo sign-up. It's labelled "local account" on every approval it records.
  - Every approval is recorded with the signed-in identity, never a typed name. Sessions are HMAC-signed cookies, and no passwords are stored.
- **Two-person approval:** at or above ₹1,00,000 (`two_person_threshold_inr`), a second, **different** person must approve.
- **Rules in one place:** `app/config.py`, which `data/rules.json` overrides. It holds the limits, PO tolerance, look-alike distance, approval threshold, prices and chart of accounts. A changed requirement is a one-line edit.
- **Kill-on-stage in one command:** `.venv/bin/python scripts/kill_demo.py`.
  1. The process is killed right after the payout was created, before the desk recorded it.
  2. It restarts twice.
  3. The result is **exactly one payout**, sent with the same idempotency key.
- **Cost & price** (`/economics`, measured from the ledger at ₹25.2 per credit):

  | Job | Measured cost | Our price |
  |---|---|---|
  | New vendor check | ₹105.84 | ₹250 |
  | Invoice, all-in (first batch, incl. vendor checks) | ₹39.69 | ₹60 |
  | Invoice from a repeat vendor | ₹0 (cached) | ₹60 |
  | Bank line | ₹2.52 | — |

  Prices are ours, not a market quote.
- **Decision letter per invoice** (`/invoices/{id}/letter`, shown in the audit trail): a plain-language account of why it was paid or held, with every source.
- **Inbox without a model key:** `.txt`/`.eml` invoices are read by fixed patterns, with the exact line as evidence for each field. PDFs need the Claude reader (`ANTHROPIC_API_KEY`).

## Vendor check (Trust & Compliance 5.1 + 5.2)
If a vendor record has no status from the Trust service, the desk runs its own check, live, and caches the result per vendor. The **Vendor check** tab runs the same check on any name and website.

1. **Resolve by domain**, keeping only a Crustdata record that lists that domain. A name alone is ambiguous.
2. **Profile:** active, year founded, headcount, HQ. Flags a company that is inactive, newly formed, tiny, or a different company than the vendor master names.
3. **Director:** must appear among the founders, CXOs or decision makers on record. Otherwise the result is **cannot verify**.
4. **Website:** live, and names the company.
5. **Adverse media:** the company is flagged only for serious legal terms about the company itself, so news where it was the victim of a scam doesn't count. A director is flagged only when an article names the person **and** the company. Other articles are cleared as namesakes and kept only as a count. A match is a lead for a person to read, never a finding.

The result is **CLEAR**, **HOLD** (with flags) or **CANNOT_VERIFY** (naming what's missing). A new vendor costs about 4–5 credits; later invoices from that vendor cost nothing.

Other checks:
- GSTIN check character and PAN entity type.
- A look-alike sender domain, caught by edit distance before any lookup.
- A PO already billed.
- A vendor record that changed after approval, which holds the invoice for re-approval.
- A drafted call-back to the contact **on file** on any bank-change request. It is never sent by the desk.

## Payments: what actually runs today
**Payouts are simulated.** We could not get RazorpayX test keys during the build (sign-up needs business KYC), so payouts run in a **local sandbox**, `app/payments.py::LocalSandbox`. No provider is called and no money moves. Everything it produces is labelled "local_sandbox (simulated)" on the page, in the audit trail and in the ledger.

The sandbox keeps RazorpayX's payout contract, so the exactly-once logic is exercised for real:
- one payout per idempotency key, stored in SQLite, so it survives restarts;
- a retry with the same key returns the first payout;
- the same key with a different amount or account is refused;
- an IFSC starting `FAIL` is refused, to demo a rejected payout.

The RazorpayX test-mode integration below is written and tested against a mocked API, but has not been run against Razorpay. It takes over automatically as soon as its three keys are in `.env`. `PAYDESK_PAYMENTS=razorpayx` forces it.

## Payments: RazorpayX Payouts, test mode (ready, not yet connected)
- A person approves every payout. The desk then pays the vendor's **verified** account, never an account named in an invoice or email, through `POST /v1/payouts` with `X-Payout-Idempotency`.
- The idempotency key is a UUID saved in SQLite **before** the call. Each outcome is handled like this:

| What happens | What the desk does |
|---|---|
| Accepted | Records the payout id and status (`queued` / `processing` / `processed`). |
| Refused (4xx, e.g. invalid IFSC) | Invoice goes to **Payout failed** with the provider's reason. Nothing was paid and nothing is retried. |
| No answer (timeout / 5xx) | Invoice stays **Paying**. `resume` retries with the **same** key, so RazorpayX treats it as the same payout. |
| Later `failed` / `reversed` | `python -m app.cli payouts` (or **Refresh payout status** on the page) moves the invoice to **Payout failed**. |

- A live key (`rzp_live_…`) is refused at start-up.

**Set-up** (in RazorpayX, test mode):
1. Generate keys under My Account & Settings → Developer Controls.
2. Copy the test **Customer Identifier** from My Account & Settings → Banking. It is your `RAZORPAYX_ACCOUNT_NUMBER`.
3. Add test balance.
4. Put the three values in `.env`.

Test payouts stay in `processing` until you move them in the Dashboard.

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
| Crustdata `POST /company/enrich` | Vendor check 5.1: name, status, year founded, headcount, HQ, founders/CXOs (director on record?) | `app/trust.py` |
| Crustdata `POST /web/enrich/live` | Vendor check 5.1: the website is live and names the company (tries `www.`, then browser-like retrieval) | `app/trust.py` |
| Crustdata `POST /web/search/live` | Vendor check 5.2: adverse news for the company and director; namesakes cleared, matches escalated as leads | `app/trust.py` |
| Local computation | GSTIN format, check character and PAN entity type; look-alike sender (edit distance) | `app/checks.py` |
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
