import frappe


COMPANY_FIELDS = [
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
]


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


def after_install():
    _ensure_company_fields()
    _ensure_server_script_proxies()
    frappe.db.commit()

