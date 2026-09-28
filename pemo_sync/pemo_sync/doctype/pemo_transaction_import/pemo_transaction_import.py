# Copyright (c) 2026, Designsmith and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from pemo_sync import importer, posting

EDITABLE_AFTER_SUBMIT = (
    "expense_account",
    "project",
    "vat_applicable",
    "verified",
    "remarks",
    "memo",
    "invoice_no",
    "vat_account",
    "manual_vat",
    "vat_amount",
)


class PemoTransactionImport(Document):
    def onload(self):
        settings = frappe.get_single("Pemo Settings")
        self.set_onload("vat_rate", flt(settings.vat_rate) or 5)
        self.set_onload("input_vat_account", settings.input_vat_account)

    def validate(self):
        self._apply_defaults()
        self._validate_accounts()
        self._validate_no_duplicates()
        self._calculate()

    def before_submit(self):
        if not self.transactions:
            frappe.throw(_("Nothing to post: fetch transactions first."))
        problems = posting.rows_not_ready(self.transactions)
        if problems:
            frappe.throw(
                "<br>".join(problems[:40])
                + ("<br>…" if len(problems) > 40 else "")
                + "<br><br>"
                + _("Verify these rows, or delete them — deleted rows come back on the next fetch."),
                title=_("{0} row(s) not ready to post").format(len(problems)),
            )

    def on_submit(self):
        posting.create_journal_entries(self)
        posting.enqueue_confirm(self.name)

    def before_update_after_submit(self):
        """Rows whose JE was rejected/cancelled may be corrected after submit; posted rows may not."""
        before = {r.name: r for r in (self.get_doc_before_save() or frappe._dict()).get("transactions") or []}
        for r in self.transactions:
            old = before.get(r.name)
            if old and old.journal_entry and any(
                r.get(f) != old.get(f) for f in EDITABLE_AFTER_SUBMIT
            ):
                frappe.throw(
                    _("Row {0} is posted in {1}. Change the Journal Entry instead.").format(r.idx, old.journal_entry)
                )
        self._calculate()

    def on_cancel(self):
        posting.reverse_journal_entries(self)

    # ------------------------------------------------------------------ #
    def _apply_defaults(self):
        settings = frappe.get_single("Pemo Settings")
        if not self.company:
            self.company = settings.company
        if not self.pemo_account and settings.company == self.company:
            self.pemo_account = settings.pemo_account

    def _validate_accounts(self):
        if self.pemo_account and frappe.db.get_value("Account", self.pemo_account, "company") != self.company:
            frappe.throw(_("Pemo Wallet Account {0} does not belong to {1}.").format(self.pemo_account, self.company))

    def _validate_no_duplicates(self):
        ids = [r.pemo_transaction_id for r in self.transactions if r.pemo_transaction_id]
        if not ids:
            return
        clashes = frappe.get_all(
            "Pemo Transaction Details",
            filters={
                "parenttype": self.doctype,
                "parent": ["!=", self.name],
                "docstatus": ["<", 2],
                "pemo_transaction_id": ["in", list(set(ids))],
            },
            fields=["pemo_transaction_id", "parent"],
        )
        if clashes:
            frappe.throw(
                "<br>".join(_("{0} is already in {1}").format(c.pemo_transaction_id, c.parent) for c in clashes[:20]),
                title=_("Pemo transactions imported twice"),
            )

    def _calculate(self):
        settings = frappe.get_single("Pemo Settings")
        rate = flt(settings.vat_rate) or 5
        default_vat_account = settings.input_vat_account if settings.company == self.company else None

        net = 0.0
        for r in self.transactions:
            gross = flt(r.amount_incl, 2)
            net += -gross if r.is_credit else gross
            if r.journal_entry:
                continue
            if r.vat_applicable == "Yes":
                r.tax_rate = rate
                if not r.manual_vat:
                    r.vat_amount = flt(gross - gross / (1 + rate / 100), 2)
                r.vat_amount = flt(r.vat_amount, 2)
                r.amount_excl_vat = flt(gross - r.vat_amount, 2)
                r.vat_account = r.vat_account or default_vat_account
            else:
                r.tax_rate = 0
                r.vat_amount = 0
                r.amount_excl_vat = gross
                r.manual_vat = 0
                if r.vat_applicable == "No":
                    r.vat_account = None

        self.total_transactions = len(self.transactions)
        self.total_amount = flt(net, 2)
        self.posted_transactions = len([r for r in self.transactions if r.journal_entry])


# --------------------------------------------------------------------------- #
# Form actions
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def get_form_defaults():
    """Non-secret Pemo Settings for a new form. Accounts Users can't read the Single itself."""
    if not frappe.has_permission("Pemo Transaction Import", "create"):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    settings = frappe.get_single("Pemo Settings")
    return {
        "company": settings.company,
        "pemo_account": settings.pemo_account,
        "vat_rate": flt(settings.vat_rate) or 5,
        "input_vat_account": settings.input_vat_account,
    }


@frappe.whitelist()
def fetch_transactions(name):
    importer.enqueue_fetch(name)
    return _("Fetching from Pemo in the background. The form reloads when it finishes.")


@frappe.whitelist()
def create_missing_journal_entries(name):
    """Re-post rows on a submitted import whose JE was rejected/deleted or cancelled."""
    doc = frappe.get_doc("Pemo Transaction Import", name)
    doc.check_permission("submit")
    if doc.docstatus != 1:
        frappe.throw(_("Only for a submitted import."))
    problems = posting.rows_not_ready(doc.transactions)
    if problems:
        frappe.throw("<br>".join(problems[:40]), title=_("Rows not ready to post"))
    created = posting.create_journal_entries(doc)
    if created:
        posting.enqueue_confirm(name)
    return created


@frappe.whitelist()
def retry_export_confirmation(name):
    doc = frappe.get_doc("Pemo Transaction Import", name)
    doc.check_permission("submit")
    if doc.docstatus != 1:
        frappe.throw(_("Only for a submitted import."))
    posting.enqueue_confirm(name)
