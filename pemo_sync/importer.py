# Copyright (c) 2026, Designsmith and contributors
# For license information, please see license.txt
"""
Pull Pemo card transactions into a draft Pemo Transaction Import.

Runs as a background job (receipt downloads make it too slow for a web
request). One job per import at a time.
"""

import mimetypes
import os
import re
from datetime import datetime, time, timezone
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

import frappe
from dateutil import parser as date_parser
from frappe import _
from frappe.utils import flt, get_system_timezone, getdate, now_datetime

from pemo_sync.client import PemoClient

IMPORT_DOCTYPE = "Pemo Transaction Import"
ROW_DOCTYPE = "Pemo Transaction Details"

# transactionType -> direction. "debit" = money left the wallet (book as
# expense), "credit" = money came back (book the reverse).
DEBIT_TYPES = {"purchase", "cashWithdrawal", "walletDebit", "saasFee", "refundReversal"}
CREDIT_TYPES = {"refund", "cashback", "chargeback", "purchaseReversal", "cashWithdrawalReversal"}
# Wallet funding. Designsmith already books top-ups from the bank side
# (e.g. JV/26/6333, BOB OD 0981 -> PEMO - DS), so importing them would double
# count. Controlled by Pemo Settings.skip_funding_movements.
FUNDING_TYPES = {"topUp", "walletTransfer", "subWalletTransfer"}
SKIP_STATUSES = {"declined"}


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def enqueue_fetch(name):
    doc = frappe.get_doc(IMPORT_DOCTYPE, name)
    doc.check_permission("write")
    if doc.docstatus != 0:
        frappe.throw(_("Transactions can only be fetched into a draft import."))
    if not (doc.start_date and doc.end_date):
        frappe.throw(_("Set Start Date and End Date first."))
    if getdate(doc.start_date) > getdate(doc.end_date):
        frappe.throw(_("Start Date is after End Date."))

    frappe.enqueue(
        "pemo_sync.importer.run_fetch",
        queue="long",
        timeout=3600,
        job_id=f"pemo_fetch::{name}",
        deduplicate=True,
        enqueue_after_commit=True,
        name=name,
        user=frappe.session.user,
    )


def run_fetch(name, user=None):
    try:
        summary = fetch_into(name)
        frappe.db.commit()
        _notify(user, name, ok=True, message=summary)
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), f"Pemo fetch failed: {name}")
        frappe.db.set_value(IMPORT_DOCTYPE, name, "last_fetch_log", f"{now_datetime()}: ERROR {str(e)[:500]}")
        frappe.db.commit()
        _notify(user, name, ok=False, message=str(e))


def fetch_into(name):
    doc = frappe.get_doc(IMPORT_DOCTYPE, name)
    settings = frappe.get_single("Pemo Settings")
    client = PemoClient(settings)
    company_currency = frappe.get_cached_value("Company", doc.company, "default_currency")

    start_iso, end_iso = utc_window(doc.start_date, doc.end_date)
    export_status = "all" if doc.include_already_exported else "readyToExport"
    wanted_export_states = {"ready", "exported", "manualExported"} if doc.include_already_exported else None

    known_ids = _already_imported_ids()
    counts = {"added": 0, "rows": 0, "duplicate": 0, "skipped": 0, "receipts": 0, "receipt_errors": 0}
    notes = []

    for page in client.iter_transaction_pages(start_iso, end_iso, export_status):
        for txn in page:
            txn_id = txn.get("id")
            if not txn_id or txn_id in known_ids:
                counts["duplicate"] += 1
                continue
            if wanted_export_states and txn.get("exportStatus") not in wanted_export_states:
                counts["skipped"] += 1
                continue
            reason = _skip_reason(txn, settings, company_currency)
            if reason:
                counts["skipped"] += 1
                notes.append(f"{txn_id}: {reason}")
                continue

            receipt_urls = []
            if settings.download_receipts:
                receipt_urls, failed = _store_receipts(doc, txn)
                counts["receipts"] += len(receipt_urls)
                counts["receipt_errors"] += failed
                if failed:
                    notes.append(f"{txn_id}: {failed} receipt(s) failed to download")

            for row in build_rows(txn, doc.company, company_currency):
                row["receipt_attachment"] = receipt_urls[0] if receipt_urls else None
                row["receipt_files"] = "\n".join(receipt_urls)
                doc.append("transactions", row)
                counts["rows"] += 1
            known_ids.add(txn_id)
            counts["added"] += 1

    summary = (
        f"{counts['added']} new transaction(s) as {counts['rows']} row(s), "
        f"{counts['duplicate']} already imported, {counts['skipped']} skipped, "
        f"{counts['receipts']} receipt(s) saved"
        + (f", {counts['receipt_errors']} receipt download(s) FAILED" if counts["receipt_errors"] else "")
    )
    doc.last_fetch_log = f"{now_datetime()}: {summary}" + ("\n" + "\n".join(notes[:50]) if notes else "")
    doc.save()
    return summary


