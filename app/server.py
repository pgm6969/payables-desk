"""HTTP API + approval page.  uvicorn app.server:app --reload
On startup it resumes every unfinished run, so a restart picks up where it stopped."""
import json
import threading

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request, Response  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import auth, pipeline  # noqa: E402
from .db import connect  # noqa: E402

app = FastAPI(title="Payables Desk")
_lock = threading.Lock()  # one worker at a time over the SQLite state
OPEN_PATHS = ("/signin", "/auth/", "/healthz")


@app.middleware("http")
async def require_sign_in(request: Request, call_next):
    """Every page and API call needs a signed-in person; approvals record who they are."""
    path = request.url.path
    if any(path == p or path.startswith(p) for p in OPEN_PATHS):
        return await call_next(request)
    user = auth.read_session(request.cookies.get(auth.COOKIE))
    if not user:
        if request.method == "GET" and path == "/":
            return RedirectResponse("/signin")
        return JSONResponse({"detail": "sign in first"}, status_code=401)
    request.state.user = user
    return await call_next(request)


def _static(name):
    with open(__file__.replace("server.py", f"static/{name}")) as f:
        return f.read()


@app.get("/signin", response_class=HTMLResponse)
def signin_page():
    return _static("signin.html").replace("__GOOGLE_CLIENT_ID__", json.dumps(auth.google_client_id()))


def _start_session(user):
    conn = connect()
    auth.upsert_user(conn, user)
    resp = JSONResponse({"ok": True, "user": {k: user[k] for k in ("name", "email", "provider")}})
    resp.set_cookie(auth.COOKIE, auth.make_session(user), max_age=auth.TTL, httponly=True, samesite="lax")
    return resp


class GoogleCredential(BaseModel):
    credential: str


@app.post("/auth/google")
def auth_google(body: GoogleCredential):
    try:
        user = auth.verify_google(body.credential)
    except ValueError as e:
        raise HTTPException(401, str(e))
    return _start_session(user)


class LocalSignup(BaseModel):
    name: str
    email: str


@app.post("/auth/local")
def auth_local(body: LocalSignup):
    """Local demo account; refused whenever Google sign-in is configured."""
    import re
    if auth.google_client_id():
        raise HTTPException(403, "Use Google sign-in")
    name, email = body.name.strip(), body.email.strip().lower()
    if len(name) < 2 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "Enter your name and a valid email")
    return _start_session({"name": name, "email": email, "provider": "local"})


@app.post("/auth/signout")
def signout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/me")
def me(request: Request):
    u = request.state.user
    return {"name": u["name"], "email": u["email"], "provider": u["provider"], "label": auth.approver_label(u)}


@app.get("/healthz")
def healthz():
    return {"ok": True}


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
    note: str = ""


@app.post("/invoices/{invoice_id}/{decision}")
def decide(invoice_id: str, decision: str, request: Request, d: Decision | None = None):
    """The approver is the signed-in person, never a name typed into the request."""
    if decision not in ("approve", "reject"):
        raise HTTPException(404)
    by = auth.approver_label(request.state.user)
    try:
        result = pipeline.decide(invoice_id, "approved" if decision == "approve" else "rejected", by, (d.note if d else ""))
    except ValueError as e:
        raise HTTPException(409, str(e))
    threading.Thread(target=_process, args=(invoice_id.split(":")[0],), daemon=True).start()
    return {"ok": True, "result": result, "by": by}


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


@app.get("/economics")
def economics():
    """Measured cost per job from the ledger, next to our price. All figures in ₹."""
    from . import config
    conn = connect()
    rate = float(config.get("inr_per_credit"))
    vc = conn.execute("SELECT invoice_id, SUM(credits) c FROM ledger WHERE invoice_id LIKE 'trust:%' GROUP BY invoice_id HAVING c > 0").fetchall()
    inv = conn.execute("SELECT COUNT(*) n FROM invoices").fetchone()["n"] or 0
    inv_credits = conn.execute("SELECT COALESCE(SUM(credits),0) c FROM ledger WHERE invoice_id IS NOT NULL AND invoice_id NOT LIKE 'trust:%' "
                               "AND invoice_id NOT LIKE 'bank_%'").fetchone()["c"]
    model_inr = conn.execute("SELECT COALESCE(SUM(inr),0) i FROM ledger WHERE service='model'").fetchone()["i"]
    bank_lines = conn.execute("SELECT COUNT(*) n FROM sqlite_master WHERE name='bank_lines'").fetchone()["n"] and \
        conn.execute("SELECT COUNT(*) n FROM bank_lines").fetchone()["n"] or 0
    bank_credits = conn.execute("SELECT COALESCE(SUM(credits),0) c FROM ledger WHERE invoice_id LIKE 'bank_%'").fetchone()["c"]
    vendor_cost = (sum(r["c"] for r in vc) / len(vc) * rate) if vc else None
    invoice_cost = ((inv_credits * rate + model_inr) / inv) if inv else None
    run_credits = conn.execute("SELECT COALESCE(SUM(credits),0) c FROM ledger WHERE run_id IN (SELECT run_id FROM runs)").fetchone()["c"]
    all_in = ((run_credits * rate + model_inr) / inv) if inv else None
    price_inv, price_vendor = config.get("price_per_invoice_inr"), config.get("price_per_new_vendor_check_inr")
    return {
        "inr_per_credit": rate,
        "per_new_vendor_check": {"measured_inr": round(vendor_cost, 2) if vendor_cost is not None else None, "samples": len(vc), "price_inr": price_vendor},
        "per_invoice": {"measured_inr": round(all_in, 2) if all_in is not None else None, "samples": inv, "price_inr": price_inv,
                        "note": "all-in, first batch: includes checking every new vendor"},
        "per_invoice_repeat_vendor": {"measured_inr": round(invoice_cost, 2) if invoice_cost is not None else None, "price_inr": price_inv,
                                      "note": "vendor already checked: its check is cached"},
        "per_bank_line": {"measured_inr": round(bank_credits * rate / bank_lines, 2) if bank_lines else None, "samples": bank_lines},
    }


