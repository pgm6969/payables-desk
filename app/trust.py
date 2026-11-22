"""Vendor check (Trust & Compliance 5.1 + 5.2) on live Crustdata data.

5.1  Is this a real, active company, and is it run by the director we were told?
     - resolve the company by DOMAIN (a name alone is ambiguous), keep only a
       record that lists the domain
     - company profile: name, status, year founded, headcount, HQ, leaders
     - website: live, and its page title names the company
     - director: named among the company's founders / CXOs / decision makers
5.2  Adverse media: directors screened against the news, namesakes cleared.
     - company: only serious legal terms about the company itself
     - director: an article counts only if it names the person AND the company;
       otherwise it is a namesake, cleared, and kept only as a count + reason
       (no personal data stored about people who are not our vendor's director)
     - a matching article is a LEAD for a person to read, never a finding

Verdict: CLEAR | HOLD (flags) | CANNOT_VERIFY (names what is missing).
Cached per vendor, so later invoices from the same vendor cost nothing.
"""
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from .crustdata import BudgetExceeded, SourceUnavailable

ENRICH = "https://api.crustdata.com/company/enrich"
IDENTIFY = "https://api.crustdata.com/company/identify"
FETCH = "https://api.crustdata.com/web/enrich/live"
SEARCH = "https://api.crustdata.com/web/search/live"

# Company-level: only events where the company itself is the subject.
COMPANY_TERMS = ["gst evasion", "tax evasion", "money laundering", "nclt", "insolvency", "liquidation",
                 "winding up", "wilful defaulter", "ed raid", "income tax raid", "it raid", "sebi order",
                 "shell company", "bogus firm", "fake invoices", "blacklisted", "debarred"]
# Person-level: counts only together with the company name in the same article.
PERSON_TERMS = COMPANY_TERMS + ["arrested", "arrest", "fir", "chargesheet", "fraud", "cheating", "embezzlement",
                                "forgery", "convicted", "raid"]
