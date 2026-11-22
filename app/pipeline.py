"""The payables desk as a resumable state machine.

Per invoice:
  RECEIVED -> VENDOR_OK -> IDENTITY_OK -> CHECKED -> AWAITING_APPROVAL
           -> APPROVED -> PAYING -> PAID
Terminal: PAID | HELD_FINAL | CANNOT_VERIFY | REJECTED

Every transition is one SQLite transaction. Kill the process anywhere and
`resume()` continues from the last committed state. Network calls happen
outside transactions; their results are cached, so a restart re-reads them
for free instead of paying for them again.
"""
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone

from . import checks, vendors as vendor_mod
from .crustdata import BudgetExceeded, Crustdata, SourceUnavailable
from .db import connect, log_event, tx
from .payments import ProviderNotConfigured, get_provider

TERMINAL = {"PAID", "CANNOT_VERIFY", "REJECTED"}
WAITING = {"AWAITING_APPROVAL"}
DEFAULT_COST_LIMIT = float(os.environ.get("PAYDESK_COST_LIMIT", "10"))  # Crustdata credits per run
DEFAULT_TIME_LIMIT = int(os.environ.get("PAYDESK_TIME_LIMIT", "600"))  # seconds per run
STEP_DELAY = float(os.environ.get("PAYDESK_STEP_DELAY", "0"))  # slow down for the kill-on-stage demo


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- submission

def submit(request: dict, conn=None) -> str:
    """Store a batch. The same batch submitted twice is the same run (no double work)."""
    conn = conn or connect()
    blob = json.dumps(request, sort_keys=True)
    run_id = "run_" + hashlib.sha256(blob.encode()).hexdigest()[:12]
    limits = request.get("limits") or {}
    with tx(conn):
        if conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
            return run_id
        conn.execute(
            "INSERT INTO runs(run_id, request, status, cost_limit, time_limit_s, started_at) VALUES (?,?,?,?,?,?)",
            (run_id, blob, "running", float(limits.get("cost_credits", DEFAULT_COST_LIMIT)),
             int(limits.get("time_s", DEFAULT_TIME_LIMIT)), time.time()),
        )
        for i, inv in enumerate(request.get("invoices", [])):
            conn.execute(
                "INSERT INTO invoices(invoice_id, run_id, line_no, raw, state, updated_at) VALUES (?,?,?,?,?,?)",
                (f"{run_id}:{i}", run_id, i, json.dumps(inv), "RECEIVED", time.time()),
            )
        log_event(conn, run_id, None, "run_submitted", {"invoices": len(request.get("invoices", []))})
    return run_id


# ---------------------------------------------------------------- helpers

def _set(conn, inv_id, state, outcome=None, reason=None, **cols):
    row = conn.execute("SELECT reasons, run_id FROM invoices WHERE invoice_id=?", (inv_id,)).fetchone()
    reasons = json.loads(row["reasons"])
    if reason:
        reasons.append(reason)
    sets = ["state=?", "outcome=?", "reasons=?", "updated_at=?"] + [f"{k}=?" for k in cols]
    vals = [state, outcome, json.dumps(reasons), time.time()] + list(cols.values()) + [inv_id]
    conn.execute(f"UPDATE invoices SET {', '.join(sets)} WHERE invoice_id=?", vals)
    log_event(conn, row["run_id"], inv_id, "state", {"to": state, "outcome": outcome, "reason": reason})


def _hold(conn, inv_id, code, text, source_ids=()):
    """A hold is not final: it goes to a person, who approves (override) or rejects."""
    _set(conn, inv_id, "AWAITING_APPROVAL", "held",
         {"code": code, "text": text, "source_ids": list(source_ids)})
    conn.execute("INSERT OR IGNORE INTO approvals(invoice_id) VALUES (?)", (inv_id,))


