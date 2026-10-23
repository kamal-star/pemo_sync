# Copyright (c) 2026, Designsmith and contributors
# For license information, please see license.txt

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

PEMO_DOCTYPES = ("Pemo Transaction Import", "Pemo Transaction Details")

JOURNAL_ENTRY_FIELDS = {
    "Journal Entry": [
        {
            "fieldname": "pemo_import",
            "label": "Pemo Import",
            "fieldtype": "Link",
            "options": "Pemo Transaction Import",
            "insert_after": "cheque_date",
            "read_only": 1,
            "no_copy": 1,
            "print_hide": 1,
        },
        {
            "fieldname": "pemo_transaction_id",
            "label": "Pemo Transaction ID",
            "fieldtype": "Data",
            "insert_after": "pemo_import",
            "read_only": 1,
            "no_copy": 1,
            "print_hide": 1,
            "search_index": 1,
        },
    ]
}


def _pec_field(fieldname, label, insert_after, **kw):
    return {
        "fieldname": fieldname,
        "label": label,
        "fieldtype": "Data",
        "insert_after": insert_after,
        "read_only": 1,
        "no_copy": 0,
        "print_hide": 1,
        **kw,
    }


# Designsmith's own Petty Expense Claim (a UI-made doctype, not part of this app).
# The sync creates claims; these fields trace each one back to Pemo.
PETTY_EXPENSE_CLAIM_FIELDS = {
    "Petty Expense Claim": [
        {
            "fieldname": "pemo_section",
            "label": "Pemo",
            "fieldtype": "Section Break",
            "insert_after": "total_excl",
            "collapsible": 1,
            "depends_on": "eval:doc.pemo_transaction_id",
        },
        _pec_field("pemo_transaction_id", "Pemo Transaction ID", "pemo_section", search_index=1),
        _pec_field("pemo_reference", "Pemo Reference", "pemo_transaction_id"),
        _pec_field("pemo_transaction_type", "Pemo Transaction Type", "pemo_reference"),
        {"fieldname": "pemo_column_break", "fieldtype": "Column Break", "insert_after": "pemo_transaction_type"},
        _pec_field("pemo_cardholder", "Cardholder", "pemo_column_break"),
        _pec_field("pemo_supplier", "Supplier (Pemo)", "pemo_cardholder"),
        _pec_field("pemo_supplier_trn", "Supplier TRN", "pemo_supplier"),
        _pec_field("pemo_invoice_no", "Invoice No", "pemo_supplier_trn"),
        _pec_field(
            "pemo_receipt_pending", "Receipt Download Pending", "pemo_invoice_no", fieldtype="Check", hidden=1, default="0"
        ),
    ]
}


def _create_fields():
    create_custom_fields(JOURNAL_ENTRY_FIELDS, ignore_validate=True)
    if frappe.db.exists("DocType", "Petty Expense Claim"):
        create_custom_fields(PETTY_EXPENSE_CLAIM_FIELDS, ignore_validate=True)


def after_install():
    _create_fields()
    disable_legacy_scripts()


def after_migrate():
    _create_fields()


def disable_legacy_scripts():
    """Turn off the UMI-era scripts on the Pemo doctypes.

    They call `apiapp.*` (not installed here), hardcode UMI's VAT accounts, and
    "Create Jv" posts on submit — which would double every Journal Entry now
    that this app posts them. Disabled, not deleted, so they stay readable.
    """
    disabled = []
    for name in frappe.get_all(
        "Server Script", filters={"reference_doctype": ["in", PEMO_DOCTYPES], "disabled": 0}, pluck="name"
    ):
        frappe.db.set_value("Server Script", name, "disabled", 1)
        disabled.append(f"Server Script: {name}")
    for name in frappe.get_all("Client Script", filters={"dt": ["in", PEMO_DOCTYPES], "enabled": 1}, pluck="name"):
        frappe.db.set_value("Client Script", name, "enabled", 0)
        disabled.append(f"Client Script: {name}")
    if disabled:
        frappe.clear_cache()
        print("pemo_sync: disabled legacy scripts -> " + ", ".join(disabled))
