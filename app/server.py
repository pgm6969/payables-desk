"""HTTP API + approval page.  uvicorn app.server:app --reload
On startup it resumes every unfinished run, so a restart picks up where it stopped."""
import json
import re
import threading

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import auth, pipeline  # noqa: E402
from .db import connect  # noqa: E402

app = FastAPI(title="Payables Desk")
auth.ensure_admin()  # creates the admin login on first start (credentials file is git-ignored)
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


class AdminLogin(BaseModel):
    username: str
    password: str


@app.post("/auth/admin")
def auth_admin(body: AdminLogin, request: Request):
    try:
        user = auth.verify_admin(body.username, body.password, request.client.host if request.client else "local")
    except ValueError as e:
        raise HTTPException(401, str(e))
    return _start_session(user)


def _require_admin(request: Request):
    if not auth.is_admin(request.state.user):
        raise HTTPException(403, "Administrators only")


@app.get("/admin/overview")
def admin_overview(request: Request):
    """Administrators see who uses the desk, what happened, and the rules in force. Admins cannot edit results."""
    from . import config
    _require_admin(request)
    conn = connect()
    users = [dict(r) for r in conn.execute("SELECT email, name, provider, created_at, last_login FROM users ORDER BY last_login DESC")] \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='users'").fetchone() else []
    events = [dict(r) for r in conn.execute("SELECT run_id, invoice_id, kind, detail, at FROM events ORDER BY event_id DESC LIMIT 60")]
    decisions = [dict(r) for r in conn.execute(
        "SELECT a.invoice_id, a.decision, a.decided_by, a.decided_at, a.first_by, i.amount_paise FROM approvals a "
        "JOIN invoices i USING(invoice_id) WHERE a.decision IS NOT NULL OR a.first_by IS NOT NULL ORDER BY COALESCE(a.decided_at, a.first_at) DESC")]
    counts = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) n FROM invoices GROUP BY state")}
    return {"users": users, "events": events, "decisions": decisions, "invoice_counts": counts, "rules": config.all_rules()}


@app.post("/auth/signout")
def signout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/me")
def me(request: Request):
    u = request.state.user
    return {"name": u["name"], "email": u["email"], "provider": u["provider"], "role": u.get("role", "user"), "label": auth.approver_label(u)}


@app.get("/gmail/status")
def gmail_status():
    from . import gmail
    return gmail.status()


@app.get("/gmail/connect")
def gmail_connect(request: Request):
    from . import gmail
    if not gmail.configured():
        raise HTTPException(409, "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first")
    return RedirectResponse(gmail.auth_url(str(request.base_url)))


@app.get("/gmail/callback")
def gmail_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    from . import gmail
    if error:
        return RedirectResponse("/?gmail=denied")
    try:
        gmail.exchange(code, state, str(request.base_url))
    except (ValueError, httpx_error()) as e:
        return HTMLResponse(f"<p>Could not connect Gmail: {e}</p><p><a href='/'>Back to the desk</a></p>", status_code=400)
    return RedirectResponse("/?gmail=connected")


def httpx_error():
    import httpx
    return httpx.HTTPError


@app.get("/gmail/messages")
def gmail_messages():
    from . import gmail
    try:
        return {"messages": gmail.list_payment_mail(), **gmail.status()}
    except gmail.NotConnected as e:
        raise HTTPException(409, str(e))


@app.post("/gmail/import/{msg_id}")
def gmail_import(msg_id: str, request: Request):
    """Read one mail (with its attachments) into the desk, like an uploaded .eml."""
    import re
    from . import gmail, scan
    if not re.fullmatch(r"[0-9a-zA-Z]{6,40}", msg_id):
        raise HTTPException(404)
    try:
        raw = gmail.raw_message(msg_id)
    except gmail.NotConnected as e:
        raise HTTPException(409, str(e))
    with _lock:
        return scan.process_upload([(f"gmail-{msg_id}.eml", raw)], auth.approver_label(request.state.user), connect())


@app.post("/gmail/disconnect")
def gmail_disconnect():
    from . import gmail
    gmail.disconnect()
    return {"ok": True}


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


@app.post("/invoices/{invoice_id}/approve")
def approve(invoice_id: str, request: Request, d: Decision | None = None):
    return decide(invoice_id, "approve", request, d)


