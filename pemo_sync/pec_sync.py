# Copyright (c) 2026, Designsmith and contributors
# For license information, please see license.txt
"""
Pemo "Ready for export" -> Designsmith Petty Expense Claim, automatically.

Pemo's External API is pull-only (no webhooks), so "push" is a scheduled pull:
every 15 minutes (hooks.py) this asks Pemo for transactions an accountant has
marked Ready for export, creates one Petty Expense Claim per transaction, and
then tells Pemo they are exported so they never arrive twice.

A claim created here is an ordinary Petty Expense Claim: it goes through the
PEC JUL 26 approval workflow, and on approval the site's own "JV CREATION NEW
-v" Server Script posts it. Its Petty Cash Account is the Pemo wallet, so that
Journal Entry credits PEMO - DS instead of a bank or petty cash account.

Order of writes per transaction: insert claim -> attach receipts -> commit ->
PATCH Pemo. A crash between commit and PATCH leaves the claim in ERPNext and
the record still "ready" in Pemo; the next run sees the id already claimed and
just confirms it. Nothing is booked twice and nothing is lost.
"""

import frappe
from frappe import _
from frappe.utils import flt, now_datetime
from frappe.utils.synchronization import LockTimeoutError, filelock

from pemo_sync import importer
from pemo_sync.client import PemoClient

PEC = "Petty Expense Claim"
PEC_ROW = "Petty Expense Details"
PROJECT_EXPENSE_TYPES = ("Project Material", "Project Works")


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def scheduled_sync():
    """Scheduler entry (every 15 min). Does nothing unless Auto Sync is on."""
    if not frappe.db.get_single_value("Pemo Settings", "auto_sync"):
        return
    run_sync()


def enqueue_sync(user=None):
    frappe.enqueue(
        "pemo_sync.pec_sync.run_sync",
        queue="long",
        timeout=3600,
        job_id="pemo_pec_sync",
        deduplicate=True,
        enqueue_after_commit=True,
        user=user,
    )


def run_sync(user=None):
    """One sync pass. Never raises: the outcome goes to Pemo Settings' status fields."""
    try:
        with filelock("pemo_pec_sync", timeout=1):
            status = _sync()
    except LockTimeoutError:
        status = "Skipped: a previous sync is still running."
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Pemo → Petty Expense Claim sync failed")
        status = f"ERROR: {str(e)[:400]}"

    frappe.db.set_single_value("Pemo Settings", {"last_sync_run": now_datetime(), "last_sync_status": status})
    frappe.db.commit()
    if user:
        frappe.publish_realtime("pemo_pec_sync_done", {"message": status}, user=user)
    return status


def claimed_transaction_ids():
    """Every Pemo transaction id that already has a Petty Expense Claim (any state)."""
    if not frappe.db.has_column(PEC, "pemo_transaction_id"):
        return set()
    return set(frappe.get_all(PEC, filters={"pemo_transaction_id": ["is", "set"]}, pluck="pemo_transaction_id"))


# --------------------------------------------------------------------------- #
# The pass
# --------------------------------------------------------------------------- #
def _sync():
    settings = frappe.get_single("Pemo Settings")
    _check_settings(settings)
    client = PemoClient(settings)
    currency = frappe.get_cached_value("Company", settings.company, "default_currency")
    start_iso, end_iso = importer.utc_window(settings.sync_from_date, now_datetime().date())

    claimed = _claims_by_transaction()
    in_imports = _ids_in_live_imports()
    counts = {"created": 0, "reconfirmed": 0, "skipped": 0, "held": 0, "failed": 0, "receipt_retry": 0}
    notes = []

    for page in client.iter_transaction_pages(start_iso, end_iso):
        confirm = []
        for txn in page:
            txn_id = txn.get("id")
            if not txn_id:
                continue

            if txn_id in claimed:
                # Already a claim: an earlier PATCH failed, or its receipts were
                # still missing. Retry receipts with this listing's fresh links.
                pec_name, receipts_pending = claimed[txn_id]
                if receipts_pending and not _retry_receipts(pec_name, txn):
                    counts["held"] += 1
                    continue
                counts["receipt_retry"] += 1 if receipts_pending else 0
                counts["reconfirmed"] += 1
                confirm.append(txn_id)
                continue

            if txn_id in in_imports:
                # Someone pulled it into a manual Pemo Transaction Import. Booking it
                # here too would double it; the import decides (submit it, or delete
                # the row and the next pass claims it).
                counts["held"] += 1
                notes.append(f"{txn_id} ({txn.get('merchant')}): already in {in_imports[txn_id]}, not claimed")
                continue

            reason, hold = _skip_reason(txn, settings, currency)
            if reason:
                notes.append(f"{txn_id} ({txn.get('merchant') or txn.get('transactionType')}): {reason}")
                if hold:
                    counts["held"] += 1  # left "ready" in Pemo for a person to handle
                else:
                    counts["skipped"] += 1
                    confirm.append(txn_id)  # handled by policy (e.g. top-up): stop it recurring
                continue

            try:
                pec_name, receipts_ok = _create_claim(txn, settings, currency)
                frappe.db.commit()
            except Exception as e:
                frappe.db.rollback()
                frappe.log_error(frappe.get_traceback(), f"Pemo transaction {txn_id} → Petty Expense Claim failed")
                counts["failed"] += 1
                notes.append(f"{txn_id}: FAILED {str(e)[:160]}")
                continue

            claimed[txn_id] = (pec_name, not receipts_ok)
            counts["created"] += 1
            if receipts_ok:
                confirm.append(txn_id)
            else:
                counts["held"] += 1
                notes.append(f"{txn_id} → {pec_name}: receipt download failed, retried next run")

        if confirm:
            client.mark_transactions_exported(confirm)

    status = (
        f"OK: {counts['created']} claim(s) created, {counts['reconfirmed']} re-confirmed, "
        f"{counts['skipped']} skipped, {counts['held']} held, {counts['failed']} failed"
    )
    if notes:
        status += "\n" + "\n".join(notes[:40]) + ("\n…" if len(notes) > 40 else "")
    return status


