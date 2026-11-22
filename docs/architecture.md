# Architecture

```mermaid
flowchart LR
  INBOX[Accounts inbox<br/>.eml · .pdf · .txt] -->|cli inbox / POST /inbox| READ[Inbox reader<br/>Claude transcribes fields + quotes<br/>flags bank-change emails<br/>cached · ledgered]
  READ --> REQ
  REQ[Invoice batch + POs + limits] -->|POST /runs or cli run| SUB[submit: run_id = hash of batch]
  SUB --> DB[(SQLite WAL<br/>runs · invoices · sources<br/>approvals · payments · ledger · events)]
  subgraph Pipeline[Resumable pipeline: one transaction per step]
    S1[1 Vendor gate<br/>cleared?] --> S2[2 Identity<br/>invoice domain = cleared company?]
    S2 --> S3[3 Checks<br/>bank change · duplicate · PO]
    S3 --> Q{{4 Approval queue<br/>person approves / rejects}}
    Q --> S5[5 Pay<br/>idempotency key saved first]
  end
  DB <--> Pipeline
  TRUST[Trust & Compliance service<br/>vendor records] -->|GET /vendors| S1
  S2 -->|retry 429/5xx · cache · ledger| CD[Crustdata API<br/>/company/identify]
  S5 -->|X-Payout-Idempotency| RZP[RazorpayX test mode]
  Pipeline --> OUT[Paid · Held + reason · Cannot verify + what is missing<br/>audit trail with sources]
  USER[Person: Google sign-in<br/>signed session] -->|approve / reject<br/>2 people ≥ ₹1 lakh| Q
  BANK[Bank-statement lines] --> BP[Bank posting<br/>vendor master → Crustdata if one company owns the name → else cannot verify<br/>duplicates held · chart of accounts]
  BP --> CD
  RULES[(Rules file<br/>limits · tolerances · thresholds · prices · accounts)] -.-> Pipeline
  RULES -.-> BP
```

Also in the picture:
- **Vendor check (5.1 + 5.2)** runs inside step 1 when the vendor master has no Trust status (`app/trust.py`). It uses `/company/identify`, `/company/enrich`, `/web/enrich/live` and `/web/search/live`, and is cached per vendor.
- **Payouts** go to RazorpayX test mode when its keys are set, otherwise to a local sandbox. The sandbox is labelled as simulated and keeps the same idempotency contract.
- **Cost ledger:** every Crustdata, model and payout call is recorded there. `/economics` turns it into cost per job.

Where things sit:
- **Data calls:** `app/crustdata.py`. It retries with backoff on 429, 5xx and timeouts, caches every response in `http_cache`, and raises `SourceUnavailable`, which holds the case rather than reading silence as an answer.
- **Approvals:** the `approvals` table, approved or rejected by a person through the UI, the API or the CLI. Results can't be edited.
- **Retries and restarts:** each step commits its new state in one transaction. On restart, `resume_all()` continues every run still in progress. A payment reuses its saved idempotency key.
- **Cost ledger:** the `ledger` table. Each row is one call, with its credits (from the `x-credits-used` header), whether it came from the cache, its latency and its status. A run stops making calls at its `cost_limit` and checks no new invoices after its `time_limit_s`.