@app.post("/invoices/{invoice_id}/reject")
def reject(invoice_id: str, request: Request, d: Decision | None = None):
    return decide(invoice_id, "reject", request, d)


def decide(invoice_id: str, decision: str, request: Request, d: Decision | None = None):
    """The approver is the signed-in person, never a name typed into the request."""
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


class Onboard(BaseModel):
    legal_name: str
    gstin: str
    domain: str = ""
    director: str = ""
    ifsc: str = ""
    account_no: str = ""
    gst_legal_name: str = ""
    gst_status: str = ""
    gst_registration_date: str = ""


@app.post("/vendors/onboard")
def onboard_vendor(body: Onboard, request: Request):
    """Add a vendor to the client's vendor master: GSTIN checked locally, the GST registry result recorded
    with who looked it up, then the live vendor check (5.1 + 5.2). The verdict decides whether it can be paid."""
    import time as _t
    from . import checks, trust, vendors as vmod
    from .crustdata import BudgetExceeded, Crustdata, SourceUnavailable
    by = auth.approver_label(request.state.user)
    g = vmod.norm_gstin(body.gstin)
    probs = checks.gstin_problems(g, body.legal_name)
    if not g or probs:
        raise HTTPException(400, probs[0][1] if probs else "A GSTIN is required")
    rec = {"legal_name": body.legal_name.strip(), "gstin": g, "domain": trust.norm_domain(body.domain),
           "director": body.director.strip() or None, "onboarded_by": by, "onboarded_at": _t.strftime("%Y-%m-%d %H:%M")}
    if body.gst_status:
        rec["gst_registry"] = {"legal_name": body.gst_legal_name.strip(), "status": body.gst_status.strip(),
                               "registration_date": body.gst_registration_date.strip(), "checked_by": by,
                               "checked_at": _t.strftime("%Y-%m-%d")}
    if body.account_no.strip():
        if not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", body.ifsc.strip().upper()):
            raise HTTPException(400, "Enter a valid 11-character IFSC with the account number")
        rec["verified_bank_account"] = {"ifsc": body.ifsc.strip().upper(), "account_no": re.sub(r"\s", "", body.account_no),
                                        "verified_by": by, "evidence": "entered at onboarding; attach a cancelled cheque to the bill"}
    conn = connect()
    saved = vmod.save_vendor(rec)
    try:
        with _lock:
            report = trust.check_and_cache(conn, Crustdata(conn, None, budget=lambda: 12), saved, force=True)
    except (SourceUnavailable, BudgetExceeded) as e:
        raise HTTPException(503, f"Saved, but the vendor check could not run: {e}")
    return {"vendor": {k: v for k, v in saved.items() if k != "verified_bank_account"}, "report": report,
            "has_verified_account": "verified_bank_account" in saved}


@app.post("/invoices/{invoice_id}/recheck")
def recheck_bill(invoice_id: str, request: Request):
    try:
        with _lock:
            return pipeline.recheck(invoice_id, auth.approver_label(request.state.user), "vendor master or records updated", connect())
    except ValueError as e:
        raise HTTPException(409, str(e))


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


@app.get("/account")
def account():
    """The client's operating account: balance and every money movement the desk knows about,
    newest first. Payouts (from this desk) and bank-statement lines (posted or held) are both shown."""
    from . import config
    conn = connect()
    acct = dict(config.get("company_account"))
    tx_rows = []
    for p in conn.execute("SELECT p.*, i.raw FROM payments p JOIN invoices i USING(invoice_id) WHERE p.provider_ref IS NOT NULL"):
        raw = json.loads(p["raw"])
        failed = (p["provider_status"] or "") in ("failed", "reversed", "cancelled", "rejected")
        tx_rows.append({"at": p["updated_at"], "date": __import__("time").strftime("%Y-%m-%d", __import__("time").localtime(p["created_at"])),
                        "kind": "Payout", "party": raw.get("vendor_name"), "detail": f"Invoice {raw.get('invoice_no')} · {p['provider_ref']}",
                        "amount_inr": -(p["amount_paise"] / 100), "status": p["provider_status"] or p["status"], "counts": not failed,
                        "simulated": p["provider"] == "local_sandbox", "invoice_id": p["invoice_id"]})
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='bank_lines'").fetchone():
        for b in conn.execute("SELECT * FROM bank_lines WHERE amount_paise IS NOT NULL"):
            raw = json.loads(b["raw"])
            tx_rows.append({"at": b["updated_at"], "date": b["date"], "kind": "Bank statement line", "party": b["payee"] or raw.get("description"),
                            "detail": f"{raw.get('description')}" + (f" → {b['account']}" if b["account"] else ""),
                            "amount_inr": -(b["amount_paise"] / 100), "status": b["status"],
                            "counts": b["status"] == "posted", "simulated": False, "line_id": b["line_id"]})
    tx_rows.sort(key=lambda t: (str(t["date"]), t["at"]), reverse=True)
    out = sum(-t["amount_inr"] for t in tx_rows if t["counts"])
    acct["balance_inr"] = round(acct["opening_balance_inr"] - out, 2)
    acct["total_out_inr"] = round(out, 2)
    acct["held_inr"] = round(sum(-t["amount_inr"] for t in tx_rows if not t["counts"]), 2)
    return {"account": acct, "transactions": tx_rows}


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


