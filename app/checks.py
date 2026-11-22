"""Deterministic checks. Same invoice, worded differently -> same answer."""
import re
from datetime import date

PO_TOLERANCE = 0.01  # 1%


def to_paise(amount) -> int:
    if isinstance(amount, (int, float)):
        return int(round(float(amount) * 100))
    s = re.sub(r"[^\d.]", "", str(amount))  # strips ₹, commas, "INR"
    if not s:
        raise ValueError(f"cannot read amount {amount!r}")
    return int(round(float(s) * 100))


def norm_invoice_no(s) -> str:
    """'INV-0042', 'inv 42', 'Inv/0042' -> 'inv42'"""
    s = re.sub(r"[^a-z0-9]", "", str(s or "").lower())
    return re.sub(r"(?<=[a-z])0+(?=\d)", "", s)


def norm_date(s) -> str | None:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            from datetime import datetime
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def mask_account(acct: str | None) -> str | None:
    if not acct:
        return None
    a = re.sub(r"\s", "", str(acct))
    return "X" * max(0, len(a) - 4) + a[-4:]


def same_account(a: dict | None, b: dict | None) -> bool:
    if not a or not b:
        return False
    na = re.sub(r"\s", "", str(a.get("account_no", ""))).lstrip("0")
    nb = re.sub(r"\s", "", str(b.get("account_no", ""))).lstrip("0")
    return na == nb and str(a.get("ifsc", "")).upper() == str(b.get("ifsc", "")).upper()


def find_duplicates(conn, invoice_id, vendor_id, inv_no_norm, amount_paise, inv_date):
    """Look only at invoices received BEFORE this one (first seen wins)."""
    me = conn.execute("SELECT rowid FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()["rowid"]
    hits = []
    if inv_no_norm:
        for r in conn.execute(
            "SELECT invoice_id, state, outcome FROM invoices WHERE vendor_id=? AND invoice_no_norm=? AND rowid<?",
            (vendor_id, inv_no_norm, me),
        ):
            hits.append(("same_invoice_number", r["invoice_id"], r["outcome"] or r["state"]))
    for r in conn.execute(
        "SELECT invoice_id, state, outcome FROM invoices WHERE vendor_id=? AND amount_paise=? AND invoice_date=? AND rowid<?",
        (vendor_id, amount_paise, inv_date, me),
    ):
        if not any(h[1] == r["invoice_id"] for h in hits):
            hits.append(("same_amount_and_date", r["invoice_id"], r["outcome"] or r["state"]))
    return hits


GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
PAN_TYPES = {"C": "company", "F": "firm / LLP", "P": "individual (proprietor)", "H": "HUF", "T": "trust",
             "A": "association of persons", "B": "body of individuals", "G": "government", "L": "local authority",
             "J": "artificial juridical person"}


def gstin_check_char(first14: str) -> str:
    """GSTIN check character (mod-36, alternating weights 1 and 2)."""
    total = 0
    for i, ch in enumerate(first14):
        p = GST_CHARS.index(ch) * (2 if i % 2 else 1)
        total += p // 36 + p % 36
    return GST_CHARS[(36 - total % 36) % 36]


def gstin_problems(gstin: str, legal_name: str = "") -> list[tuple[str, str]]:
    """Free, local checks. Returns [(code, text)]; empty when the GSTIN is well-formed and fits the legal form."""
    g = re.sub(r"[^0-9A-Z]", "", (gstin or "").upper())
    if not g:
        return []
    if not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", g):
        return [("gstin_malformed", f"GSTIN {g} is not in the 15-character GSTIN format.")]
    if gstin_check_char(g[:14]) != g[14]:
        return [("gstin_check_digit", f"GSTIN {g} fails its check character (expected {gstin_check_char(g[:14])}, got {g[14]}): mistyped or forged.")]
    pan_type = g[5]
    n = (legal_name or "").lower()
    if re.search(r"\b(ltd|limited|pvt|private)\b", n) and pan_type != "C":
        return [("gstin_entity_type", f"{legal_name} is a company, but its GSTIN's PAN is for a {PAN_TYPES.get(pan_type, pan_type)}.")]
    if re.search(r"\bllp\b", n) and pan_type != "F":
        return [("gstin_entity_type", f"{legal_name} is an LLP, but its GSTIN's PAN is for a {PAN_TYPES.get(pan_type, pan_type)}.")]
    return []


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def billed_against_po(conn, invoice_id, vendor_id, po_number) -> int:
    """Paise already committed against this PO by other invoices (ready, approved, paying or paid)."""
    tot = 0
    for r in conn.execute(
        "SELECT invoice_id, raw, amount_paise FROM invoices WHERE vendor_id=? AND invoice_id<>? "
        "AND (state IN ('APPROVED','PAYING','PAID') OR (state='AWAITING_APPROVAL' AND outcome='ready'))",
        (vendor_id, invoice_id)):
        import json as _j
        if str(_j.loads(r["raw"]).get("po_number", "")).strip().lower() == str(po_number or "").strip().lower():
            tot += r["amount_paise"] or 0
    return tot


def match_po(invoice: dict, pos: list[dict], amount_paise: int):
    """Returns (ok, reason). Missing PO is a hold, not a guess."""
    po_no = (invoice.get("po_number") or "").strip()
    if not po_no:
        return False, "invoice has no purchase order number"
    po = next((p for p in pos if str(p.get("po_number", "")).strip().lower() == po_no.lower()), None)
    if not po:
        return False, f"purchase order {po_no} not found in the client's records"
    po_paise = to_paise(po.get("amount"))
    if po_paise and abs(amount_paise - po_paise) > PO_TOLERANCE * po_paise:
        return False, f"invoice amount ₹{amount_paise/100:,.2f} differs from PO {po_no} amount ₹{po_paise/100:,.2f}"
    return True, f"matches PO {po_no}"
