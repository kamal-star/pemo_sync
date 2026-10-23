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

	onload(frm) {
		frappe.realtime.off("pemo_pec_sync_done");
		frappe.realtime.on("pemo_pec_sync_done", (data) => {
			const ok = (data.message || "").startsWith("OK");
			frappe.show_alert({ message: (data.message || "").split("\n")[0], indicator: ok ? "green" : "red" }, 15);
			frm.reload_doc();
		});
	},

	refresh(frm) {
		frm.set_query("default_requested_by", () => ({ filters: { enabled: 1, user_type: "System User" } }));
		frm.add_custom_button(__("Sync Now"), () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save first."));
				return;
			}
			frm.call({ method: "sync_now", doc: frm.doc, freeze: true }).then((r) => {
				if (r.message) frappe.show_alert({ message: r.message, indicator: "blue" }, 10);
			});
		});
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
