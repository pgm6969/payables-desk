"""Command line:
  python -m app.cli run requests/batch.json     submit a batch and process it
  python -m app.cli resume                      continue every unfinished run (after a crash/kill)
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


def main():
    ap = argparse.ArgumentParser(prog="paydesk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("file")
    sub.add_parser("resume")
    sub.add_parser("queue")
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
    elif a.cmd == "resume":
        print(json.dumps(pipeline.resume_all(conn), indent=2, ensure_ascii=False))
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