def _check_settings(settings):
    missing = [
        label
        for field, label in (
            ("company", "Company"),
            ("pemo_account", "Pemo Wallet Account"),
            ("sync_from_date", "Sync From Date"),
            ("default_expense_claim_type", "Default Expense Claim Type"),
        )
        if not settings.get(field)
    ]
    if missing:
        frappe.throw(_("Pemo Settings is missing: {0}").format(", ".join(missing)))


def _skip_reason(txn, settings, currency):
    """(reason, hold). hold=True leaves the record 'ready' in Pemo for a person."""
    if txn.get("transactionType") in importer.CREDIT_TYPES or flt(txn.get("totalAmount")) < 0:
        # A claim books an expense; a refund needs the reverse. Left "ready" in Pemo
        # so it can be booked through a Pemo Transaction Import, which reverses it.
        return "refund/credit — book it with a Pemo Transaction Import", True
    reason = importer._skip_reason(txn, settings, currency)
    if reason and "currency" in reason:
        return reason, True
    return reason, False


def _ids_in_live_imports():
    """{transaction id: import name} for rows in draft or submitted Pemo Transaction Imports."""
    rows = frappe.get_all(
        importer.ROW_DOCTYPE,
        filters={"parenttype": importer.IMPORT_DOCTYPE, "docstatus": ["<", 2], "pemo_transaction_id": ["is", "set"]},
        fields=["pemo_transaction_id", "parent"],
    )
    return {r.pemo_transaction_id: r.parent for r in rows}


def _claims_by_transaction():
    if not frappe.db.has_column(PEC, "pemo_transaction_id"):
        frappe.throw(_("Petty Expense Claim has no Pemo fields yet. Run bench migrate."))
    rows = frappe.get_all(
        PEC,
        filters={"pemo_transaction_id": ["is", "set"]},
        fields=["name", "pemo_transaction_id", "pemo_receipt_pending"],
        order_by="creation asc",
    )
    return {r.pemo_transaction_id: (r.name, bool(r.pemo_receipt_pending)) for r in rows}