def _cannot_verify(conn, inv_id, code, text, missing):
    _set(conn, inv_id, "CANNOT_VERIFY", "cannot_verify",
         {"code": code, "text": text, "missing": missing})


def _source(conn, inv_id, fact, url, field, value, record_date=None, retrieved_at=None):
    cur = conn.execute(
        "INSERT INTO sources(invoice_id, fact, url, field, value, record_date, retrieved_at) VALUES (?,?,?,?,?,?,?)",
        (inv_id, fact, url, field, str(value), record_date, retrieved_at or now_iso()),
    )
    return cur.lastrowid


def _credits_used(conn, run_id):
    return conn.execute("SELECT COALESCE(SUM(credits),0) c FROM ledger WHERE run_id=?", (run_id,)).fetchone()["c"]


# ---------------------------------------------------------------- steps

def step_vendor(conn, inv_id, inv, vendor_list):
    """Gate 1: the payee must be a vendor the Trust service has CLEARED."""
    vendor, how = vendor_mod.find_vendor(inv, vendor_list)
    with tx(conn):
        if not vendor:
            _cannot_verify(conn, inv_id, "no_vendor_record", f"Payee '{inv.get('vendor_name')}': {how}.",
                           missing="a cleared vendor record from Trust & Compliance for this payee")
            return
        # what the invoice itself says, with the exact quote from the inbox message
        for ev in inv.get("_evidence", []):
            _source(conn, inv_id, f"invoice {ev.get('field')}", f"inbox:{inv.get('_message', 'request')}",
                    ev.get("field", ""), ev.get("quote", ""), None, None)
        sid = _source(conn, inv_id, f"vendor record status = {vendor.get('status')}",
                      "trust-service:vendors/" + str(vendor.get("vendor_id")), "status", vendor.get("status"),
                      vendor.get("verified_at"))
        try:
            amount = checks.to_paise(inv.get("amount"))
        except ValueError as e:
            _cannot_verify(conn, inv_id, "bad_amount", str(e), missing="a readable invoice amount")
            return
        cols = dict(vendor_id=str(vendor.get("vendor_id")),
                    invoice_no_norm=checks.norm_invoice_no(inv.get("invoice_no")),
                    amount_paise=amount,
                    invoice_date=checks.norm_date(inv.get("invoice_date")))
        status = (vendor.get("status") or "").lower()
        if status != "cleared":
            _set(conn, inv_id, "RECEIVED", None, None, **cols)
            _hold(conn, inv_id, "vendor_not_cleared",
                  f"Vendor {vendor.get('legal_name')} is '{status}' in Trust & Compliance, not cleared.", [sid])
            return
        _set(conn, inv_id, "VENDOR_OK", None,
             {"code": "vendor_cleared", "text": f"Matched vendor {vendor.get('legal_name')} by {how.replace('_', ' ')}.",
              "source_ids": [sid]}, **cols)


