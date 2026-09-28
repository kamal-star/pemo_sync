# Copyright (c) 2026, Designsmith and contributors
# For license information, please see license.txt
"""
Pemo Transaction Import -> Journal Entries -> Pemo "markAsExported".

One Journal Entry per Pemo transaction. A transaction Pemo split into lines
arrives as several import rows sharing one pemo_transaction_id; those rows
become the expense lines of a single JE, with one wallet line for the total.

Debit (purchase):   Dr expense (net)  Dr input VAT   Cr Pemo wallet (gross)
Credit (refund...): the same lines, sides swapped.

JEs are created as drafts and moved to Pemo Settings.je_workflow_state
("Waiting for Accounts Approval" on Designsmith) so they go through the normal
JE Standard approval. They are never submitted here.

Pemo is told the records are exported only AFTER the database commit, from a
background job: a PATCH sent inside the submit transaction would survive a
rollback and leave Pemo saying "exported" for rows that were never booked.
"""

import frappe
from frappe import _
from frappe.model.workflow import get_workflow_name
from frappe.utils import flt, now_datetime

from pemo_sync.client import PemoClient

IMPORT_DOCTYPE = "Pemo Transaction Import"
ROW_DOCTYPE = "Pemo Transaction Details"


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def rows_not_ready(rows):
    """Return a list of human-readable problems for rows that would be posted."""
    problems = []
    for r in rows:
        if r.journal_entry:
            continue
        label = _("Row {0} ({1})").format(r.idx, r.supplier or r.merchant or r.pemo_transaction_id or "")
        if not r.verified:
            problems.append(_("{0}: not verified").format(label))
        elif not r.expense_account:
            problems.append(_("{0}: no Expense Account").format(label))
        elif r.vat_applicable not in ("Yes", "No"):
            problems.append(_("{0}: VAT Applicable not set").format(label))
        elif r.vat_applicable == "Yes" and not r.vat_account:
            problems.append(_("{0}: no VAT Account").format(label))
        elif flt(r.amount_incl) <= 0:
            problems.append(_("{0}: amount must be above zero").format(label))
        elif r.vat_applicable == "Yes" and flt(r.vat_amount) >= flt(r.amount_incl):
            problems.append(_("{0}: VAT amount is not below the gross amount").format(label))
    return problems


# --------------------------------------------------------------------------- #
# Journal Entries
# --------------------------------------------------------------------------- #
def create_journal_entries(imp):
    """Create one draft JE per unposted transaction on `imp`. All or nothing."""
    settings = frappe.get_single("Pemo Settings")
    target_state = _target_workflow_state(settings)

    groups = {}
    for r in imp.transactions:
        if r.journal_entry:
            continue
        groups.setdefault(r.pemo_transaction_id or r.name, []).append(r)

    created = []
    for rows in groups.values():
        je = _build_journal_entry(imp, rows, settings)
        je.flags.ignore_permissions = True
        je.insert()
        if target_state:
            je.workflow_state = target_state
            je.save()
        _attach_receipts(imp, rows, je.name)
        for r in rows:
            r.db_set("journal_entry", je.name, update_modified=False)
        created.append(je.name)

    _update_posted_count(imp.name)
    return created


def _build_journal_entry(imp, rows, settings):
    first = rows[0]
    wallet_side, expense_side = ("debit", "credit") if first.is_credit else ("credit", "debit")
    accounts = []
    gross_total = 0.0
    for r in rows:
        gross = flt(r.amount_incl, 2)
        vat = flt(r.vat_amount, 2) if r.vat_applicable == "Yes" else 0.0
        cost_center = _cost_center(r.project, settings, imp.company)
        remark = _line_remark(r)
        accounts.append(_line(r.expense_account, expense_side, flt(gross - vat, 2), cost_center, r.project, remark))
        if vat:
            accounts.append(_line(r.vat_account, expense_side, vat, cost_center, r.project, _("Input VAT")))
        gross_total += gross

    accounts.append(
        _line(
            imp.pemo_account,
            wallet_side,
            flt(gross_total, 2),
            _cost_center(None, settings, imp.company),
            None,
            _("Pemo card {0}").format(first.cardholder or first.spender or ""),
        )
    )

    return frappe.get_doc(
        {
            "doctype": "Journal Entry",
            "voucher_type": "Journal Entry",
            "company": imp.company,
            "posting_date": first.date,
            "cheque_no": (first.pemo_reference or first.pemo_transaction_id or "")[:140] or None,
            "cheque_date": first.date if (first.pemo_reference or first.pemo_transaction_id) else None,
            "user_remark": _je_remark(first),
            "pemo_import": imp.name,
            "pemo_transaction_id": first.pemo_transaction_id,
            "accounts": accounts,
        }
    )


def _line(account, side, amount, cost_center, project, remark):
    line = {
        "account": account,
        "debit_in_account_currency": amount if side == "debit" else 0,
        "credit_in_account_currency": amount if side == "credit" else 0,
        "cost_center": cost_center,
        "user_remark": remark,
    }
    if project:
        line["project"] = project
    return line


def _je_remark(r):
    parts = [
        _("Pemo {0}").format(r.transaction_type or "transaction"),
        r.supplier or r.merchant,
        r.cardholder or r.spender,
        _("Inv {0}").format(r.invoice_no) if r.invoice_no else "",
        r.memo,
        r.pemo_reference or r.pemo_transaction_id,
    ]
    return " | ".join(p for p in parts if p)


def _line_remark(r):
    parts = [r.supplier or r.merchant, r.memo, r.remarks if r.pemo_line_id else ""]
    return " | ".join(p for p in parts if p)[:140]