# --------------------------------------------------------------------------- #
# Building a claim
# --------------------------------------------------------------------------- #
def _create_claim(txn, settings, currency):
    rows = importer.build_rows(txn, settings.company, currency)
    first = rows[0]
    rate = flt(settings.vat_rate) or 5

    expense_type = settings.pec_expense_type or "Other Expenses"
    project = next((r.get("project") for r in rows if r.get("project")), None)
    header = {}
    if expense_type in PROJECT_EXPENSE_TYPES:
        if project:
            pm, category = frappe.db.get_value(
                "Project", project, ["custom_project_manager", "custom_project_category"]
            ) or (None, None)
            header = {"project": project, "project_manager": pm, "project_category": category}
        if not (header.get("project") and header.get("project_manager") and header.get("project_category")):
            # The PEC requires Project, Project Manager and Project Category for project
            # expense types. Without them the claim could not even be saved.
            expense_type = "Other Expenses"
            header = {}

    details = [_detail_row(r, settings, rate, header.get("project")) for r in rows]

    pec = frappe.get_doc(
        {
            "doctype": PEC,
            "requested_by": _requester(first.get("spender"), settings),
            "expense_type": expense_type,
            "date": first.get("date"),
            "cheque_number": (first.get("pemo_reference") or txn.get("id"))[:140],
            "petty_cash_account": settings.pemo_account,
            "status": "Paid" if settings.mark_pec_paid else "Unpaid",
            "pemo_transaction_id": txn.get("id"),
            "pemo_reference": first.get("pemo_reference"),
            "pemo_cardholder": first.get("cardholder") or first.get("spender"),
            "pemo_supplier": first.get("supplier"),
            "pemo_supplier_trn": first.get("supplier_trn"),
            "pemo_invoice_no": first.get("invoice_no"),
            "pemo_transaction_type": first.get("transaction_type"),
            "petty_expense_details": details,
            "total_incl": flt(sum(d["amount_incl_of_vat"] for d in details), 2),
            "total_excl": flt(sum(d["amount_excl_of_vat"] for d in details), 2),
            **header,
        }
    )
    pec.flags.ignore_permissions = True
    pec.insert()

    receipts_ok = True
    if settings.download_receipts and txn.get("receipts"):
        urls, failed = importer._store_receipts(pec, txn)
        receipts_ok = not failed
        if failed:
            pec.db_set("pemo_receipt_pending", 1, update_modified=False)

    state = (settings.pec_workflow_state or "").strip()
    if state and state != (pec.get("workflow_state") or "Draft"):
        pec.workflow_state = state
        pec.save()
    return pec.name, receipts_ok


def _detail_row(r, settings, rate, project):
    claim_type, account = _expense_claim_type(r, settings)
    gross = flt(r.get("amount_incl"), 2)
    vat_applicable = r.get("vat_applicable") or settings.default_vat_applicable or "No"
    if vat_applicable == "Yes":
        net = flt(gross / (1 + rate / 100), 2)
        vat = flt(gross - net, 2)
    else:
        net, vat = gross, 0.0
    description = " | ".join(
        p for p in (r.get("supplier") or r.get("merchant"), r.get("memo"), r.get("cardholder"), r.get("pemo_reference")) if p
    )
    row = {
        "date": r.get("date"),
        "expense_type": claim_type,
        "description": description[:140],
        "amount_incl_of_vat": gross,
        "vat_applicable": vat_applicable,
        "vat_rate": f"{rate:g}" if vat_applicable == "Yes" else "0",
        "amount_excl_of_vat": net,
        "vat_amount": vat,
        "expense_account": account,
        "vat_account": settings.input_vat_account,
    }
    if project:
        row["project"] = project
    return row


def _expense_claim_type(r, settings):
    """(Expense Claim Type, Expense Account) for a row.

    1. Pemo's category matched an account -> the claim type whose default
       account (for this company) is that account.
    2. Pemo's category value is itself a claim type name.
    3. Settings' Default Expense Claim Type.
    The row keeps the matched account even when the type falls back.
    """
    account = r.get("expense_account")
    if account:
        claim_type = frappe.db.get_value(
            "Expense Claim Account", {"parenttype": "Expense Claim Type", "default_account": account, "company": settings.company}, "parent"
        )
        if claim_type:
            return claim_type, account
    category = (r.get("pemo_category") or "").split(" | ")[0].strip()
    if category and frappe.db.exists("Expense Claim Type", category):
        return category, account or _default_account(category, settings.company)
    claim_type = settings.default_expense_claim_type
    return claim_type, account or _default_account(claim_type, settings.company)


def _default_account(claim_type, company):
    return frappe.db.get_value(
        "Expense Claim Account", {"parenttype": "Expense Claim Type", "parent": claim_type, "company": company}, "default_account"
    )


def _requester(email, settings):
    """The cardholder's ERPNext user when one exists, else the configured fallback."""
    if email:
        user = frappe.db.get_value("User", {"name": email, "enabled": 1}) or frappe.db.get_value(
            "User", {"email": email, "enabled": 1}
        )
        if user:
            return user
    return settings.default_requested_by or "Administrator"


def _retry_receipts(pec_name, txn):
    """Re-download a claim's receipts after a failed first attempt. True when all succeeded.

    Replaces every receipt file this sync attached (names start "pemo-") with a
    fresh full set from this listing's links, so a partial first attempt can't
    leave duplicates or gaps.
    """
    for name in frappe.get_all(
        "File",
        filters={"attached_to_doctype": PEC, "attached_to_name": pec_name, "file_name": ["like", "pemo-%"]},
        pluck="name",
    ):
        frappe.delete_doc("File", name, ignore_permissions=True)
    failed = 0
    if txn.get("receipts"):
        _, failed = importer._store_receipts(frappe.get_doc(PEC, pec_name), txn)
    if not failed:
        frappe.db.set_value(PEC, pec_name, "pemo_receipt_pending", 0, update_modified=False)
    frappe.db.commit()
    return not failed