def step_identity(conn, inv_id, inv, vendor, cd: Crustdata):
    """Live data: the company behind the invoice's domain must be the same
    company the vendor was cleared as (catches lookalike names)."""
    inv_domain = vendor_mod.norm_domain(inv.get("vendor_domain") or "")
    expected_id = vendor.get("crustdata_company_id")
    if not inv_domain or not expected_id:
        with tx(conn):
            _set(conn, inv_id, "IDENTITY_OK", None,
                 {"code": "identity_by_gstin",
                  "text": "No invoice domain to check against Crustdata; identity rests on the GSTIN match."})
        return
    try:
        matches, fetched = cd.identify_domain(inv_domain, inv_id)
    except SourceUnavailable as e:
        with tx(conn):
            _hold(conn, inv_id, "source_unavailable", f"Crustdata did not answer ({e}); held rather than guessed.")
        return
    with tx(conn):
        url = "https://api.crustdata.com/company/identify"
        # Only trust a match whose record actually lists this domain (fuzzy matches on
        # generic hosts like *.business.site return unrelated companies).
        exact = [m for m in matches
                 if inv_domain in {vendor_mod.norm_domain(x) for x in
                                   (m["company_data"]["basic_info"].get("all_domains") or [])
                                   + [m["company_data"]["basic_info"].get("primary_domain") or ""]}]
        if not exact:
            _hold(conn, inv_id, "domain_unknown",
                  f"Crustdata has no company that owns the invoice domain {inv_domain}; "
                  f"cannot confirm the sender is the cleared vendor.")
            return
        top = exact[0]["company_data"]["basic_info"]
        sid = _source(conn, inv_id, f"{inv_domain} belongs to {top.get('name')}", url,
                      f"domains=[{inv_domain}] -> basic_info.crustdata_company_id",
                      top.get("crustdata_company_id"), None, fetched)
        if str(top.get("crustdata_company_id")) != str(expected_id):
            _hold(conn, inv_id, "lookalike_payee",
                  f"Invoice domain {inv_domain} resolves to '{top.get('name')}' "
                  f"(id {top.get('crustdata_company_id')}), not the cleared vendor (id {expected_id}).", [sid])
            return
        _set(conn, inv_id, "IDENTITY_OK", None,
             {"code": "identity_confirmed", "text": f"{inv_domain} is {top.get('name')}, the cleared vendor.",
              "source_ids": [sid]})


def step_checks(conn, inv_id, inv, vendor, pos, bank_changes=(), vendor_list=()):
    with tx(conn):
        row = conn.execute("SELECT * FROM invoices WHERE invoice_id=?", (inv_id,)).fetchone()
        verified = vendor.get("verified_bank_account")
        if not verified:
            _cannot_verify(conn, inv_id, "no_verified_account", "Vendor has no verified bank account on file.",
                           missing="a bank account verified by Trust & Compliance")
            return
        problems = []  # run every check, report every reason (most severe first)
        # 0. an email asked to change this vendor's bank details: never act on it, re-verify first
        for bc in bank_changes:
            v, _ = vendor_mod.find_vendor(bc, vendor_list)
            if v and str(v.get("vendor_id")) == str(vendor.get("vendor_id")):
                problems.append(("bank_change_requested",
                                 f"Message {bc.get('message')} from {bc.get('from') or 'unknown sender'} asks to pay "
                                 f"{checks.mask_account(bc.get('new_account_no'))} (IFSC {bc.get('new_ifsc')}) instead. "
                                 f"Bank details are only changed through Trust & Compliance re-verification, never by email."))
                break
        # 1. bank details must be the verified ones
        inv_acct = inv.get("bank_account")
        if inv_acct and not checks.same_account(inv_acct, verified):
            problems.append(("bank_details_changed",
                             f"Invoice asks for payment to {checks.mask_account(inv_acct.get('account_no'))} "
                             f"(IFSC {inv_acct.get('ifsc')}), not the verified account "
                             f"{checks.mask_account(verified.get('account_no'))}. Send the vendor back to "
                             f"Trust & Compliance for re-verification; do not pay from an email."))
        # 2. duplicate
        for kind, other, other_state in checks.find_duplicates(conn, inv_id, row["vendor_id"], row["invoice_no_norm"],
                                                               row["amount_paise"], row["invoice_date"])[:1]:
            problems.append(("possible_duplicate",
                             f"Possible duplicate of {other} ({kind.replace('_', ' ')}; that one is {other_state})."))
        # 3. purchase order
        ok, why = checks.match_po(inv, pos, row["amount_paise"])
        if not ok:
            problems.append(("po_mismatch", why))
        if problems:
            for code, text in problems[1:]:
                _set(conn, inv_id, row["state"], None, {"code": code, "text": text})
            _hold(conn, inv_id, problems[0][0], problems[0][1])
            return
        _set(conn, inv_id, "AWAITING_APPROVAL", "ready",
             {"code": "checks_passed", "text": f"No duplicate; verified account; {why}."})
        conn.execute("INSERT OR IGNORE INTO approvals(invoice_id) VALUES (?)", (inv_id,))


