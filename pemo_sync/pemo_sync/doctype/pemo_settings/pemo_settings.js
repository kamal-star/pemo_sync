// Copyright (c) 2026, Designsmith and contributors
// For license information, please see license.txt

frappe.ui.form.on("Pemo Settings", {
	setup(frm) {
		frm.set_query("pemo_account", () => ({
			filters: { company: frm.doc.company, is_group: 0, root_type: "Asset" },
		}));
		frm.set_query("input_vat_account", () => ({
			filters: { company: frm.doc.company, is_group: 0, account_type: "Tax" },
		}));
		frm.set_query("default_cost_center", () => ({
			filters: { company: frm.doc.company, is_group: 0 },
		}));
	},

	refresh(frm) {
		frm.add_custom_button(__("Test Connection"), () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save first."));
				return;
			}
			frm.call({ method: "test_connection", doc: frm.doc, freeze: true }).then((r) => {
				if (r.message) frappe.msgprint(r.message);
			});
		});
	},
});