# --------------------------------------------------------------------------- #
# Mapping
# --------------------------------------------------------------------------- #
def build_rows(txn, company, company_currency):
    """One row per transaction, or one per split line when Pemo split it."""
    total = abs(flt(txn.get("totalAmount")) / 100)
    tx_type = txn.get("transactionType") or ""
    is_credit = 1 if (tx_type in CREDIT_TYPES or (tx_type not in DEBIT_TYPES and flt(txn.get("totalAmount")) < 0)) else 0
    header = _accounting(txn.get("accounting"))

    base = {
        "pemo_transaction_id": txn.get("id"),
        "pemo_reference": txn.get("reference") or "",
        "transaction_type": tx_type,
        "pemo_status": txn.get("status") or "",
        "is_credit": is_credit,
        "date": to_local_date(txn.get("date")),
        "spender": ((txn.get("spender") or {}).get("email")) or "",
        "cardholder": txn.get("createdFor") or "",
        "merchant": txn.get("merchant") or "",
        "mcc": txn.get("mcc") or "",
        "currency": txn.get("totalCurrency") or company_currency,
        "original_amount": abs(flt(txn.get("transactionAmount") or txn.get("actualAmount")) / 100),
        "original_currency": txn.get("transactionCurrency") or txn.get("actualCurrency") or "",
        "amount_incl_vat": total,
        "verified": 0,
    }

    lines = txn.get("lineItems") or []
    usable = [l for l in lines if (l.get("currency") or base["currency"]) == base["currency"] and flt(l.get("amount"))]
    if not usable or len(usable) != len(lines):
        return [_row(base, header, total, company)]

    rows = []
    for line in usable:
        line_acc = _accounting(line.get("accounting"))
        merged = {**header, **line_acc}
        amount = abs(flt(line.get("amount")) / 100)
        row = _row(base, merged, amount, company)
        row["pemo_line_id"] = line.get("id") or ""
        if line.get("description"):
            row["remarks"] = (line["description"] + " | " + row["remarks"])[:140]
        rows.append(row)

    remainder = flt(total - sum(r["amount_incl"] for r in rows), 2)
    if remainder > 0.004:
        rows.append(_row(base, header, remainder, company))
    return rows


def _row(base, acc, amount, company):
    row = dict(base)
    vendor = acc.get("vendor") or {}
    coa = acc.get("chartofaccounts") or {}
    vat = acc.get("vatcode") or {}
    row.update(
        {
            "amount_incl": amount,
            "supplier": vendor.get("value") or base["merchant"],
            "supplier_trn": vendor.get("trn") or (acc.get("trn") or {}).get("value") or "",
            "pemo_category": " | ".join(v for v in (coa.get("value"), coa.get("code")) if v),
            "pemo_vat_code": vat.get("value") or "",
            "memo": (acc.get("memo") or {}).get("value") or "",
            "invoice_no": _invoice_no(acc),
            "remarks": " | ".join(v for v in (base["merchant"], base["cardholder"], base["pemo_reference"]) if v)[:140],
        }
    )
    row["expense_account"] = find_account(company, coa.get("code"), coa.get("value"))
    row["project"] = find_project((acc.get("project") or {}).get("code"), (acc.get("project") or {}).get("value"))
    row["vat_applicable"] = vat_applicable_from_code(row["pemo_vat_code"])
    return row


def _accounting(entries):
    """Pemo accounting array -> {lower-cased type: entry}. First entry wins."""
    out = {}
    for entry in entries or []:
        key = (entry.get("type") or "").strip().lower()
        if key and key not in out:
            out[key] = entry
    return out


INVOICE_LABEL = re.compile(r"^(invoice|inv|bill|receipt)\s*(no|number|num|#)?$")


def _invoice_no(acc):
    if acc.get("receiptnumber"):
        return acc["receiptnumber"].get("value") or ""
    for label, entry in acc.items():
        if INVOICE_LABEL.match(re.sub(r"[^a-z# ]", "", label).strip()):
            return entry.get("value") or ""
    return ""


