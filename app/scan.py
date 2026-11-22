"""Scan or upload a bill (photo, PDF, text, email) and run it through the desk.

Reading, in order of preference:
  - Claude (ANTHROPIC_API_KEY set): reads PDFs and photos directly, quoting each field;
  - no model key: text PDFs via pypdf, photos via Tesseract OCR, .txt/.eml as text,
    then the fixed-pattern reader (inbox.rule_extract). Anything it cannot read stays empty, never guessed.
The original file is kept (by content hash) so anyone can open it from the bill page.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import time

from . import config, inbox
from .db import connect, log_event

DOCS_DIR = inbox.DOCS_DIR
IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
ALLOWED = {".pdf", ".txt", ".eml", ".json", *IMAGE_EXT}
MAX_BYTES = 15 * 1024 * 1024


class UploadRejected(Exception):
    pass


def save_file(data: bytes, filename: str) -> tuple[str, str]:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED:
        raise UploadRejected(f"{filename}: only PDF, photo (PNG/JPG/WEBP), .txt or .eml files are accepted")
    if len(data) > MAX_BYTES:
        raise UploadRejected(f"{filename}: larger than 15 MB")
    if not data:
        raise UploadRejected(f"{filename}: the file is empty")
    os.makedirs(DOCS_DIR, exist_ok=True)
    doc_id = hashlib.sha256(data).hexdigest()[:20]
    with open(os.path.join(DOCS_DIR, doc_id + ext), "wb") as f:
        f.write(data)
    return doc_id, ext


def pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)[:20000]
    except Exception:
        return ""


def ocr_image(path: str) -> str:
    exe = shutil.which("tesseract")
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, path, "-", "-l", "eng", "--psm", "6"], capture_output=True, text=True, timeout=60)
        return r.stdout[:20000]
    except (subprocess.SubprocessError, OSError):
        return ""


def to_message(data: bytes, filename: str, doc_id: str, ext: str) -> dict:
    path = os.path.join(DOCS_DIR, doc_id + ext)
    msg = {"id": f"upload:{doc_id}{ext}", "from": "", "subject": filename, "date": time.strftime("%Y-%m-%d"),
           "body": "", "pdfs": [], "images": [], "reader": ""}
    if ext == ".pdf":
        msg["pdfs"] = [(filename, data)]
        msg["body"] = pdf_text(data)
        msg["reader"] = "pdf text" if msg["body"].strip() else ""
    elif ext in IMAGE_EXT:
        msg["images"] = [(filename, data, IMAGE_EXT[ext])]
        msg["body"] = ocr_image(path)
        msg["reader"] = "ocr (tesseract)" if msg["body"].strip() else ""
    else:
        m = inbox.load_message(path)
        m.update(id=f"upload:{doc_id}{ext}", images=[], reader="text")
        if not m["subject"]:
            m["subject"] = filename
        return m
    return msg


def process_upload(files: list[tuple[str, bytes]], uploaded_by: str, conn=None, client=None, **process_kw) -> dict:
    """files: [(filename, bytes)]. Reads each, submits the bills as one run, processes it."""
    from . import pipeline
    conn = conn or connect()
    invoices, changes, unreadable, docs = [], [], [], []
    for filename, data in files:
        doc_id, ext = save_file(data, filename)
        msg = to_message(data, filename, doc_id, ext)
        has_model = client is not None or bool(os.environ.get("ANTHROPIC_API_KEY"))
        if not has_model and not (msg["body"] or "").strip():
            unreadable.append({"file": filename, "doc": f"{doc_id}{ext}",
                               "why": "no text could be read from it (scanned PDF or unclear photo); the Claude reader needs ANTHROPIC_API_KEY"})
            continue
        try:
            data_x = inbox.extract(msg, conn, client=client)
        except inbox.ExtractionRefused as e:
            unreadable.append({"file": filename, "doc": f"{doc_id}{ext}", "why": str(e)})
            continue
        found = inbox.message_to_records(msg, data_x)
        for inv in found["invoices"]:
            inv["_original"]["upload"] = {"doc": f"{doc_id}{ext}", "name": filename, "by": uploaded_by,
                                          "reader": "claude" if has_model else msg.get("reader")}
        invoices += found["invoices"]
        changes += found["bank_change_requests"]
        if not found["invoices"] and not found["bank_change_requests"]:
            unreadable.append({"file": filename, "doc": f"{doc_id}{ext}", "why": "no invoice number or amount found in it"})
        docs.append({"file": filename, "doc": f"{doc_id}{ext}", "reader": "claude" if has_model else msg.get("reader")})
    out = {"docs": docs, "unreadable": unreadable, "run": None}
    if not invoices and not changes:
        return out
    req = {"invoices": invoices, "bank_change_requests": changes, "purchase_orders": load_purchase_orders(),
           "source": "upload", "uploaded_by": uploaded_by, "uploaded_at": time.time()}
    run_id = pipeline.submit(req, conn)
    log_event(conn, run_id, None, "bills_uploaded", {"by": uploaded_by, "files": [d["file"] for d in docs]})
    out["run"] = pipeline.process(run_id, conn, **process_kw)
    return out


def load_purchase_orders() -> list:
    path = os.environ.get("PURCHASE_ORDERS_FILE") or os.path.join(os.path.dirname(__file__), "..", "data", "purchase_orders.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


# ---------------------------------------------------------------- supporting documents

ATTACH_KINDS = {"purchase_order": "Purchase order", "goods_receipt": "Goods receipt / delivery proof",
                "contract": "Contract or rate card", "bank_proof": "Bank account proof (cancelled cheque)", "other": "Other"}
SCHEMA = """CREATE TABLE IF NOT EXISTS attachments (
    att_id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL, kind TEXT NOT NULL, filename TEXT NOT NULL,
    doc TEXT NOT NULL, uploaded_by TEXT NOT NULL, uploaded_at REAL NOT NULL)"""


def attach(conn, invoice_id: str, kind: str, filename: str, data: bytes, by: str) -> dict:
    if kind not in ATTACH_KINDS:
        raise UploadRejected(f"unknown document type {kind}")
    if not conn.execute("SELECT 1 FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone():
        raise UploadRejected("no such bill")
    doc_id, ext = save_file(data, filename)
    conn.execute(SCHEMA)
    att = {"att_id": f"{invoice_id}:{doc_id}", "invoice_id": invoice_id, "kind": kind, "filename": filename,
           "doc": doc_id + ext, "uploaded_by": by, "uploaded_at": time.time()}
    conn.execute("INSERT OR IGNORE INTO attachments VALUES (:att_id,:invoice_id,:kind,:filename,:doc,:uploaded_by,:uploaded_at)", att)
    run_id = conn.execute("SELECT run_id FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()["run_id"]
    log_event(conn, run_id, invoice_id, "document_attached", {"kind": ATTACH_KINDS[kind], "file": filename, "by": by})
    return att


def attachments(conn, invoice_id: str) -> list:
    conn.execute(SCHEMA)
    return [dict(r) | {"kind_label": ATTACH_KINDS.get(r["kind"], r["kind"])}
            for r in conn.execute("SELECT * FROM attachments WHERE invoice_id=? ORDER BY uploaded_at", (invoice_id,))]


def checklist(conn, invoice_id: str) -> list:
    """Bill-payment verification checklist: each line is passed, failed, or not checked, with the reason."""
    inv = conn.execute("SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
    codes = {r["code"]: r["text"] for r in json.loads(inv["reasons"])}
    kinds = {a["kind"] for a in attachments(conn, invoice_id)}
    pay = conn.execute("SELECT * FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
    appr = conn.execute("SELECT * FROM approvals WHERE invoice_id=?", (invoice_id,)).fetchone()

    def item(label, ok_codes, bad_codes, extra=None):
        for c in bad_codes:
            if c in codes:
                return {"check": label, "result": "fail", "detail": codes[c]}
        for c in ok_codes:
            if c in codes:
                return {"check": label, "result": "pass", "detail": codes[c]}
        return {"check": label, "result": "not checked", "detail": extra or "The desk stopped before this check."}

    out = [
        item("Payee is a known, cleared vendor", ["vendor_cleared"], ["vendor_not_cleared", "no_vendor_record"]),
        item("GSTIN valid and matches the vendor", ["vendor_cleared"], ["gstin_check_digit", "gstin_malformed", "gstin_entity_type", "gstin_mismatch"]),
        item("Sender is the vendor (no look-alike)", ["identity_confirmed", "identity_by_gstin"], ["lookalike_sender", "lookalike_payee", "domain_unknown"]),
        item("Pays only the verified bank account", ["checks_passed"], ["bank_change_requested", "bank_details_changed", "no_verified_account"]),
        item("Not a duplicate", ["checks_passed"], ["possible_duplicate"]),
        item("Matches its purchase order", ["checks_passed"], ["po_mismatch", "po_already_billed"]),
        {"check": "Goods receipt or delivery proof on file", "result": "pass" if "goods_receipt" in kinds else "missing",
         "detail": "Attached." if "goods_receipt" in kinds else "Not attached: add one below for a full three-way match."},
        {"check": "A person approved the payment", "result": "pass" if appr and appr["decision"] == "approved" else
         ("fail" if appr and appr["decision"] == "rejected" else "waiting"),
         "detail": (f"Approved by {appr['decided_by']}" + (f", first approval by {appr['first_by']}" if appr["first_by"] else "")) if appr and appr["decision"] == "approved"
         else (f"Rejected by {appr['decided_by']}" if appr and appr["decision"] == "rejected" else
               (f"First approval by {appr['first_by']}; a second person must approve" if appr and appr["first_by"] else "Waiting for a person."))},
        {"check": "Paid exactly once", "result": "pass" if pay and pay["provider_ref"] else "waiting",
         "detail": f"Payout {pay['provider_ref']} with key {pay['idempotency_key'][:8]}…" + (" (simulated)" if pay["provider"] == "local_sandbox" else "")
         if pay and pay["provider_ref"] else "Not paid yet."},
    ]
    return out