@app.get("/invoices/{invoice_id}/doc", response_class=HTMLResponse)
def invoice_doc(invoice_id: str):
    """The bill as a document anyone on the team can open, print or save as PDF: what the vendor sent,
    the original message (and PDF) when it came from the inbox, and the desk's decision stamped on it."""
    import html
    from .checks import mask_account
    conn = connect()
    inv = conn.execute("SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
    if not inv:
        raise HTTPException(404)
    raw, reasons = json.loads(inv["raw"]), json.loads(inv["reasons"])
    pay = conn.execute("SELECT * FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
    e = lambda x: html.escape(str(x if x not in (None, "") else "—"))
    orig = raw.get("_original") or {}
    acct = raw.get("bank_account") or {}
    stamp = {"PAID": ("Paid", "paid"), "CANNOT_VERIFY": ("Cannot verify", "bad"), "REJECTED": ("Rejected", "bad"),
             "PAYOUT_FAILED": ("Payout failed", "bad")}.get(inv["state"]) or (("Ready to pay", "ok") if inv["outcome"] == "ready" else ("Held", "bad"))
    rows = [("Vendor", raw.get("vendor_name")), ("GSTIN", raw.get("vendor_gstin")), ("Invoice number", raw.get("invoice_no")),
            ("Invoice date", raw.get("invoice_date")), ("Purchase order", raw.get("po_number")),
            ("Pay to (as written on the bill)", f"{mask_account(acct.get('account_no'))} · {acct.get('ifsc')}" if acct else None),
            ("Sender", orig.get("from") or raw.get("_from") or raw.get("vendor_domain"))]
    pdfs = "".join(f'<p><a href="/docs/{e(p["id"])}.pdf" target="_blank" rel="noopener">Open the original PDF: {e(p["name"])}</a></p>'
                   for p in orig.get("pdfs", []))
    pdfs += "".join(f'<figure><img src="/docs/{e(i["doc"])}" alt="Scanned bill {e(i["name"])}"><figcaption>Scanned image: {e(i["name"])}</figcaption></figure>'
                    for i in orig.get("images", []))
    up = orig.get("upload")
    if up:
        pdfs = f'<p class="meta">Uploaded by {e(up.get("by"))} as {e(up.get("name"))}, read by {e(up.get("reader") or "the desk")}.</p>' + pdfs
    from . import scan
    conn2 = connect()
    checks_ = scan.checklist(conn2, invoice_id)
    atts = scan.attachments(conn2, invoice_id)
    icon = {"pass": "✓", "fail": "✗", "missing": "○", "waiting": "…", "not checked": "–"}
    checklist_html = "<table class='chk'>" + "".join(
        f"<tr class='{c['result'].replace(' ', '-')}'><td>{icon.get(c['result'], '–')}</td><td>{e(c['check'])}<div class='meta'>{e(c['detail'])}</div></td><td>{e(c['result'])}</td></tr>"
        for c in checks_) + "</table>"
    att_html = ("<ul>" + "".join(f"<li>{e(a['kind_label'])}: <a href='/docs/{e(a['doc'])}' target='_blank' rel='noopener'>{e(a['filename'])}</a>"
                                 f" <span class='meta'>added by {e(a['uploaded_by'])}</span></li>" for a in atts) + "</ul>") if atts \
        else "<p class='meta'>No supporting documents yet.</p>"
    kinds = "".join(f"<option value='{k}'>{e(v)}</option>" for k, v in scan.ATTACH_KINDS.items())
    attach_form = f"""<form id="att" class="att"><select name="kind" aria-label="Document type">{kinds}</select>
      <input type="file" name="file" required accept=".pdf,.png,.jpg,.jpeg,.webp,.txt,.eml" aria-label="Document file">
      <button type="submit">Attach document</button><span id="attmsg" class="meta"></span></form>
      <script>document.getElementById('att').addEventListener('submit', async ev => {{ ev.preventDefault();
        const m = document.getElementById('attmsg'); m.textContent = 'Uploading…';
        const r = await fetch(location.pathname.slice(0, -4) + '/attachments', {{method: 'POST', body: new FormData(ev.target)}});
        const b = await r.json().catch(() => ({{}}));
        if (r.ok) location.reload(); else m.textContent = b.detail || 'Upload failed'; }});</script>"""
    original = (f'<h2>Original message</h2><p class="meta">{e(orig.get("subject"))} · received {e(orig.get("date"))}</p>'
                f'<pre>{e(orig.get("text"))}</pre>{pdfs}') if orig else \
        '<h2>Original message</h2><p class="meta">This bill arrived as structured data, not as an email or PDF, so there is no original document to show.</p>'
    why = "".join(f"<li>{e(r['text'])}</li>" for r in reasons)
    paid = (f"<p>Payment {e(pay['provider_ref'] or 'not yet accepted')} via {e(pay['provider'])}"
            f"{' (simulated: no money moved)' if pay['provider'] == 'local_sandbox' else ''}, status {e(pay['provider_status'] or pay['status'])}, "
            f"to the verified account {e(pay['account_masked'])}.</p>") if pay else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<title>Bill {e(raw.get('invoice_no'))} · {e(raw.get('vendor_name'))}</title>
<style>
 :root{{--ink:#14213d;--paper:#fbfcfe;--rule:#d9dee7;--red:#b3261e;--green:#1f6f43;--muted:#5b667a}}
 body{{margin:0;background:#eef1f5;color:var(--ink);font:15px/1.55 "Instrument Sans",system-ui,sans-serif}}
 .page{{max-width:760px;margin:24px auto;background:var(--paper);border:1px solid var(--rule);padding:40px 48px;position:relative}}
 h1{{font-weight:600;font-size:26px;margin:0 0 4px}} h2{{font-size:17px;margin:32px 0 8px}}
 .meta{{color:var(--muted);font-size:13px;margin:0}}
 table{{width:100%;border-collapse:collapse;margin-top:20px}} td{{padding:9px 0;border-bottom:1px solid var(--rule);vertical-align:top}}
 td:first-child{{color:var(--muted);width:42%}} .amount td{{font-size:22px;color:var(--ink);font-variant-numeric:tabular-nums}}
 pre{{white-space:pre-wrap;background:#f1f3f7;padding:14px;border-radius:6px;font:13px/1.5 ui-monospace,Menlo,monospace}}
 .stamp{{position:absolute;top:36px;right:44px;transform:rotate(-8deg);border:3px solid currentColor;border-radius:8px;padding:4px 12px;font-weight:700;font-size:20px;letter-spacing:.04em}}
 .stamp.paid,.stamp.ok{{color:var(--green)}} .stamp.bad{{color:var(--red)}}
 .bar{{max-width:760px;margin:16px auto 0;display:flex;gap:8px;justify-content:flex-end}}
 button,a.btn{{font:inherit;border:1px solid var(--ink);background:#fff;color:var(--ink);border-radius:6px;padding:7px 12px;cursor:pointer;text-decoration:none}}
 .chk{{margin-top:8px}} .chk td{{padding:8px 6px}} .chk td:first-child{{width:24px;font-weight:700}} .chk td:last-child{{width:90px;color:var(--muted);text-align:right}}
 .chk tr.pass td:first-child{{color:var(--green)}} .chk tr.fail td:first-child{{color:var(--red)}} .chk tr.missing td:first-child{{color:#a06f00}}
 figure{{margin:12px 0}} figure img{{max-width:100%;border:1px solid var(--rule);border-radius:6px}} figcaption{{font-size:12px;color:var(--muted)}}
 form.att{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}} select,input[type=file]{{font:inherit;font-size:14px}}
 @media print{{.bar,form.att{{display:none}} body{{background:#fff}} .page{{border:0;margin:0}}}}
 @media (max-width:640px){{.page{{padding:24px 16px}} .stamp{{position:static;display:inline-block;margin-bottom:12px}}}}
</style></head><body>
<div class="bar"><a class="btn" href="/">Back to the desk</a><button onclick="print()">Print or save as PDF</button></div>
<article class="page">
 <span class="stamp {stamp[1]}">{e(stamp[0])}</span>
 <h1>Bill from {e(raw.get('vendor_name'))}</h1>
 <p class="meta">Desk reference {e(invoice_id)}</p>
 <table><tr class="amount"><td>Amount</td><td>₹{(inv['amount_paise'] or 0)/100:,.2f}</td></tr>
 {''.join(f'<tr><td>{e(k)}</td><td>{e(v)}</td></tr>' for k, v in rows)}</table>
 <h2>Payment verification checklist</h2>{checklist_html}
 <h2>What the desk decided</h2><ul>{why}</ul>{paid}
 <h2>Supporting documents</h2>{att_html}{attach_form}
 {original}
</article></body></html>"""


@app.get("/docs/{name}")
def original_doc(name: str):
    """Original bills and supporting documents, by content hash (signed-in users only)."""
    import os
    import re
    from fastapi.responses import FileResponse
    from .inbox import DOCS_DIR
    m = re.fullmatch(r"([0-9a-f]{20})(\.(pdf|png|jpg|jpeg|webp|txt|eml|json))", name)
    if not m:
        raise HTTPException(404)
    path = os.path.join(DOCS_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(404)
    media = {"pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(m.group(3), "text/plain")
    return FileResponse(path, media_type=media)


@app.post("/bills/upload")
async def upload_bills(request: Request, files: list[UploadFile] = File(...)):
    """Scan or upload bills (photo, PDF, .txt, .eml). Each is read, checked and queued for approval."""
    from . import scan
    by = auth.approver_label(request.state.user)
    payload = [(f.filename or "bill", await f.read()) for f in files]
    try:
        with _lock:
            out = scan.process_upload(payload, by, connect())
    except scan.UploadRejected as e:
        raise HTTPException(400, str(e))
    return out


@app.post("/invoices/{invoice_id}/attachments")
async def add_attachment(invoice_id: str, request: Request, kind: str = Form(...), file: UploadFile = File(...)):
    """Attach a supporting document (PO, goods receipt, contract, bank proof) to a bill."""
    from . import scan
    try:
        with _lock:
            conn = connect()
            att = scan.attach(conn, invoice_id, kind, file.filename or "document", await file.read(),
                              auth.approver_label(request.state.user))
    except scan.UploadRejected as e:
        raise HTTPException(400, str(e))
    return att


@app.get("/invoices/{invoice_id}/attachments")
def list_attachments(invoice_id: str):
    from . import scan
    return scan.attachments(connect(), invoice_id)


@app.get("/invoices/{invoice_id}/checklist")
def bill_checklist(invoice_id: str):
    from . import scan
    conn = connect()
    if not conn.execute("SELECT 1 FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone():
        raise HTTPException(404)
    return scan.checklist(conn, invoice_id)


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
            "reader": "claude" if os.environ.get("ANTHROPIC_API_KEY") else "rules + ocr",
            "crustdata_key": bool(os.environ.get("CRUSTDATA_API_KEY"))}


@app.get("/state")
def state():
    conn = connect()
    rows = conn.execute("SELECT i.invoice_id, i.raw, i.state, i.outcome, i.reasons, i.updated_at, i.amount_paise, p.provider_status, p.provider_ref "
                        "FROM invoices i LEFT JOIN payments p ON p.invoice_id=i.invoice_id ORDER BY i.rowid DESC LIMIT 200").fetchall()
    return [{"invoice_id": r["invoice_id"], "raw": {k: v for k, v in json.loads(r["raw"]).items() if k not in ("bank_account", "_original", "_evidence")},
             "state": r["state"], "outcome": r["outcome"], "updated_at": r["updated_at"],
             "payout_status": r["provider_status"], "payout_id": r["provider_ref"], "amount_paise": r["amount_paise"],
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
