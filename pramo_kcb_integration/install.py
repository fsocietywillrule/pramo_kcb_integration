import frappe


COMPANY_FIELDS = [
    {
        "fieldname": "custom_kcb_shared_shortcode",
        "label": "KCB Shared Shortcode",
        "fieldtype": "Check",
        "default": "1",
        "insert_after": "custom_kcb_invoice_number_base",
    },
    {
        "fieldname": "custom_kcb_org_shortcode",
        "label": "KCB Org Shortcode",
        "fieldtype": "Data",
        "insert_after": "custom_kcb_shared_shortcode",
    },
    {
        "fieldname": "custom_kcb_org_passkey",
        "label": "KCB Org PassKey",
        "fieldtype": "Password",
        "insert_after": "custom_kcb_org_shortcode",
    },
    {
        "fieldname": "custom_kcb_prod_stk_push_url",
        "label": "KCB Production STK Push URL",
        "fieldtype": "Data",
        "default": "https://buni.kcbgroup.com/mm/api/request/1.0.0/stkpush",
        "insert_after": "custom_kcb_prod_token_endpoint",
    },
    {
        "fieldname": "custom_kcb_uat_token_endpoint",
        "label": "KCB UAT Token Endpoint",
        "fieldtype": "Data",
        "default": "https://uat.buni.kcbgroup.com/token?grant_type=client_credentials",
        "insert_after": "custom_kcb_prod_stk_push_url",
    },
    {
        "fieldname": "custom_kcb_uat_stk_push_url",
        "label": "KCB UAT STK Push URL",
        "fieldtype": "Data",
        "default": "https://uat.buni.kcbgroup.com/mm/api/request/1.0.0/stkpush",
        "insert_after": "custom_kcb_uat_token_endpoint",
    },
    {
        "fieldname": "custom_kcb_default_bank_account",
        "label": "KCB Default Bank Account",
        "fieldtype": "Link",
        "options": "Account",
        "insert_after": "custom_kcb_ft_callback_url",
    },
    {
        "fieldname": "custom_kcb_default_mode_of_payment",
        "label": "KCB Default Mode of Payment",
        "fieldtype": "Link",
        "options": "Mode of Payment",
        "insert_after": "custom_kcb_default_bank_account",
    },
    {
        "fieldname": "custom_kcb_auto_submit_payment_entries",
        "label": "KCB Auto Submit Payment Entries",
        "fieldtype": "Check",
        "default": "0",
        "insert_after": "custom_kcb_default_mode_of_payment",
    },
]

PROXY_ENDPOINTS = [
    "kcb_mpesa_callback",
    "kcb_mpesa_ipn",
    "kcb_till_notification",
    "kcb_account_notification",
    "kcb_ft_callback",
    "kcb_validation",
    "kcb_stk_push",
]


SALES_INVOICE_CLIENT_SCRIPT = r"""
frappe.ui.form.on('Sales Invoice', {
    refresh(frm) {
        if (frm.doc.docstatus !== 1 || flt(frm.doc.outstanding_amount) <= 0) {
            return;
        }

        frappe.db.get_value('Company', frm.doc.company, 'custom_kcb_invoice_number_base')
            .then((res) => {
                const base = res && res.message && res.message.custom_kcb_invoice_number_base;
                if (!base || frm.__kcb_button_added) {
                    return;
                }
                frm.__kcb_button_added = true;

                frm.add_custom_button(__('Send M-Pesa Request'), () => {
                    frappe.prompt([
                        {
                            fieldname: 'phone_number',
                            label: 'Customer M-Pesa Phone',
                            fieldtype: 'Data',
                            reqd: 1,
                            default: frm.doc.contact_mobile || frm.doc.customer_mobile_no || ''
                        },
                        {
                            fieldname: 'amount',
                            label: 'Amount',
                            fieldtype: 'Currency',
                            reqd: 1,
                            default: frm.doc.outstanding_amount || frm.doc.rounded_total || frm.doc.grand_total
                        }
                    ], (values) => {
                        frappe.call({
                            method: 'pramo_kcb_integration.api.kcb_stk_push',
                            args: {
                                sales_invoice: frm.doc.name,
                                phone_number: values.phone_number,
                                amount: values.amount
                            },
                            freeze: true,
                            freeze_message: __('Sending KCB M-Pesa request...'),
                            callback: (r) => {
                                if (r.message) {
                                    frappe.msgprint({
                                        title: __('KCB M-Pesa Request Sent'),
                                        indicator: 'green',
                                        message: __(r.message.message || 'STK Push sent to customer phone')
                                    });
                                }
                                frm.reload_doc();
                            }
                        });
                    }, __('KCB M-Pesa Request'), __('Send Request'));
                }, __('KCB'));
            });
    }
});
"""


def _ensure_company_fields():
    for field in COMPANY_FIELDS:
        name = f"Company-{field['fieldname']}"
        if frappe.db.exists("Custom Field", name):
            continue
        doc = frappe.get_doc(
            {
                "doctype": "Custom Field",
                "dt": "Company",
                "name": name,
                "module": "Core",
                **field,
            }
        )
        doc.insert(ignore_permissions=True)


def _ensure_server_script_proxies():
    if not frappe.db.exists("DocType", "Server Script"):
        return

    for endpoint in PROXY_ENDPOINTS:
        if endpoint == "kcb_stk_push":
            script = (
                "args = dict(frappe.form_dict)\n"
                "args.pop('cmd', None)\n"
                "frappe.response['message'] = "
                "frappe.call('pramo_kcb_integration.api.kcb_stk_push', **args)"
            )
        else:
            script = (
                "frappe.response['message'] = "
                f"frappe.call('pramo_kcb_integration.api.{endpoint}')"
            )
        if frappe.db.exists("Server Script", endpoint):
            doc = frappe.get_doc("Server Script", endpoint)
            doc.script_type = "API"
            doc.api_method = endpoint
            doc.script = script
            doc.disabled = 0
            doc.save(ignore_permissions=True)
        else:
            doc = frappe.get_doc(
                {
                    "doctype": "Server Script",
                    "name": endpoint,
                    "script_type": "API",
                    "api_method": endpoint,
                    "script": script,
                    "disabled": 0,
                }
            )
            doc.insert(ignore_permissions=True)


def _ensure_sales_invoice_client_script():
    if not frappe.db.exists("DocType", "Client Script"):
        return

    name = "KCB Sales Invoice Button"
    if frappe.db.exists("Client Script", name):
        doc = frappe.get_doc("Client Script", name)
        doc.dt = "Sales Invoice"
        doc.script = SALES_INVOICE_CLIENT_SCRIPT
        doc.enabled = 1
        doc.save(ignore_permissions=True)
    else:
        frappe.get_doc(
            {
                "doctype": "Client Script",
                "name": name,
                "dt": "Sales Invoice",
                "enabled": 1,
                "script": SALES_INVOICE_CLIENT_SCRIPT,
            }
        ).insert(ignore_permissions=True)


def after_install():
    _ensure_company_fields()
    _ensure_server_script_proxies()
    _ensure_sales_invoice_client_script()
    frappe.db.commit()


def after_migrate():
    after_install()
