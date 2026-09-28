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


def after_install():
    create_custom_fields(JOURNAL_ENTRY_FIELDS, ignore_validate=True)
    disable_legacy_scripts()


def after_migrate():
    create_custom_fields(JOURNAL_ENTRY_FIELDS, ignore_validate=True)


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
