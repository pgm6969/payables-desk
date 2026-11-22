"""Bank-statement posting: "Post these bank lines to the correct accounts."

For each line:
  1. Duplicate: same date, amount and payee descriptor as an earlier line -> HELD (possible duplicate charge).
  2. Payee:
     a. the client's vendor master (legal name, or a bank descriptor the client recorded for that vendor);
     b. else Crustdata company identify by the cleaned descriptor, accepted ONLY when one large company
        clearly owns the name (domain on record, 501+ people, no other sizeable company with that name);
     c. else CANNOT VERIFY: no source identifies the payee. Never a guess.
  3. Account: the vendor's default account, else the client's chart-of-accounts keywords matched against
     the company's own description; if nothing matches -> HELD for a person to classify.
Posting is a journal entry (reversible); nothing is paid here.
"""
import hashlib
import json
import re
import time
from datetime import datetime, timezone

from . import checks, config, vendors as vendor_mod
from .crustdata import BudgetExceeded, Crustdata, SourceUnavailable
from .db import connect, tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS bank_lines (
    line_id     TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL,
    line_no     INTEGER NOT NULL,
    raw         TEXT NOT NULL,
    date        TEXT,
    amount_paise INTEGER,
    descriptor  TEXT,
    payee       TEXT,
    account     TEXT,
    status      TEXT NOT NULL,           -- new | posted | held | cannot_verify
    reasons     TEXT NOT NULL DEFAULT '[]',
    updated_at  REAL NOT NULL
);
"""

PREFIX = re.compile(r"^(RAZ|RZP|PAYTM|PYTM|UPI|POS|NEFT|IMPS|RTGS|ACH|ECS|NACH|SI|BIL|BILLDESK|PAYU|CCAVENUE|CC|DC|VISA|MC)[\*/\-: ]+")
NOISE = {"BLR", "BANGALORE", "BENGALURU", "MUM", "MUMBAI", "BOM", "DEL", "DELHI", "NEW", "NOIDA", "GGN", "GURGAON",
         "GURUGRAM", "HYD", "HYDERABAD", "CHENNAI", "MAA", "PUNE", "KOLKATA", "CCU", "IN", "IND", "INDIA", "EMEA",
         "APAC", "US", "UK", "PVT", "LTD", "PRIVATE", "LIMITED", "CO", "COM", "WWW", "ONLINE", "PAYMENT", "PAYMENTS"}
ABBREV = {"TRD": "TRADERS", "TRDRS": "TRADERS", "ENT": "ENTERPRISES", "ENTP": "ENTERPRISES", "SVCS": "SERVICES",
          "SVC": "SERVICES", "INDS": "INDUSTRIES", "INTL": "INTERNATIONAL", "TECH": "TECHNOLOGIES"}
SIZES = ["myself only", "1-10", "2-10", "11-50", "51-200", "201-500", "501-1000", "1001-5000", "5001-10000", "10001+"]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_descriptor(desc: str) -> str:
    s = (desc or "").upper().strip()
    for _ in range(3):
        s = PREFIX.sub("", s)
    toks = [ABBREV.get(t, t) for t in re.split(r"[^A-Z0-9&]+", s) if t]
    toks = [t for t in toks if t not in NOISE and not t.isdigit() and len(t) > 1]
    return " ".join(toks)


def size_rank(r):
    try:
        return SIZES.index((r or "").strip())
    except ValueError:
        return -1


def from_vendor_master(cleaned: str, vendors: list):
    for v in vendors:
        names = [v.get("legal_name", "")] + list(v.get("bank_descriptors") or [])
        for n in names:
            cn = clean_descriptor(n)
            if cn and (cn == cleaned or (len(cn) >= 4 and cleaned.startswith(cn)) or (len(cleaned) >= 4 and cn.startswith(cleaned))):
                return v, n
    return None, None


def dominant_company(matches: list):
    """Return (company, None) when one large company clearly owns the name, else (None, why)."""
    strong = [m for m in matches if (m.get("confidence_score") or 0) >= 0.9]
    if not strong:
        return None, "no confident match for this name"
    big = [m for m in strong if m["company_data"]["basic_info"].get("primary_domain")
           and size_rank(m["company_data"]["basic_info"].get("employee_count_range")) >= SIZES.index("501-1000")]
    if len(big) == 1:
        others = [m for m in strong if m is not big[0] and size_rank(m["company_data"]["basic_info"].get("employee_count_range")) >= SIZES.index("51-200")]
        if not others:
            return big[0]["company_data"]["basic_info"], None
    names = {m["company_data"]["basic_info"].get("name") for m in strong}
    return None, f"{len(strong)} different businesses match this name ({', '.join(sorted(n for n in names if n)[:4])}); none clearly owns it"


def account_for(text: str, vendor: dict | None):
    if vendor and vendor.get("default_account"):
        return vendor["default_account"], "vendor master default_account"
    low = (text or "").lower()
    for account, words in config.get("chart_of_accounts").items():
        for w in words:
            if re.search(rf"\b{re.escape(w)}", low):
                return account, f"keyword '{w}' in the company's description"
    return None, None


def submit(request: dict, conn=None) -> str:
    conn = conn or connect()
    conn.executescript(SCHEMA)
    blob = json.dumps(request, sort_keys=True)
    batch = "bank_" + hashlib.sha256(blob.encode()).hexdigest()[:12]
    with tx(conn):
        if conn.execute("SELECT 1 FROM bank_lines WHERE batch_id=?", (batch,)).fetchone():
            return batch
        for i, line in enumerate(request.get("bank_lines", [])):
            conn.execute("INSERT INTO bank_lines(line_id, batch_id, line_no, raw, status, updated_at) VALUES (?,?,?,?,?,?)",
                         (f"{batch}:{i}", batch, i, json.dumps(line), "new", time.time()))
    return batch


def _finish(conn, line_id, status, reasons, **cols):
    sets = ["status=?", "reasons=?", "updated_at=?"] + [f"{k}=?" for k in cols]
    conn.execute(f"UPDATE bank_lines SET {', '.join(sets)} WHERE line_id=?",
                 [status, json.dumps(reasons), time.time()] + list(cols.values()) + [line_id])


def _src(conn, line_id, fact, url, field, value, retrieved_at=None):
    conn.execute("INSERT INTO sources(invoice_id, fact, url, field, value, record_date, retrieved_at) VALUES (?,?,?,?,?,?,?)",
                 (line_id, fact, url, field, str(value), None, retrieved_at or now_iso()))


def process(batch: str, conn=None, cd=None, vendor_list=None) -> dict:
    conn = conn or connect()
    conn.executescript(SCHEMA)
    vendor_list = vendor_list if vendor_list is not None else vendor_mod.load_vendors()
    limit = config.get("bank_cost_limit_credits")
    if cd is None:
        spent = lambda: conn.execute("SELECT COALESCE(SUM(credits),0) c FROM ledger WHERE run_id=?", (batch,)).fetchone()["c"]
        cd = Crustdata(conn, batch, budget=lambda: limit - spent())
    for row in conn.execute("SELECT * FROM bank_lines WHERE batch_id=? AND status='new' ORDER BY line_no", (batch,)).fetchall():
        line = json.loads(row["raw"])
        lid = row["line_id"]
        try:
            amount = checks.to_paise(line.get("amount"))
        except ValueError:
            with tx(conn):
                _finish(conn, lid, "cannot_verify", [{"code": "bad_amount", "text": f"Cannot read amount {line.get('amount')!r}."}])
            continue
        date = checks.norm_date(line.get("date")) or line.get("date")
        cleaned = clean_descriptor(line.get("description"))
        # 1. duplicate: same date, amount, descriptor as an earlier line
        dup = conn.execute("SELECT line_id FROM bank_lines WHERE date=? AND amount_paise=? AND descriptor=? AND line_id<>? "
                           "AND rowid < (SELECT rowid FROM bank_lines WHERE line_id=?)", (date, amount, cleaned, lid, lid)).fetchone()
        if dup:
            with tx(conn):
                _finish(conn, lid, "held", [{"code": "possible_duplicate", "text": f"Same payee, amount and date as {dup['line_id']}. Held for approval as a possible duplicate charge."}],
                        date=date, amount_paise=amount, descriptor=cleaned)
            continue
        # 2a. client's vendor master
        v, via = from_vendor_master(cleaned, vendor_list)
        payee, desc_text, reasons = None, "", []
        if v:
            payee = v["legal_name"]
            with tx(conn):
                _src(conn, lid, f"payee = {payee}", "client:vendor_master/" + str(v.get("vendor_id")),
                     "legal_name" if via == v.get("legal_name") else "bank_descriptors", via)
            reasons.append({"code": "payee_vendor_master", "text": f"Payee {payee}: descriptor '{line.get('description')}' matches the client's vendor master ({via})."})
        elif cleaned:
            # 2b. Crustdata, only when one large company clearly owns the name
            try:
                data, at = cd.post("/company/identify", {"names": [cleaned.title()]}, lid)
            except (SourceUnavailable, BudgetExceeded) as e:
                with tx(conn):
                    _finish(conn, lid, "held", [{"code": "source_unavailable", "text": f"Payee lookup failed ({e}); held, not guessed."}],
                            date=date, amount_paise=amount, descriptor=cleaned)
                continue
            company, why = dominant_company((data[0].get("matches") if data else None) or [])
            if company:
                payee = company.get("name")
                desc_text = " ".join([company.get("description") or ""] + list(company.get("industries") or []))
                with tx(conn):
                    _src(conn, lid, f"payee = {payee}", "https://api.crustdata.com/company/identify",
                         f"names=[{cleaned.title()}] -> basic_info.name (domain {company.get('primary_domain')}, {company.get('employee_count_range')} people)",
                         payee, at)
                reasons.append({"code": "payee_identified", "text": f"Payee {payee} ({company.get('primary_domain')}): the only large company with this name in Crustdata."})
            else:
                with tx(conn):
                    _finish(conn, lid, "cannot_verify", [{"code": "payee_unknown", "text": f"No source identifies the payee for '{line.get('description')}': {why}. Held for a person.",
                                                          "missing": "a record tying this descriptor to one business (e.g. the client's vendor master)"}],
                            date=date, amount_paise=amount, descriptor=cleaned)
                continue
        else:
            with tx(conn):
                _finish(conn, lid, "cannot_verify", [{"code": "payee_unknown", "text": "Empty descriptor: nothing identifies the payee."}],
                        date=date, amount_paise=amount, descriptor=cleaned)
            continue
        # 3. account
        account, how = account_for(desc_text, v)
        with tx(conn):
            if account:
                _src(conn, lid, f"account = {account}", "client:chart_of_accounts", how, account)
                reasons.append({"code": "posted", "text": f"Posted to {account} ({how})."})
                _finish(conn, lid, "posted", reasons, date=date, amount_paise=amount, descriptor=cleaned, payee=payee, account=account)
            else:
                reasons.append({"code": "account_unknown", "text": f"Payee {payee} identified, but no account rule fits. Held for a person to classify."})
                _finish(conn, lid, "held", reasons, date=date, amount_paise=amount, descriptor=cleaned, payee=payee)
    return summary(batch, conn)


def summary(batch, conn=None):
    conn = conn or connect()
    conn.executescript(SCHEMA)
    rows = conn.execute("SELECT * FROM bank_lines WHERE batch_id=? ORDER BY line_no", (batch,)).fetchall()
    return {"batch_id": batch, "lines": [{"line_id": r["line_id"], "raw": json.loads(r["raw"]), "status": r["status"], "payee": r["payee"],
                                          "account": r["account"], "reasons": json.loads(r["reasons"])} for r in rows]}


def all_lines(conn=None):
    conn = conn or connect()
    conn.executescript(SCHEMA)
    rows = conn.execute("SELECT * FROM bank_lines ORDER BY rowid DESC LIMIT 200").fetchall()
    return [{"line_id": r["line_id"], "raw": json.loads(r["raw"]), "status": r["status"], "payee": r["payee"],
             "account": r["account"], "reasons": json.loads(r["reasons"])} for r in rows]
