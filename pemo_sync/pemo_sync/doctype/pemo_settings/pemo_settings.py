# Copyright (c) 2026, Designsmith and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class PemoSettings(Document):
    def validate(self):
        for field in ("pemo_account", "input_vat_account", "default_cost_center"):
            value = self.get(field)
            if not (value and self.company):
                continue
            doctype = "Cost Center" if field == "default_cost_center" else "Account"
            if frappe.db.get_value(doctype, value, "company") != self.company:
                frappe.throw(_("{0} does not belong to {1}.").format(value, self.company))

    @frappe.whitelist()
    def test_connection(self):
        """Read one page of 1 record. Proves the key and host without changing anything."""
        from pemo_sync.client import PemoAPIError, PemoClient

        client = PemoClient(self)
        try:
            data = client._request("GET", "/transactions", params={"limit": 1})
        except PemoAPIError as e:
            hint = _("Check the key matches the Environment.") if "401" in str(e) else ""
            frappe.throw(f"{e}<br>{hint}", title=_("Pemo connection failed"))
        return _("Connected to Pemo {0}. {1} ready-to-export transaction(s) on the first page.").format(
            self.environment, len((data or {}).get("transactions") or [])
        )
