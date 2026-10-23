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

        if self.default_expense_claim_type and self.company and not frappe.db.exists(
            "Expense Claim Account",
            {"parenttype": "Expense Claim Type", "parent": self.default_expense_claim_type, "company": self.company},
        ):
            frappe.throw(
                _("Expense Claim Type {0} has no default account for {1}.").format(
                    self.default_expense_claim_type, self.company
                )
            )

        state = (self.pec_workflow_state or "").strip()
        if state:
            from frappe.model.workflow import get_workflow_name

            workflow = get_workflow_name("Petty Expense Claim")
            doc_status = workflow and frappe.db.get_value(
                "Workflow Document State", {"parent": workflow, "state": state}, "doc_status"
            )
            if str(doc_status) != "0":
                frappe.throw(
                    _("Claim Workflow State '{0}' must be a draft state of the active Petty Expense Claim workflow.").format(state)
                )

    @frappe.whitelist()
    def sync_now(self):
        """Run the Petty Expense Claim sync once, in the background."""
        from pemo_sync.pec_sync import _check_settings, enqueue_sync

        _check_settings(self)
        enqueue_sync(user=frappe.session.user)
        return _("Sync started. This form updates when it finishes.")

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
