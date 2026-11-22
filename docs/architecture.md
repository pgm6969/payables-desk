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
```

Where things sit:
- **Data calls:** `app/crustdata.py`. It retries with backoff on 429, 5xx and timeouts, caches every response in `http_cache`, and raises `SourceUnavailable`, which holds the case rather than reading silence as an answer.
- **Approvals:** the `approvals` table, approved or rejected by a person through the UI, the API or the CLI. Results can't be edited.
- **Retries and restarts:** each step commits its new state in one transaction. On restart, `resume_all()` continues every run still in progress. A payment reuses its saved idempotency key.
- **Cost ledger:** the `ledger` table. Each row is one call, with its credits (from the `x-credits-used` header), whether it came from the cache, its latency and its status. A run stops making calls at its `cost_limit` and checks no new invoices after its `time_limit_s`.