def vat_applicable_from_code(code):
    """Pemo vatCode label -> 'Yes' / 'No' / '' (unknown: the user picks)."""
    text = (code or "").lower()
    if not text:
        return ""
    if any(word in text for word in ("zero", "exempt", "out of scope", "no vat", "non-vat")):
        return "No"
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if match:
        return "Yes" if flt(match.group(1)) > 0 else "No"
    if "standard" in text or "vat" in text:
        return "Yes"
    return ""


def find_account(company, code, value):
    base = {"company": company, "is_group": 0, "disabled": 0}
    if code:
        name = frappe.db.get_value("Account", {**base, "account_number": code})
        if name:
            return name
    if value:
        return frappe.db.get_value("Account", {**base, "account_name": value})
    return None


def find_project(code, value):
    for candidate in (code, value):
        if candidate and frappe.db.exists("Project", candidate):
            return candidate
    if value:
        return frappe.db.get_value("Project", {"project_name": value})
    return None


def _skip_reason(txn, settings, company_currency):
    if txn.get("status") in SKIP_STATUSES:
        return f"status {txn.get('status')}"
    if settings.skip_funding_movements and txn.get("transactionType") in FUNDING_TYPES:
        return f"{txn.get('transactionType')} (wallet funding is booked from the bank side)"
    if not flt(txn.get("totalAmount")):
        return "zero amount"
    currency = txn.get("totalCurrency") or company_currency
    if currency != company_currency:
        return f"wallet currency {currency} is not the company currency {company_currency}"
    return None


# --------------------------------------------------------------------------- #
# Receipts
# --------------------------------------------------------------------------- #
def _store_receipts(doc, txn):
    """Download every receipt onto the import. Returns ([file_url, ...], failed count).

    The exact URLs are kept on the row: Frappe appends a hash to the name when a
    file of that name already exists (e.g. on re-import), so names can't be
    matched back later.
    """
    urls, failed = [], 0
    for idx, url in enumerate(txn.get("receipts") or [], start=1):
        try:
            content, content_type = PemoClient.download(url)
            file_doc = frappe.get_doc(
                {
                    "doctype": "File",
                    "file_name": _receipt_filename(txn, idx, url, content_type),
                    "attached_to_doctype": doc.doctype,
                    "attached_to_name": doc.name,
                    "is_private": 1,
                    "content": content,
                }
            )
            file_doc.save(ignore_permissions=True)
            urls.append(file_doc.file_url)
        except Exception:
            failed += 1
            frappe.log_error(frappe.get_traceback(), f"Pemo receipt download failed: {txn.get('id')}")
    return urls, failed


def _receipt_filename(txn, idx, url, content_type):
    ext = os.path.splitext(unquote(urlparse(url).path))[1].lower()
    if not ext or len(ext) > 6:
        ext = mimetypes.guess_extension(content_type or "") or ".bin"
    if ext == ".jpe":
        ext = ".jpg"
    stem = re.sub(r"[^A-Za-z0-9_-]", "", txn.get("reference") or txn.get("id") or "receipt")
    return f"pemo-{stem}-{idx}{ext}"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def utc_window(start_date, end_date):
    """Local [start 00:00, end 23:59:59.999] as UTC ISO strings, end capped at now.

    Pemo filters on the UTC timestamp. Designsmith books in Asia/Dubai, so a
    purchase at 02:00 on the 5th local is 22:00Z on the 4th; converting the
    window keeps it in the local day the user asked for. The end is pinned to
    a fixed instant for the whole paged run, as Pemo recommends.
    """
    tz = ZoneInfo(get_system_timezone())
    start = datetime.combine(getdate(start_date), time.min, tz).astimezone(timezone.utc)
    end = datetime.combine(getdate(end_date), time.max, tz).astimezone(timezone.utc)
    end = min(end, datetime.now(timezone.utc))
    return _iso(start), _iso(end)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def to_local_date(value):
    if not value:
        return None
    dt = date_parser.isoparse(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(get_system_timezone())).date()


def _already_imported_ids():
    """Transaction ids sitting in any draft or submitted import (not cancelled)."""
    rows = frappe.get_all(
        ROW_DOCTYPE,
        filters={"parenttype": IMPORT_DOCTYPE, "docstatus": ["<", 2], "pemo_transaction_id": ["is", "set"]},
        pluck="pemo_transaction_id",
    )
    return set(rows)


def _notify(user, name, ok, message):
    frappe.publish_realtime(
        "pemo_fetch_done",
        {"name": name, "ok": ok, "message": message},
        user=user,
        after_commit=True,
    )