SUFFIXES = {"pvt", "private", "ltd", "limited", "llp", "inc", "co", "company", "corp", "corporation", "the", "india"}
MIN_AGE_YEARS = 1
MIN_HEADCOUNT = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS vendor_checks (
    vendor_key TEXT PRIMARY KEY,
    report     TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tokens(name: str) -> set:
    return {t for t in re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split() if t not in SUFFIXES and len(t) > 1}


def mentions(text: str, name: str) -> bool:
    """All distinctive tokens of the name appear in the text."""
    t = tokens(name)
    low = (text or "").lower()
    return bool(t) and all(re.search(rf"\b{re.escape(x)}\b", low) for x in t)


def norm_domain(s: str) -> str:
    s = re.sub(r"^https?://", "", (s or "").lower().strip()).split("/")[0]
    return s[4:] if s.startswith("www.") else s


def real_url(u: str) -> str:
    """News results come as google.com/goto?url=... ; cite the article itself."""
    q = parse_qs(urlparse(u or "").query)
    return q.get("url", [u])[0]


class Report:
    def __init__(self, request):
        self.request = request
        self.facts, self.flags, self.missing = [], [], []
        self.screening = {"hits_found": 0, "hits_cleared": 0, "hits_escalated": 0, "cleared_reasons": []}
        self.entity = None
        self.credits = 0.0

    def fact(self, claim, value, url, field, record_date=None, retrieved_at=None):
        self.facts.append({"fact": claim, "value": str(value), "url": url, "field": field,
                           "record_date": record_date, "retrieved_at": retrieved_at or now_iso()})

    def flag(self, severity, code, text, url=None):
        self.flags.append({"severity": severity, "code": code, "text": text, "url": url})

    def verdict(self):
        if self.missing and not self.entity:
            return "CANNOT_VERIFY"
        if any(f["severity"] == "red" for f in self.flags) or self.flags:
            return "HOLD"
        if self.missing:
            return "CANNOT_VERIFY"
        return "CLEAR"

    def to_dict(self):
        v = self.verdict()
        name = (self.entity or {}).get("name") or self.request.get("legal_name")
        if v == "CLEAR":
            summary = f"{name} is a real, active company" + (
                f", founded {self.entity.get('year_founded')}" if (self.entity or {}).get("year_founded") else "") + (
                f", run by {self.request.get('director')}" if self.request.get("director") else "") + (
                "; no adverse media about the company or its director." if self.request.get("director")
                else "; no adverse media about the company (no director given to screen).")
        elif v == "HOLD":
            summary = "; ".join(f["text"] for f in self.flags)
        else:
            summary = "Cannot verify: " + "; ".join(self.missing)
        return {"verdict": v, "summary": summary, "entity": self.entity, "facts": self.facts, "flags": self.flags,
                "cannot_verify": self.missing, "screening": self.screening,
                "cost": {"credits": round(self.credits, 2)}, "checked_at": now_iso(), "request": self.request}


def _post(cd, endpoint, body, rep, job):
    before = cd.conn.execute("SELECT COALESCE(SUM(credits),0) c FROM ledger WHERE invoice_id IS ?", (job,)).fetchone()["c"]
    data, at = cd.post(endpoint, body, job)
    after = cd.conn.execute("SELECT COALESCE(SUM(credits),0) c FROM ledger WHERE invoice_id IS ?", (job,)).fetchone()["c"]
    rep.credits += after - before
    return data, at


def check_vendor(cd, request: dict) -> dict:
    """request: {legal_name, domain, director?, gstin?, vendor_id?}"""
    rep = Report(request)
    job = "trust:" + str(request.get("vendor_id") or request.get("domain") or request.get("legal_name"))
    domain = norm_domain(request.get("domain") or "")
    name = request.get("legal_name") or ""

    # 1. resolve by domain; keep only a record that lists the domain
    if not domain:
        data, at = _post(cd, "/company/identify", {"names": [name]}, rep, job)
        cands = {(m["company_data"]["basic_info"].get("crustdata_company_id"), m["company_data"]["basic_info"].get("name"))
                 for m in (data[0].get("matches") or [])} if data else set()
        rep.missing.append("a website domain to resolve the company by" + (
            f"; the name alone matches {len(cands)} different companies" if len(cands) > 1 else ""))
        return rep.to_dict()
    data, at = _post(cd, "/company/identify", {"domains": [domain]}, rep, job)
    matches = (data[0].get("matches") if data else None) or []
    owned = [m for m in matches if domain in {norm_domain(x) for x in
             (m["company_data"]["basic_info"].get("all_domains") or []) + [m["company_data"]["basic_info"].get("primary_domain") or ""]}]
    if not owned:
        rep.missing.append(f"a company record that owns the domain {domain} (a front company often has none)")
        return rep.to_dict()
    cid = owned[0]["company_data"]["basic_info"]["crustdata_company_id"]
    rep.fact(f"{domain} belongs to Crustdata company {cid}", cid, IDENTIFY, f"domains=[{domain}] -> crustdata_company_id", None, at)

    # 2. profile
    data, at = _post(cd, "/company/enrich", {"crustdata_company_ids": [cid], "fields": ["basic_info", "headcount", "people", "locations"]}, rep, job)
    cdata = data[0]["matches"][0]["company_data"]
    b = cdata.get("basic_info") or {}
    hc = (cdata.get("headcount") or {}).get("total")
    loc = cdata.get("locations") or {}
    rep.entity = {"crustdata_company_id": cid, "name": b.get("name"), "domain": b.get("primary_domain"),
                  "year_founded": b.get("year_founded"), "status": b.get("status"), "company_type": b.get("company_type"),
                  "headcount": hc, "hq": loc.get("headquarters")}
    rep.fact("company name", b.get("name"), ENRICH, "basic_info.name", None, at)
    rep.fact("dataset status", b.get("status"), ENRICH, "basic_info.status (Crustdata, not the MCA registry)", None, at)
    if b.get("year_founded"):
        rep.fact("year founded", b["year_founded"], ENRICH, "basic_info.year_founded", None, at)
    if hc is not None:
        rep.fact("headcount", hc, ENRICH, "headcount.total", None, at)
    if loc.get("headquarters"):
        rep.fact("headquarters", loc["headquarters"], ENRICH, "locations.headquarters", None, at)
    if b.get("status") and b["status"] != "active":
        rep.flag("red", "inactive", f"Crustdata lists {b.get('name')} as '{b['status']}', not active.", ENRICH)
    if b.get("year_founded") and datetime.now().year - int(b["year_founded"]) < MIN_AGE_YEARS:
        rep.flag("amber", "new_company", f"Founded {b['year_founded']}: newly formed.", ENRICH)
    if hc is not None and hc < MIN_HEADCOUNT:
        rep.flag("amber", "tiny_headcount", f"Only {hc} people on record.", ENRICH)
    if name and b.get("name") and not (tokens(name) & tokens(b["name"])):
        rep.flag("amber", "name_mismatch", f"Vendor master says '{name}' but {domain} belongs to '{b['name']}'.", ENRICH)

    # director: named among the leaders on record?
    people = cdata.get("people") or {}
    leaders = []
    for group in ("founders", "cxos", "decision_makers"):
        for p in people.get(group) or []:
            bp = p.get("basic_profile") or {}
            if bp.get("name"):
                leaders.append((bp["name"], bp.get("current_title") or group, bp.get("last_updated")))
    director = (request.get("director") or "").strip()
    if director:
        hit = next((l for l in leaders if tokens(director) and tokens(director) <= tokens(l[0])), None)
        if hit:
            rep.fact(f"{director} is {hit[1]} at {b.get('name')}", hit[0], ENRICH, "people.*.basic_profile.name / current_title",
                     (hit[2] or "")[:10] or None, at)
        else:
            rep.missing.append(f"a record showing {director} runs {b.get('name')} (not among {len(leaders)} leaders on record)")

    # 3. website live and names the company
    try:
        # many sites only answer on www., and some block crawlers: try both, then browser-like retrieval
        pages, at = _post(cd, "/web/enrich/live", {"urls": [f"https://www.{domain}", f"https://{domain}"]}, rep, job)
        page = next((p for p in pages if p.get("success") and p.get("http_status") in (None, 200)), None)
        if not page:
            pages, at = _post(cd, "/web/enrich/live", {"urls": [f"https://www.{domain}"], "human_mode": True}, rep, job)
            page = next((p for p in pages if p.get("success") and p.get("http_status") in (None, 200)), None)
        if page:
            title = page.get("title") or ""
            rep.fact("website is live", page.get("url"), FETCH, "http_status", None, at)
            if mentions(title + " " + (page.get("content") or "")[:20000], b.get("name") or name):
                rep.fact("website names the company", title[:160], FETCH, "title", None, at)
            else:
                rep.flag("amber", "site_unrelated", f"{page.get('url')} does not name {b.get('name') or name} (title: '{title[:80]}').", FETCH)
        else:
            try:
                established = (hc or 0) >= 50 and b.get("year_founded") and datetime.now().year - int(b["year_founded"]) >= 3
            except (TypeError, ValueError):
                established = False
            if established:  # a long-standing company whose site blocks crawlers is not a red flag
                rep.fact("website could not be fetched (likely bot protection)", f"https://{domain}", FETCH, "success=false", None, at)
            else:
                rep.flag("amber", "site_down", f"https://{domain} did not load, and the company is small or new.", FETCH)
    except SourceUnavailable as e:
        rep.missing.append(f"website check (source unavailable: {e})")

    # 4. adverse media (5.2)
    cname = b.get("name") or name
    try:
        q = f"\"{cname}\" (" + " OR ".join(f"\"{t}\"" for t in COMPANY_TERMS[:10]) + ")"
        res, at = _post(cd, "/web/search/live", {"query": q, "sources": ["news"], "location": "IN"}, rep, job)
        for r in res.get("results") or []:
            text = f"{r.get('title', '')} {r.get('snippet', '')}"
            if mentions(text, cname) and any(t in text.lower() for t in COMPANY_TERMS):
                rep.screening["hits_found"] += 1
                rep.screening["hits_escalated"] += 1
                rep.flag("red", "adverse_media_company", f"News lead about {cname}: '{r.get('title', '')[:120]}'. A person must read it before paying.",
                         real_url(r.get("url")))
        if director:
            q = f"\"{director}\" (" + " OR ".join(f"\"{t}\"" for t in PERSON_TERMS[-10:]) + ")"
            res, at = _post(cd, "/web/search/live", {"query": q, "sources": ["news"], "location": "IN"}, rep, job)
            for r in res.get("results") or []:
                text = f"{r.get('title', '')} {r.get('snippet', '')}"
                if not (mentions(text, director) and any(re.search(rf"\b{re.escape(t)}\b", text.lower()) for t in PERSON_TERMS)):
                    continue
                rep.screening["hits_found"] += 1
                if mentions(text, cname):
                    rep.screening["hits_escalated"] += 1
                    rep.flag("red", "adverse_media_director", f"News lead naming {director} and {cname}: '{r.get('title', '')[:120]}'. A person must read it.",
                             real_url(r.get("url")))
                else:
                    rep.screening["hits_cleared"] += 1
            if rep.screening["hits_cleared"]:
                rep.screening["cleared_reasons"].append(
                    f"{rep.screening['hits_cleared']} article(s) about people named {director} do not mention {cname}: cleared as namesakes (not stored).")
        rep.fact("adverse media screen", f"found {rep.screening['hits_found']}, cleared {rep.screening['hits_cleared']}, escalated {rep.screening['hits_escalated']}",
                 SEARCH, "news search (company + director)", None, at)
    except SourceUnavailable as e:
        rep.missing.append(f"adverse media screen (source unavailable: {e})")
    return rep.to_dict()


# ---------------------------------------------------------------- caching per vendor

def vendor_key(v: dict) -> str:
    return str(v.get("vendor_id") or norm_domain(v.get("domain") or "") or v.get("legal_name"))


def cached(conn, v):
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT report FROM vendor_checks WHERE vendor_key=?", (vendor_key(v),)).fetchone()
    return json.loads(row["report"]) if row else None


def check_and_cache(conn, cd, v: dict, force=False) -> dict:
    conn.executescript(SCHEMA)
    if not force and (r := cached(conn, v)):
        return r
    rep = check_vendor(cd, {"vendor_id": v.get("vendor_id"), "legal_name": v.get("legal_name"), "domain": v.get("domain"),
                            "director": v.get("director"), "gstin": v.get("gstin")})
    conn.execute("INSERT OR REPLACE INTO vendor_checks(vendor_key, report, checked_at) VALUES (?,?,?)",
                 (vendor_key(v), json.dumps(rep), rep["checked_at"]))
    return rep


STATUS = {"CLEAR": "cleared", "HOLD": "held", "CANNOT_VERIFY": "cannot_verify"}


def effective(conn, v: dict | None) -> dict | None:
    """Vendor record with its trust status: from the Trust service if it sent one, else from our own check."""
    if not v or v.get("status"):
        return v
    r = cached(conn, v)
    if not r:
        return v
    out = dict(v)
    out["status"] = STATUS[r["verdict"]]
    if r.get("entity") and not out.get("crustdata_company_id"):
        out["crustdata_company_id"] = r["entity"]["crustdata_company_id"]
    out["_trust"] = r
    return out
