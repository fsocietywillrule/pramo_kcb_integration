import json
from datetime import date

import frappe
from frappe.utils import flt, now_datetime, today

from pramo_kcb_integration.crypto import verify_rsa_signature


CALLBACK_METHODS = {
    "kcb_mpesa_callback": "M-Pesa Callback",
    "kcb_mpesa_ipn": "M-Pesa IPN",
    "kcb_till_notification": "Till Notification",
    "kcb_account_notification": "Account Notification",
    "kcb_ft_callback": "Funds Transfer",
    "kcb_validation": "Validation",
}


def _request_payload() -> dict:
    if frappe.form_dict:
        data = frappe._dict(frappe.form_dict)
        data.pop("cmd", None)
        return dict(data)
    return {}


def _request_body_bytes() -> bytes:
    try:
        return frappe.request.get_data() or b""
    except Exception:
        return b""


def _request_json() -> dict:
    payload = _request_payload()
    body = _request_body_bytes()

    if body:
        try:
            body_payload = json.loads(body.decode("utf-8"))
            if isinstance(body_payload, dict):
                payload.update(body_payload)
        except Exception:
            pass

    return payload


def _headers() -> dict:
    try:
        return dict(frappe.request.headers)
    except Exception:
        return {}


def _header_value(headers: dict, *names: str) -> str:
    lowered = {str(k).lower(): v for k, v in headers.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value:
            return str(value)
    return ""


def _first(payload: dict, *names: str) -> str:
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None:
            return str(value)
    return ""


def _amount(payload: dict) -> float:
    return flt(_first(payload, "amount", "Amount", "transactionAmount", "TransAmount"))


def _transaction_reference(payload: dict) -> str:
    return _first(
        payload,
        "transactionReference",
        "transaction_reference",
        "TransID",
        "transID",
        "receiptNumber",
        "mpesaReceiptNumber",
        "retrievalReferenceNumber",
        "retrievalRefNumber",
        "requestId",
        "messageId",
    )


def _invoice_reference(payload: dict) -> str:
    return _first(
        payload,
        "invoiceNumber",
        "accountReference",
        "account_reference",
        "BillRefNumber",
        "billRefNumber",
        "customerReference",
        "externalReference",
    )


def _reference_without_paybill(reference: str) -> str:
    if not reference:
        return ""
    for separator in ("#", "-"):
        if separator in reference:
            parts = reference.split(separator, 1)
            if len(parts) == 2 and parts[1]:
                return parts[1]
    return reference


def _company_for_payload(payload: dict) -> str:
    invoice_reference = _invoice_reference(payload)
    base = ""
    if "#" in invoice_reference:
        base = invoice_reference.split("#", 1)[0]
    elif "-" in invoice_reference:
        base = invoice_reference.split("-", 1)[0]

    if base:
        company = frappe.db.get_value("Company", {"custom_kcb_invoice_number_base": base}, "name")
        if company:
            return company

    default_company = frappe.defaults.get_user_default("Company")
    if default_company:
        return default_company

    return frappe.db.get_single_value("Global Defaults", "default_company") or ""


def _company_config(company: str) -> frappe._dict:
    if not company:
        return frappe._dict()
    fields = [
        "custom_kcb_environment",
        "custom_kcb_prod_public_key",
        "custom_kcb_uat_public_key",
        "custom_kcb_auto_create_payments",
        "custom_kcb_default_bank_account",
        "custom_kcb_default_mode_of_payment",
        "custom_kcb_auto_submit_payment_entries",
    ]
    existing = {row.get("Field") for row in frappe.db.sql("show columns from `tabCompany`", as_dict=True)}
    usable = [field for field in fields if field in existing]
    if not usable:
        return frappe._dict()
    return frappe._dict(frappe.db.get_value("Company", company, usable, as_dict=True) or {})


def _environment(config: frappe._dict) -> str:
    return (config.get("custom_kcb_environment") or "Production").strip()


def _public_key_for_environment(config: frappe._dict) -> str:
    env = _environment(config).lower()
    if env == "sandbox":
        return config.get("custom_kcb_uat_public_key") or ""
    return config.get("custom_kcb_prod_public_key") or ""


def _signature_status(company: str, config: frappe._dict, payload: dict, headers: dict) -> tuple[bool, str, str]:
    signature = _header_value(headers, "Signature", "X-Signature", "x-kcb-signature")
    env = _environment(config)
    if env.lower() == "sandbox" and not signature:
        return True, "Sandbox request without signature accepted", signature

    public_key = _public_key_for_environment(config)
    ok, message = verify_rsa_signature(public_key, signature, _request_body_bytes())
    return ok, message, signature


def _find_sales_invoice(reference: str) -> str:
    invoice_name = _reference_without_paybill(reference)
    if invoice_name and frappe.db.exists("Sales Invoice", invoice_name):
        return invoice_name
    return ""


def _log_exists(transaction_reference: str) -> bool:
    if not transaction_reference:
        return False
    if not frappe.db.exists("DocType", "KCB Integration Log"):
        return False
    return bool(
        frappe.db.exists(
            "KCB Integration Log",
            {"transaction_reference": transaction_reference, "processing_status": ["!=", "Rejected"]},
        )
    )


def _write_log(
    callback_type: str,
    company: str,
    payload: dict,
    headers: dict,
    signature_header: str,
    processing_status: str,
    error_message: str = "",
    linked_sales_invoice: str = "",
    linked_payment_entry: str = "",
) -> str:
    if not frappe.db.exists("DocType", "KCB Integration Log"):
        frappe.log_error(json.dumps(payload, indent=2), f"KCB {callback_type}: {processing_status}")
        return ""

    doc = frappe.new_doc("KCB Integration Log")
    doc.callback_type = callback_type
    doc.transaction_reference = _transaction_reference(payload)
    doc.external_reference = _invoice_reference(payload)
    doc.status = _first(payload, "status", "ResultCode", "resultCode") or "RECEIVED"
    doc.amount = _amount(payload)
    doc.currency = _first(payload, "currency") or "KES"
    doc.phone_number = _first(payload, "phoneNumber", "MSISDN", "msisdn")
    doc.account_reference = _invoice_reference(payload)
    doc.received_on = now_datetime()
    doc.processing_status = processing_status
    doc.linked_sales_invoice = linked_sales_invoice
    doc.linked_payment_entry = linked_payment_entry
    doc.request_ip = getattr(getattr(frappe, "local", None), "request_ip", "") or ""
    doc.raw_payload = json.dumps(payload, indent=2, sort_keys=True, default=str)
    doc.headers = json.dumps(headers, indent=2, sort_keys=True, default=str)
    doc.response_json = ""
    doc.error_message = error_message
    doc.kcb_endpoint = frappe.local.request.path if getattr(frappe.local, "request", None) else ""
    doc.environment = _environment(_company_config(company))
    doc.company = company
    doc.signature_header = signature_header
    doc.request_id = _first(payload, "requestId", "messageId")
    doc.customer_reference = _invoice_reference(payload)
    doc.checkout_request_id = _first(payload, "checkoutRequestID", "CheckoutRequestID")
    doc.merchant_request_id = _first(payload, "merchantRequestID", "MerchantRequestID")
    doc.message_id = _first(payload, "messageId")
    doc.retrieval_ref_number = _first(payload, "retrievalReferenceNumber", "retrievalRefNumber")
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return doc.name


def _create_payment_entry(company: str, config: frappe._dict, payload: dict, invoice_name: str) -> str:
    if not invoice_name:
        return ""
    if not config.get("custom_kcb_auto_create_payments"):
        return ""

    bank_account = config.get("custom_kcb_default_bank_account")
    if not bank_account:
        frappe.throw("KCB default bank account is not configured on Company")

    invoice = frappe.get_doc("Sales Invoice", invoice_name)
    amount = _amount(payload)
    if amount <= 0:
        frappe.throw("KCB amount is missing or zero")

    from erpnext.accounts.party import get_party_account

    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Receive"
    pe.company = company
    pe.posting_date = today()
    pe.mode_of_payment = config.get("custom_kcb_default_mode_of_payment") or "M-Pesa"
    pe.party_type = "Customer"
    pe.party = invoice.customer
    pe.paid_from = get_party_account("Customer", invoice.customer, company)
    pe.paid_to = bank_account
    pe.paid_amount = amount
    pe.received_amount = amount
    pe.reference_no = _transaction_reference(payload)
    pe.reference_date = date.today()
    pe.append(
        "references",
        {
            "reference_doctype": "Sales Invoice",
            "reference_name": invoice.name,
            "allocated_amount": min(amount, flt(invoice.outstanding_amount)),
        },
    )
    pe.insert(ignore_permissions=True)
    if config.get("custom_kcb_auto_submit_payment_entries"):
        pe.submit()
    frappe.db.commit()
    return pe.name


def handle_callback(endpoint: str) -> dict:
    callback_type = CALLBACK_METHODS.get(endpoint, endpoint)
    payload = _request_json()
    headers = _headers()
    company = _company_for_payload(payload)
    config = _company_config(company)
    transaction_reference = _transaction_reference(payload)
    invoice_reference = _invoice_reference(payload)
    invoice_name = _find_sales_invoice(invoice_reference)

    if transaction_reference and _log_exists(transaction_reference):
        return {
            "status": "SUCCESS",
            "message": "Duplicate callback ignored",
            "duplicate": True,
            "transaction_reference": transaction_reference,
        }

    signature_ok, signature_message, signature_header = _signature_status(company, config, payload, headers)
    if not signature_ok:
        log_name = _write_log(
            callback_type,
            company,
            payload,
            headers,
            signature_header,
            "Rejected",
            signature_message,
            invoice_name,
        )
        frappe.local.response["http_status_code"] = 401
        return {
            "status": "ERROR",
            "message": signature_message,
            "log": log_name,
        }

    payment_entry = ""
    processing_status = "Verified"
    error_message = ""
    try:
        payment_entry = _create_payment_entry(company, config, payload, invoice_name)
        if payment_entry:
            processing_status = "Payment Entry Created"
        elif config.get("custom_kcb_auto_create_payments"):
            processing_status = "Pending Manual Review"
            error_message = "No matching Sales Invoice or missing configuration"
        else:
            processing_status = "Verified - Log Only"
    except Exception as exc:
        processing_status = "Payment Entry Failed"
        error_message = str(exc)

    log_name = _write_log(
        callback_type,
        company,
        payload,
        headers,
        signature_header,
        processing_status,
        error_message,
        invoice_name,
        payment_entry,
    )

    return {
        "status": "SUCCESS",
        "message": processing_status,
        "log": log_name,
        "company": company,
        "sales_invoice": invoice_name,
        "payment_entry": payment_entry,
    }


@frappe.whitelist(allow_guest=True)
def kcb_mpesa_callback():
    return handle_callback("kcb_mpesa_callback")


@frappe.whitelist(allow_guest=True)
def kcb_mpesa_ipn():
    return handle_callback("kcb_mpesa_ipn")


@frappe.whitelist(allow_guest=True)
def kcb_till_notification():
    return handle_callback("kcb_till_notification")


@frappe.whitelist(allow_guest=True)
def kcb_account_notification():
    return handle_callback("kcb_account_notification")


@frappe.whitelist(allow_guest=True)
def kcb_ft_callback():
    return handle_callback("kcb_ft_callback")


@frappe.whitelist(allow_guest=True)
def kcb_validation():
    return handle_callback("kcb_validation")

