"""Command line:
  python -m app.cli run requests/batch.json     submit a batch and process it
  python -m app.cli resume                      continue every unfinished run (after a crash/kill)
  python -m app.cli bank lines.json             post bank-statement lines to payee + account
  python -m app.cli payouts                     refresh RazorpayX payout status (queued/processing)
  python -m app.cli queue                       list invoices waiting for a person
  python -m app.cli approve <invoice_id> --by NAME
  python -m app.cli reject  <invoice_id> --by NAME
  python -m app.cli ledger [run_id]             cost of every call
"""
import argparse
import json

from dotenv import load_dotenv

load_dotenv()

from . import pipeline  # noqa: E402
from .db import connect  # noqa: E402


def run_inbox(folder, conn, client=None, **process_kw):
    """Read every message in the folder, then submit and process the batch."""
    import time
    from . import inbox
    t0 = time.time()
    req = inbox.read_inbox(folder, conn, client=client)
    run_id = pipeline.submit(req, conn)
    # attribute the model calls made while reading this inbox to the run
    conn.execute("UPDATE ledger SET run_id=? WHERE run_id IS NULL AND service='model' AND at>=?", (run_id, t0))
    out = pipeline.process(run_id, conn, **process_kw)
    out["not_invoices"] = req.get("not_invoices", [])
    out["bank_change_requests"] = [{k: v for k, v in b.items() if k != "new_account_no"} for b in req.get("bank_change_requests", [])]
    return out


def main():
    ap = argparse.ArgumentParser(prog="paydesk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("file")
    ib = sub.add_parser("inbox"); ib.add_argument("folder")
    sub.add_parser("resume")
    sub.add_parser("queue")
    sub.add_parser("payouts")
    bk = sub.add_parser("bank"); bk.add_argument("file")
    for name in ("approve", "reject"):
        p = sub.add_parser(name); p.add_argument("invoice_id"); p.add_argument("--by", required=True); p.add_argument("--note", default="")
    lg = sub.add_parser("ledger"); lg.add_argument("run_id", nargs="?")
    a = ap.parse_args()
    conn = connect()

    if a.cmd == "run":
        with open(a.file) as f:
            req = json.load(f)
        run_id = pipeline.submit(req, conn)
        print(json.dumps(pipeline.process(run_id, conn), indent=2, ensure_ascii=False))
    elif a.cmd == "inbox":
        print(json.dumps(run_inbox(a.folder, conn), indent=2, ensure_ascii=False))
    elif a.cmd == "resume":
        print(json.dumps(pipeline.resume_all(conn), indent=2, ensure_ascii=False))
    elif a.cmd == "bank":
        from . import bank
        with open(a.file) as f:
            req = json.load(f)
        print(json.dumps(bank.process(bank.submit(req, conn), conn), indent=2, ensure_ascii=False))
    elif a.cmd == "payouts":
        print(json.dumps(pipeline.sync_payouts(conn), indent=2, ensure_ascii=False))
    elif a.cmd == "queue":
        for row in conn.execute("SELECT invoice_id, outcome, reasons FROM invoices WHERE state='AWAITING_APPROVAL' ORDER BY rowid"):
            last = json.loads(row["reasons"])[-1]
            print(f"{row['invoice_id']}  [{row['outcome']}]  {last['text']}")
    elif a.cmd in ("approve", "reject"):
        pipeline.decide(a.invoice_id, "approved" if a.cmd == "approve" else "rejected", a.by, a.note, conn)
        run_id = a.invoice_id.split(":")[0]
        print(json.dumps(pipeline.process(run_id, conn), indent=2, ensure_ascii=False))
    elif a.cmd == "ledger":
        q = "SELECT service, endpoint, COUNT(*) n, SUM(credits) credits, SUM(cached) cached FROM ledger"
        args = ()
        if a.run_id:
            q += " WHERE run_id=?"; args = (a.run_id,)
        for row in conn.execute(q + " GROUP BY service, endpoint", args):
            print(dict(row))


if __name__ == "__main__":
    main()
