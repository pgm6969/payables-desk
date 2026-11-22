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


def _vendor_report(conn, vendor_id):
    from . import trust, vendors as vmod
    v = next((x for x in vmod.load_vendors() if str(x.get("vendor_id")) == str(vendor_id)), None)
    r = trust.cached(conn, v) if v else None
    if not r:
        return None
    return {k: r[k] for k in ("verdict", "summary", "flags", "cannot_verify", "screening", "cost", "checked_at", "entity")}


@app.get("/invoices/{invoice_id}/audit")
def audit(invoice_id: str):
    conn = connect()
    inv = conn.execute("SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
    if not inv:
        raise HTTPException(404)
    from .checks import mask_account
    raw = json.loads(inv["raw"])
    if raw.get("bank_account"):  # never send full account numbers to the browser
        raw["bank_account"] = {**raw["bank_account"], "account_no": mask_account(raw["bank_account"].get("account_no"))}
    return {
        "invoice": {**dict(inv), "raw": raw, "reasons": json.loads(inv["reasons"])},
        "sources": [dict(r) for r in conn.execute("SELECT * FROM sources WHERE invoice_id=?", (invoice_id,))],
        "payment": dict(p) if (p := conn.execute("SELECT * FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()) else None,
        "draft": dict(d) if (d := conn.execute("SELECT * FROM drafts WHERE invoice_id=?", (invoice_id,)).fetchone()) else None,
        "vendor_check": _vendor_report(conn, inv["vendor_id"]),
        "events": [dict(r) for r in conn.execute("SELECT kind, detail, at FROM events WHERE invoice_id=? ORDER BY event_id", (invoice_id,))],
    }


@app.get("/ledger")
def ledger():
    conn = connect()
    return [dict(r) for r in conn.execute(
        "SELECT run_id, service, endpoint, COUNT(*) calls, SUM(credits) credits, SUM(inr) inr, SUM(cached) cached_hits, "
        "MAX(at) last_at FROM ledger GROUP BY run_id, service, endpoint ORDER BY last_at DESC")]


class VendorCheck(BaseModel):
    legal_name: str
    domain: str = ""
    director: str = ""
    gstin: str = ""
    refresh: bool = False


@app.post("/vendors/check")
def vendor_check(body: VendorCheck):
    """5.1 + 5.2 on their own: is this a real, trading business, who runs it, and is anything off."""
    from . import checks, trust
    from .crustdata import BudgetExceeded, Crustdata, SourceUnavailable
    conn = connect()
    limit = float(__import__("os").environ.get("PAYDESK_VENDOR_CHECK_LIMIT", "12"))
    v = {"legal_name": body.legal_name.strip(), "domain": trust.norm_domain(body.domain), "director": body.director.strip(), "gstin": body.gstin.strip()}
    key = trust.vendor_key(v)
    spent = lambda: conn.execute("SELECT COALESCE(SUM(credits),0) c FROM ledger WHERE invoice_id=?", ("trust:" + key,)).fetchone()["c"]
    cd = Crustdata(conn, None, budget=lambda: limit - spent())
    try:
        with _lock:
            rep = trust.check_and_cache(conn, cd, v, force=body.refresh)
    except SourceUnavailable as e:
        raise HTTPException(503, f"Crustdata unavailable: {e}. Not the same as 'no record'; try again.")
    except BudgetExceeded as e:
        raise HTTPException(402, f"Cost limit for one vendor check reached: {e}")
    rep = dict(rep)
    rep["gstin_checks"] = [{"code": c, "text": t} for c, t in checks.gstin_problems(v["gstin"], v["legal_name"])]
    return rep


@app.get("/vendors")
def vendors_list():
    """The client's vendor master, each with the latest vendor-check verdict (bank details masked)."""
    from . import trust, vendors as vmod
    from .checks import mask_account
    conn = connect()
    out = []
    for v in vmod.load_vendors():
        e = trust.effective(conn, v) or v
        r = e.get("_trust")
        acct = v.get("verified_bank_account") or {}
        out.append({"vendor_id": v.get("vendor_id"), "legal_name": v.get("legal_name"), "domain": v.get("domain"),
                    "gstin": v.get("gstin"), "director": v.get("director"), "status": e.get("status"),
                    "status_source": "own vendor check" if r else ("Trust service" if v.get("status") else "not checked yet"),
                    "bank": f"{mask_account(acct.get('account_no'))} · {acct.get('ifsc', '')}" if acct else None,
                    "summary": r["summary"] if r else None, "checked_at": r["checked_at"] if r else v.get("verified_at"),
                    "cost": (r or {}).get("cost")})
    return out


@app.get("/meta")
def meta():
    import os
    from .payments import ProviderNotConfigured, get_provider
    try:
        get_provider(); provider = True
    except ProviderNotConfigured:
        provider = False
    return {"demo": os.environ.get("PAYDESK_DEMO") == "1", "payment_provider": provider,
            "model_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "crustdata_key": bool(os.environ.get("CRUSTDATA_API_KEY"))}


@app.get("/state")
def state():
    conn = connect()
    rows = conn.execute("SELECT invoice_id, raw, state, outcome, reasons, updated_at FROM invoices ORDER BY rowid DESC LIMIT 200").fetchall()
    return [{"invoice_id": r["invoice_id"], "raw": {k: v for k, v in json.loads(r["raw"]).items() if k != "bank_account"},
             "state": r["state"], "outcome": r["outcome"], "updated_at": r["updated_at"],
             "reasons": json.loads(r["reasons"])} for r in rows]


@app.get("/", response_class=HTMLResponse)
def page():
    with open(__file__.replace("server.py", "static/index.html")) as f:
        return f.read()