@app.get("/invoices/{invoice_id}/letter")
def letter(invoice_id: str):
    """A plain-language account of the decision, with its sources, for the client or an auditor."""
    conn = connect()
    inv = conn.execute("SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
    if not inv:
        raise HTTPException(404)
    raw, reasons = json.loads(inv["raw"]), json.loads(inv["reasons"])
    srcs = conn.execute("SELECT * FROM sources WHERE invoice_id=?", (invoice_id,)).fetchall()
    pay = conn.execute("SELECT * FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
    verdict = {"PAID": "was paid", "CANNOT_VERIFY": "could not be verified and was not paid", "REJECTED": "was rejected by a person",
               "PAYOUT_FAILED": "was approved, but the payout failed", "AWAITING_APPROVAL": "is waiting for a person"}.get(inv["state"], "is in progress")
    lines = [f"Invoice {raw.get('invoice_no') or '(no number)'} from {raw.get('vendor_name')} for ₹{(inv['amount_paise'] or 0)/100:,.2f} {verdict}.", "",
             "Why:"] + [f"- {r['text']}" + (f" Missing: {r['missing']}." if r.get("missing") else "") for r in reasons]
    if pay:
        lines += ["", f"Payment: {pay['provider']}" + (" (SIMULATED, no money moved)" if pay["provider"] == "local_sandbox" else "") +
                  f", key {pay['idempotency_key']}, payout {pay['provider_ref'] or 'not yet accepted'}, status {pay['provider_status'] or pay['status']}."]
    lines += ["", "Sources:"] + [f"- {s['fact']}: {s['value']} ({s['url']}, field {s['field']}, retrieved {s['retrieved_at']})" for s in srcs]
    return {"text": "\n".join(lines)}


@app.post("/bank")
def bank_post(request: dict):
    """Post bank-statement lines: {"bank_lines": [{"date", "description", "amount"}]}."""
    from . import bank
    with _lock:
        conn = connect()
        return bank.process(bank.submit(request, conn), conn)


@app.get("/bank")
def bank_list():
    from . import bank
    return bank.all_lines(connect())


@app.get("/meta")
def meta():
    import os
    from .payments import ProviderNotConfigured, get_provider
    try:
        p = get_provider(connect())
        provider, mode = True, p.name
    except ProviderNotConfigured:
        provider, mode = False, None
    from . import config
    return {"demo": os.environ.get("PAYDESK_DEMO") == "1", "payment_provider": provider, "payment_mode": mode,
            "two_person_threshold_inr": config.get("two_person_threshold_inr"),
            "payments_simulated": mode == "local_sandbox",
            "model_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "crustdata_key": bool(os.environ.get("CRUSTDATA_API_KEY"))}


@app.get("/state")
def state():
    conn = connect()
    rows = conn.execute("SELECT i.invoice_id, i.raw, i.state, i.outcome, i.reasons, i.updated_at, p.provider_status, p.provider_ref "
                        "FROM invoices i LEFT JOIN payments p ON p.invoice_id=i.invoice_id ORDER BY i.rowid DESC LIMIT 200").fetchall()
    return [{"invoice_id": r["invoice_id"], "raw": {k: v for k, v in json.loads(r["raw"]).items() if k != "bank_account"},
             "state": r["state"], "outcome": r["outcome"], "updated_at": r["updated_at"],
             "payout_status": r["provider_status"], "payout_id": r["provider_ref"],
             "reasons": json.loads(r["reasons"])} for r in rows]


@app.post("/payouts/sync")
def payouts_sync():
    """Ask RazorpayX (test mode) for the latest status of payouts still in flight."""
    from .payments import ProviderNotConfigured
    try:
        with _lock:
            return {"updated": pipeline.sync_payouts(connect())}
    except ProviderNotConfigured as e:
        raise HTTPException(409, str(e))


@app.post("/resume")
def resume_now():
    """Continue every unfinished run now (e.g. after Razorpay keys were added or a timeout)."""
    with _lock:
        return pipeline.resume_all(connect())


@app.get("/", response_class=HTMLResponse)
def page():
    return _static("index.html")