def step_approval(conn, inv_id):
    with tx(conn):
        a = conn.execute("SELECT decision, decided_by FROM approvals WHERE invoice_id=?", (inv_id,)).fetchone()
        if not a or not a["decision"]:
            return False
        if a["decision"] == "rejected":
            _set(conn, inv_id, "REJECTED", "rejected", {"code": "rejected_by_person", "text": f"Rejected by {a['decided_by']}."})
        else:
            _set(conn, inv_id, "APPROVED", None, {"code": "approved_by_person", "text": f"Approved by {a['decided_by']}."})
        return True


def step_pay(conn, inv_id, vendor, provider):
    row = conn.execute("SELECT * FROM invoices WHERE invoice_id=?", (inv_id,)).fetchone()
    # Hard guard, even after a person's approval: never pay an uncleared vendor or an unverified account.
    if not vendor or (vendor.get("status") or "").lower() != "cleared" or not vendor.get("verified_bank_account"):
        with tx(conn):
            _cannot_verify(conn, inv_id, "not_payable", "Approved, but the vendor is not cleared or has no verified account.",
                           missing="vendor cleared by Trust & Compliance with a verified bank account")
        return
    acct = vendor["verified_bank_account"]
    # (1) write the idempotency key first — exactly once per invoice, ever.
    with tx(conn):
        p = conn.execute("SELECT * FROM payments WHERE invoice_id=?", (inv_id,)).fetchone()
        if not p:
            conn.execute(
                "INSERT INTO payments(invoice_id, idempotency_key, provider, status, amount_paise, account_masked, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (inv_id, str(uuid.uuid4()), provider.name, "pending", row["amount_paise"],
                 checks.mask_account(acct.get("account_no")), time.time(), time.time()),
            )
            _set(conn, inv_id, "PAYING", None, None)
        p = conn.execute("SELECT * FROM payments WHERE invoice_id=?", (inv_id,)).fetchone()
    if p["status"] == "paid":
        with tx(conn):
            _set(conn, inv_id, "PAID", "paid", None)
        return
    # (2) call the provider with the SAME key (a retry after a crash returns the first payout)
    t0 = time.time()
    ref = provider.pay(p["idempotency_key"], p["amount_paise"], vendor["legal_name"], acct, inv_id)
    # (3) record the result
    with tx(conn):
        conn.execute("UPDATE payments SET status='paid', provider_ref=?, updated_at=? WHERE invoice_id=?",
                     (ref, time.time(), inv_id))
        conn.execute("INSERT INTO ledger(run_id, invoice_id, service, endpoint, latency_ms, status, at) VALUES (?,?,?,?,?,?,?)",
                     (row["run_id"], inv_id, "payment", provider.name, int((time.time() - t0) * 1000), "paid", time.time()))
        _set(conn, inv_id, "PAID", "paid",
             {"code": "paid", "text": f"Paid ₹{p['amount_paise']/100:,.2f} to {vendor['legal_name']} "
                                      f"{p['account_masked']} ({provider.name} ref {ref})."})


# ---------------------------------------------------------------- driver