def _cost_center(project, settings, company):
    if project:
        cc = frappe.get_cached_value("Project", project, "cost_center")
        if cc:
            return cc
    if settings.default_cost_center and frappe.get_cached_value(
        "Cost Center", settings.default_cost_center, "company"
    ) == company:
        return settings.default_cost_center
    return frappe.get_cached_value("Company", company, "cost_center")


def _target_workflow_state(settings):
    """The draft state to move new JEs into, or None to leave them in Draft."""
    state = (settings.je_workflow_state or "").strip()
    workflow = get_workflow_name("Journal Entry")
    if not state or not workflow:
        return None
    doc_status = frappe.db.get_value("Workflow Document State", {"parent": workflow, "state": state}, "doc_status")
    if doc_status is None:
        frappe.throw(
            _("Pemo Settings: workflow state '{0}' is not in the active Journal Entry workflow '{1}'.").format(
                state, workflow
            )
        )
    if str(doc_status) != "0":
        frappe.throw(
            _("Pemo Settings: '{0}' is a submitted state. Pemo only creates drafts; pick a draft state.").format(state)
        )
    return state


def _attach_receipts(imp, rows, je_name):
    """Link the receipt files stored on the import onto the JE (no copy on disk)."""
    urls = []
    for r in rows:
        for url in [r.receipt_attachment, *(r.receipt_files or "").splitlines()]:
            url = (url or "").strip()
            if url and url not in urls:
                urls.append(url)
    for url in urls:
        if frappe.db.exists("File", {"file_url": url, "attached_to_doctype": "Journal Entry", "attached_to_name": je_name}):
            continue
        source = frappe.db.get_value(
            "File",
            {"file_url": url, "attached_to_doctype": IMPORT_DOCTYPE, "attached_to_name": imp.name},
            ["file_name", "is_private"],
            as_dict=True,
        ) or frappe._dict(file_name=url.rsplit("/", 1)[-1], is_private=1 if url.startswith("/private/") else 0)
        frappe.get_doc(
            {
                "doctype": "File",
                "file_url": url,
                "file_name": source.file_name,
                "is_private": source.is_private,
                "attached_to_doctype": "Journal Entry",
                "attached_to_name": je_name,
            }
        ).insert(ignore_permissions=True)


def _update_posted_count(name):
    posted = frappe.db.count(ROW_DOCTYPE, {"parent": name, "parenttype": IMPORT_DOCTYPE, "journal_entry": ["is", "set"]})
    frappe.db.set_value(IMPORT_DOCTYPE, name, "posted_transactions", posted, update_modified=False)


# --------------------------------------------------------------------------- #
# Cancelling an import
# --------------------------------------------------------------------------- #
def reverse_journal_entries(imp):
    """Delete draft JEs and cancel submitted ones. Called from on_cancel."""
    for je_name in sorted({r.journal_entry for r in imp.transactions if r.journal_entry}):
        docstatus = frappe.db.get_value("Journal Entry", je_name, "docstatus")
        if docstatus == 0:
            frappe.delete_doc("Journal Entry", je_name, ignore_permissions=True)
        elif docstatus == 1:
            je = frappe.get_doc("Journal Entry", je_name)
            je.flags.ignore_permissions = True
            je.cancel()


def release_rows_for_journal_entry(doc, method=None):
    """Journal Entry on_trash / on_cancel: unlink it from its import rows.

    Clearing the link does two jobs: the JE can then be deleted/cancelled
    without Frappe's back-link check refusing, and the row counts as unposted
    again so "Create Missing Journal Entries" can re-post it.
    """
    if not doc.get("pemo_import"):
        return
    rows = frappe.get_all(
        ROW_DOCTYPE, filters={"parenttype": IMPORT_DOCTYPE, "journal_entry": doc.name}, fields=["name", "parent"]
    )
    for row in rows:
        frappe.db.set_value(ROW_DOCTYPE, row.name, "journal_entry", None, update_modified=False)
    for parent in {r.parent for r in rows}:
        _update_posted_count(parent)


# --------------------------------------------------------------------------- #
# Confirming the export back to Pemo
# --------------------------------------------------------------------------- #
def enqueue_confirm(name):
    frappe.db.set_value(IMPORT_DOCTYPE, name, "pemo_export_status", "Pending", update_modified=False)
    frappe.enqueue(
        "pemo_sync.posting.confirm_export",
        queue="short",
        job_id=f"pemo_confirm::{name}",
        deduplicate=True,
        enqueue_after_commit=True,
        name=name,
    )


def confirm_export(name):
    """PATCH markAsExported for every posted row. Idempotent, safe to re-run."""
    ids = frappe.get_all(
        ROW_DOCTYPE,
        filters={
            "parent": name,
            "parenttype": IMPORT_DOCTYPE,
            "journal_entry": ["is", "set"],
            "pemo_transaction_id": ["is", "set"],
        },
        pluck="pemo_transaction_id",
    )
    try:
        count = PemoClient().mark_transactions_exported(ids) if ids else 0
        frappe.db.set_value(
            IMPORT_DOCTYPE,
            name,
            {"pemo_export_status": "Confirmed", "pemo_export_log": f"{now_datetime()}: {count} transaction(s) marked exported in Pemo"},
            update_modified=False,
        )
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), f"Pemo export confirmation failed: {name}")
        frappe.db.set_value(
            IMPORT_DOCTYPE,
            name,
            {"pemo_export_status": "Failed", "pemo_export_log": f"{now_datetime()}: {str(e)[:500]}"},
            update_modified=False,
        )
    frappe.db.commit()
