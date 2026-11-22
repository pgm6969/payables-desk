"""HTTP API + approval page.  uvicorn app.server:app --reload
On startup it resumes every unfinished run, so a restart picks up where it stopped."""
import json
import threading

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import pipeline  # noqa: E402
from .db import connect  # noqa: E402

app = FastAPI(title="Payables Desk")
_lock = threading.Lock()  # one worker at a time over the SQLite state


def _process(run_id):
    with _lock:
        return pipeline.process(run_id, connect())


@app.on_event("startup")
def resume_on_start():
    threading.Thread(target=lambda: [_process(r["run_id"]) for r in
                                     connect().execute("SELECT run_id FROM runs WHERE status='running'").fetchall()],
                     daemon=True).start()


@app.post("/runs")
def create_run(request: dict):
    run_id = pipeline.submit(request)
    threading.Thread(target=_process, args=(run_id,), daemon=True).start()
    return {"run_id": run_id}


class InboxRequest(BaseModel):
    folder: str


@app.post("/inbox")
def run_inbox(body: InboxRequest):
    """Read an inbox folder (emails / PDFs / text) on this machine, then run the batch."""
    from .cli import run_inbox as _run
    with _lock:
        return _run(body.folder, connect())


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    return pipeline.summary(run_id)


class Decision(BaseModel):
    by: str
    note: str = ""


@app.post("/invoices/{invoice_id}/{decision}")
def decide(invoice_id: str, decision: str, d: Decision):
    if decision not in ("approve", "reject"):
        raise HTTPException(404)
    try:
        pipeline.decide(invoice_id, "approved" if decision == "approve" else "rejected", d.by, d.note)
    except ValueError as e:
        raise HTTPException(409, str(e))
    threading.Thread(target=_process, args=(invoice_id.split(":")[0],), daemon=True).start()
    return {"ok": True}


@app.get("/invoices/{invoice_id}/audit")
def audit(invoice_id: str):
    conn = connect()
    inv = conn.execute("SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
    if not inv:
        raise HTTPException(404)
    return {
        "invoice": {**dict(inv), "raw": json.loads(inv["raw"]), "reasons": json.loads(inv["reasons"])},
        "sources": [dict(r) for r in conn.execute("SELECT * FROM sources WHERE invoice_id=?", (invoice_id,))],
        "payment": dict(p) if (p := conn.execute("SELECT * FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()) else None,
        "events": [dict(r) for r in conn.execute("SELECT kind, detail, at FROM events WHERE invoice_id=? ORDER BY event_id", (invoice_id,))],
    }


@app.get("/ledger")
def ledger():
    conn = connect()
    return [dict(r) for r in conn.execute(
        "SELECT run_id, service, endpoint, COUNT(*) calls, SUM(credits) credits, SUM(cached) cached_hits "
        "FROM ledger GROUP BY run_id, service, endpoint")]


@app.get("/state")
def state():
    conn = connect()
    rows = conn.execute("SELECT invoice_id, raw, state, outcome, reasons FROM invoices ORDER BY rowid DESC LIMIT 200").fetchall()
    return [{"invoice_id": r["invoice_id"], "raw": json.loads(r["raw"]), "state": r["state"], "outcome": r["outcome"],
             "reasons": json.loads(r["reasons"])} for r in rows]


@app.get("/", response_class=HTMLResponse)
def page():
    with open(__file__.replace("server.py", "static/index.html")) as f:
        return f.read()