def process(run_id: str, conn=None, provider=None, cd=None, vendor_list=None) -> dict:
    """Advance every invoice in the run as far as it can go without a person."""
    conn = conn or connect()
    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    req = json.loads(run["request"])
    pos = req.get("purchase_orders", [])
    vendor_list = vendor_list if vendor_list is not None else vendor_mod.load_vendors()
    by_id = {str(v.get("vendor_id")): v for v in vendor_list}
    if cd is None:
        cd = Crustdata(conn, run_id, budget=lambda: run["cost_limit"] - _credits_used(conn, run_id))
    if provider is None:
        try:
            provider = get_provider()
        except ProviderNotConfigured as e:
            provider, provider_err = None, str(e)
    deadline = run["started_at"] + run["time_limit_s"]

    for row in conn.execute("SELECT * FROM invoices WHERE run_id=? ORDER BY line_no", (run_id,)).fetchall():
        inv_id = row["invoice_id"]
        inv = json.loads(row["raw"])
        while True:
            state = conn.execute("SELECT state FROM invoices WHERE invoice_id=?", (inv_id,)).fetchone()["state"]
            if state in TERMINAL:
                break
            if time.time() > deadline and state not in ("PAYING", "APPROVED", "AWAITING_APPROVAL"):
                with tx(conn):
                    _hold(conn, inv_id, "time_limit", "Run time limit reached before this invoice was checked.")
                break
            if STEP_DELAY:
                time.sleep(STEP_DELAY)
            vendor = by_id.get(conn.execute("SELECT vendor_id FROM invoices WHERE invoice_id=?", (inv_id,)).fetchone()["vendor_id"])
            try:
                if state == "RECEIVED":
                    step_vendor(conn, inv_id, inv, vendor_list)
                elif state == "VENDOR_OK":
                    step_identity(conn, inv_id, inv, vendor, cd)
                elif state == "IDENTITY_OK":
                    step_checks(conn, inv_id, inv, vendor, pos, req.get("bank_change_requests", []), vendor_list)
                elif state == "AWAITING_APPROVAL":
                    if not step_approval(conn, inv_id):
                        break  # waiting for a person
                elif state in ("APPROVED", "PAYING"):
                    if provider is None:
                        with tx(conn):
                            log_event(conn, run_id, inv_id, "payment_blocked", {"why": provider_err})
                        break
                    step_pay(conn, inv_id, vendor, provider)
                else:
                    break
            except BudgetExceeded as e:
                with tx(conn):
                    _hold(conn, inv_id, "cost_limit", f"Cost limit reached: {e}")
                break
    with tx(conn):
        open_ = conn.execute("SELECT COUNT(*) c FROM invoices WHERE run_id=? AND state NOT IN ('PAID','CANNOT_VERIFY','REJECTED')",
                             (run_id,)).fetchone()["c"]
        if open_ == 0:
            conn.execute("UPDATE runs SET status='done', finished_at=? WHERE run_id=?", (time.time(), run_id))
    return summary(run_id, conn)


def summary(run_id, conn=None):
    conn = conn or connect()
    rows = conn.execute("SELECT invoice_id, line_no, state, outcome, reasons FROM invoices WHERE run_id=? ORDER BY line_no",
                        (run_id,)).fetchall()
    return {
        "run_id": run_id,
        "credits_used": _credits_used(conn, run_id),
        "invoices": [
            {"invoice_id": r["invoice_id"], "line": r["line_no"], "state": r["state"], "outcome": r["outcome"],
             "reasons": json.loads(r["reasons"])}
            for r in rows
        ],
    }


def resume_all(conn=None):
    conn = conn or connect()
    out = []
    for r in conn.execute("SELECT run_id FROM runs WHERE status='running'").fetchall():
        out.append(process(r["run_id"], conn))
    return out


def decide(invoice_id: str, decision: str, by: str, note: str = "", conn=None):
    """A person approves or rejects. They cannot edit results."""
    assert decision in ("approved", "rejected")
    conn = conn or connect()
    with tx(conn):
        row = conn.execute("SELECT state, run_id FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
        if not row or row["state"] != "AWAITING_APPROVAL":
            raise ValueError(f"{invoice_id} is not waiting for approval")
        conn.execute("UPDATE approvals SET decision=?, decided_by=?, decided_at=?, note=? WHERE invoice_id=? AND decision IS NULL",
                     (decision, by, time.time(), note, invoice_id))
        log_event(conn, row["run_id"], invoice_id, "decision", {"decision": decision, "by": by, "note": note})
