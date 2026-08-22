import json
import base64
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

import frappe
from frappe.utils import flt, now_datetime, today
from frappe.utils.password import get_decrypted_password

from pramo_kcb_integration.crypto import verify_rsa_signature


# Values MUST be valid "KCB Integration Log".callback_type Select options:
# M-Pesa, Funds Transfer, Till Notification, Account Notification, Validation, STK Push, Other
CALLBACK_METHODS = {
    "kcb_mpesa_callback": "M-Pesa",
    "kcb_mpesa_ipn": "M-Pesa",
    "kcb_till_notification": "Till Notification",
    "kcb_account_notification": "Account Notification",
    "kcb_ft_callback": "Funds Transfer",
    "kcb_validation": "Validation",
}

# The "KCB Integration Log" Processing Status field only allows these values:
# Received, Duplicate, Pending Spec, Pending Signature Setup, Pending Config,
# Processed, Error. Every internal status string used below must map to one
# of them before being written to the doc.
STATUS_MAP = {
    "Received": "Received",
    "Duplicate": "Duplicate",
    "Rejected": "Error",
    "Verified": "Processed",
    "Verified - Log Only": "Processed",
    "Payment Entry Created": "Processed",
    "Pending Manual Review": "Pending Config",
    "Payment Entry Failed": "Error",
    "STK Push Sent": "Processed",
    "STK Push Failed": "Error",
}

# Mapped statuses that should NOT block a transaction_reference from being
# retried by _log_exists().
NON_BLOCKING_STATUSES = {"Error", "Pending Config"}


def _mapped_status(status: str) -> str:
    return STATUS_MAP.get(status, "Error")


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
    return flt(
        _first(
            payload,
            "amount",
            "Amount",
            "transactionAmount",
            "transactionAmt",
            "TransAmount",
            "debitAmount",
            "creditAmount",
        )
    )


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
        "businessKey",
        "customerReference",
        "externalReference",
    )


def _known_bill_bases() -> dict:
    """{invoice_number_base: company}. Only these count as a paybill prefix."""
    bases = {}
    for row in frappe.get_all(
        "Company",
        filters={"custom_kcb_invoice_number_base": ["!=", ""]},
        fields=["name", "custom_kcb_invoice_number_base"],
    ):
        base = (row.get("custom_kcb_invoice_number_base") or "").strip()
        if base:
            bases[base] = row.get("name")
    return bases


def _split_reference(reference: str) -> tuple:
    """Return (company_from_prefix, invoice_reference).

    A prefix is only stripped when it matches a Company's bill base. "-" is part of
    every invoice name (SINV-MSA-00028), so splitting on it blindly derived base
    "SINV", matched no company, and silently fell back to the default company.
    """
    if not reference:
        return "", ""
    reference = reference.strip()
    bases = _known_bill_bases()
    for separator in ("#", "-"):
        if separator in reference:
            head, tail = reference.split(separator, 1)
            head, tail = head.strip(), tail.strip()
            if tail and head in bases:
                return bases[head], tail
    return "", reference


def _reference_without_paybill(reference: str) -> str:
    return _split_reference(reference)[1]


def _company_for_payload(payload: dict) -> str:
    company, invoice_reference = _split_reference(_invoice_reference(payload))
    if company:
        return company

    # No recognised paybill prefix (customer typed the bare invoice number).
    # Take the company off the invoice itself rather than guessing.
    if invoice_reference and frappe.db.exists("Sales Invoice", invoice_reference):
        company = frappe.db.get_value("Sales Invoice", invoice_reference, "company")
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
        "custom_kcb_buni_username",
        "custom_kcb_invoice_number_base",
        "custom_kcb_credit_account",
        "custom_kcb_mpesa_callback_url",
        "custom_kcb_prod_consumer_key",
        "custom_kcb_prod_token_endpoint",
        "custom_kcb_prod_stk_push_url",
        "custom_kcb_uat_token_endpoint",
        "custom_kcb_uat_stk_push_url",
        "custom_kcb_shared_shortcode",
        "custom_kcb_org_shortcode",
        "custom_kcb_org_passkey",
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


def _stk_origin(payload: dict) -> dict:
    """The outbound STK Push log row this callback came from, found by CheckoutRequestID.

    An M-Pesa STK result callback carries NO invoice reference - Daraja never echoes
    the account reference back. Verified against production 2026-08-20: the payload
    holds only Amount, MpesaReceiptNumber, Balance, TransactionDate, PhoneNumber and
    the two request ids. The only link to the invoice and company is our own push.
    """
    checkout_id = _first(payload, "CheckoutRequestID", "checkoutRequestID", "checkout_request_id")
    if not checkout_id:
        return {}
    if not frappe.db.exists("DocType", "KCB Integration Log"):
        return {}
    return frappe.db.get_value(
        "KCB Integration Log",
        {"callback_type": "STK Push", "checkout_request_id": checkout_id},
        ["company", "linked_sales_invoice", "owner"],
        as_dict=True,
    ) or {}


def _can_post_payments(user: str) -> bool:
    """Could this user create a Payment Entry if they were logged in?"""
    if not user or user == "Guest":
        return False
    if user == "Administrator":
        return True
    if not frappe.db.get_value("User", user, "enabled"):
        return False
    try:
        return bool(frappe.has_permission("Payment Entry", "create", user=user))
    except Exception:
        return False


def _posting_user(company: str, config: frappe._dict, endpoint: str, payload: dict) -> str:
    """Which user the Payment Entry should be written as.

    A callback arrives as Guest, and Guest cannot create a Payment Entry: ERPNext's
    get_account_details() runs its own frappe.has_permission("Payment Entry",
    throw=True), which pe.insert(ignore_permissions=True) never reaches.

    For an STK push we know exactly who asked the customer for the money, so the
    entry is credited to them. That keeps a real name on the document instead of
    Administrator, and keeps their Company user permission in force. Direct paybill
    deposits have no originating user, so they fall back to the configured posting
    user and finally to Administrator.
    """
    candidates = []
    if endpoint == "kcb_mpesa_callback":
        candidates.append(_stk_origin(payload).get("owner"))
    candidates.append(config.get("custom_kcb_posting_user"))
    for user in candidates:
        if user and _can_post_payments(user):
            return user
    return "Administrator"


def _stk_callback_is_ours(payload: dict) -> bool:
    """Trust an unsigned STK result only when its CheckoutRequestID matches a push
    this system actually sent. A forger cannot guess an id we generated."""
    return bool(_stk_origin(payload))


def _signature_status(company: str, config: frappe._dict, payload: dict, headers: dict,
                      endpoint: str = "") -> tuple[bool, str, str]:
    signature = _header_value(headers, "Signature", "X-Signature", "x-kcb-signature")
    env = _environment(config)
    if env.lower() == "sandbox" and not signature:
        return True, "Sandbox request without signature accepted", signature

    # KCB does NOT sign M-Pesa STK result callbacks. Verified against production on
    # 2026-08-20: the callback carried 13 headers, none of them a signature. Those are
    # authenticated by matching the CheckoutRequestID back to our own push instead.
    # Scoped to the endpoint on purpose - kcb_mpesa_ipn shares callback_type "M-Pesa"
    # but is the C2B paybill path, has no CheckoutRequestID, and MUST stay signed.
    if endpoint == "kcb_mpesa_callback" and not signature:
        if _stk_callback_is_ours(payload):
            return True, "Verified by CheckoutRequestID", signature
        return False, "Unsigned STK callback with unknown CheckoutRequestID", signature

    public_key = _public_key_for_environment(config)
    ok, message = verify_rsa_signature(public_key, signature, _request_body_bytes())
    return ok, message, signature


def _find_sales_invoice(reference: str) -> str:
    invoice_name = _reference_without_paybill(reference)
    if invoice_name and frappe.db.exists("Sales Invoice", invoice_name):
        return invoice_name
    return ""


def _decrypted_company_password(company: str, fieldname: str) -> str:
    try:
        return get_decrypted_password("Company", company, fieldname, raise_exception=False) or ""
    except Exception:
        return ""


def _as_money_string(value) -> str:
    amount = Decimal(str(flt(value))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = format(amount, "f")
    if text.endswith(".00"):
        return text[:-3]
    if text.endswith("0"):
        return text[:-1]
    return text


def _normalize_phone(phone: str) -> str:
    phone = str(phone or "").strip().replace(" ", "").replace("-", "")
    if phone.startswith("+"):
        phone = phone[1:]
    if phone.startswith("0") and len(phone) >= 10:
        phone = "254" + phone[1:]
    if phone.startswith("7") and len(phone) == 9:
        phone = "254" + phone
    return phone


def _stk_url(config: frappe._dict) -> str:
    if _environment(config).lower() == "sandbox":
        return (
            config.get("custom_kcb_uat_stk_push_url")
            or "https://uat.buni.kcbgroup.com/mm/api/request/1.0.0/stkpush"
        )
    return (
        config.get("custom_kcb_prod_stk_push_url")
        or "https://buni.kcbgroup.com/mm/api/request/1.0.0/stkpush"
    )


def _token_url(config: frappe._dict) -> str:
    if _environment(config).lower() == "sandbox":
        return (
            config.get("custom_kcb_uat_token_endpoint")
            or "https://uat.buni.kcbgroup.com/token?grant_type=client_credentials"
        )
    return (
        config.get("custom_kcb_prod_token_endpoint")
        or "https://accounts.buni.kcbgroup.com/oauth2/token"
    )


def _get_access_token(company: str, config: frappe._dict) -> str:
    import requests

    consumer_key = config.get("custom_kcb_prod_consumer_key") or ""
    consumer_secret = _decrypted_company_password(company, "custom_kcb_prod_consumer_secret")
    if _environment(config).lower() == "sandbox":
        # Sandbox credentials were entered into the same fields during UAT on this site.
        consumer_key = consumer_key or config.get("custom_kcb_uat_consumer_key") or ""
        consumer_secret = consumer_secret or _decrypted_company_password(company, "custom_kcb_uat_consumer_secret")

    if not consumer_key or not consumer_secret:
        frappe.throw("KCB consumer key/secret is not configured on Company")

    token_url = _token_url(config)
    basic = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }

    response = requests.post(token_url, data={"grant_type": "client_credentials"}, headers=headers, timeout=30)
    if response.status_code >= 400 or not response.text:
        # Some Buni examples put grant_type in the query string and send an empty body.
        fallback_url = token_url
        if "grant_type=" not in fallback_url:
            separator = "&" if "?" in fallback_url else "?"
            fallback_url = f"{fallback_url}{separator}grant_type=client_credentials"
        response = requests.post(fallback_url, data="", headers=headers, timeout=30)

    try:
        data = response.json()
    except Exception:
        data = {"raw_response": response.text}

    if response.status_code >= 400:
        frappe.throw(f"KCB token request failed: HTTP {response.status_code} {str(data)[:300]}")

    token = data.get("access_token") or data.get("accessToken") or data.get("token")
    if not token:
        frappe.throw(f"KCB token response did not include access_token: {str(data)[:300]}")
    return token


def _stk_id(response_payload: dict, key: str) -> str:
    """KCB returns the ids nested: {"header": {...}, "response": {"CheckoutRequestID": ...}}."""
    if not isinstance(response_payload, dict):
        return ""
    inner = response_payload.get("response")
    if isinstance(inner, dict) and inner.get(key):
        return inner.get(key)
    return response_payload.get(key) or ""


def _insert_stk_log(company: str, invoice_name: str, payload: dict, response_payload: dict, status: str) -> str:
    if not frappe.db.exists("DocType", "KCB Integration Log"):
        frappe.log_error(json.dumps({"request": payload, "response": response_payload}, indent=2), "KCB STK Push")
        return ""

    doc = frappe.new_doc("KCB Integration Log")
    doc.callback_type = "STK Push"
    doc.kcb_endpoint = "kcb_stk_push"
    doc.environment = _environment(_company_config(company))
    doc.company = company
    doc.transaction_reference = response_payload.get("CheckoutRequestID") or response_payload.get("MerchantRequestID") or payload.get("messageId")
    doc.request_id = payload.get("messageId")
    doc.customer_reference = payload.get("invoiceNumber")
    doc.external_reference = payload.get("invoiceNumber")
    doc.status = str(response_payload.get("statusCode") or response_payload.get("ResponseCode") or status)
    doc.amount = flt(payload.get("amount"))
    doc.currency = "KES"
    doc.phone_number = payload.get("phoneNumber")
    doc.account_reference = payload.get("invoiceNumber")
    doc.checkout_request_id = _stk_id(response_payload, "CheckoutRequestID")
    doc.merchant_request_id = _stk_id(response_payload, "MerchantRequestID")
    doc.message_id = payload.get("messageId")
    doc.received_on = now_datetime()
    doc.processing_status = _mapped_status(status)
    doc.error_message = status
    doc.linked_sales_invoice = invoice_name
    doc.raw_payload = json.dumps(payload, indent=2, sort_keys=True, default=str)
    doc.response_json = json.dumps(response_payload, indent=2, sort_keys=True, default=str)
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return doc.name


def _log_exists(transaction_reference: str) -> bool:
    if not transaction_reference:
        return False
    if not frappe.db.exists("DocType", "KCB Integration Log"):
        return False
    return bool(
        frappe.db.exists(
            "KCB Integration Log",
            {
                "transaction_reference": transaction_reference,
                "processing_status": ["not in", list(NON_BLOCKING_STATUSES)],
            },
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
    doc.processing_status = _mapped_status(processing_status)
    doc.linked_sales_invoice = linked_sales_invoice
    doc.linked_payment_entry = linked_payment_entry
    doc.request_ip = getattr(getattr(frappe, "local", None), "request_ip", "") or ""
    doc.raw_payload = json.dumps(payload, indent=2, sort_keys=True, default=str)
    doc.headers = json.dumps(headers, indent=2, sort_keys=True, default=str)
    doc.response_json = ""
    doc.error_message = (f"[{processing_status}] " + error_message) if error_message else processing_status
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
    if invoice.company != company:
        frappe.throw(
            "KCB routing mismatch: {0} belongs to {1}, callback routed to {2}".format(
                invoice.name, invoice.company, company
            )
        )
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


def _flatten_kcb(payload: dict) -> dict:
    """Merge nested KCB structures (Till header/notificationData, STK Body.stkCallback)
    up to the top level so the flat extractors below can read invoice, amount, etc."""
    if not isinstance(payload, dict):
        return {}
    flat = dict(payload)

    header = payload.get("header")
    if isinstance(header, dict):
        for key, value in header.items():
            flat.setdefault(key, value)

    request_payload = payload.get("requestPayload")
    if isinstance(request_payload, dict):
        additional = request_payload.get("additionalData")
        notification = additional.get("notificationData") if isinstance(additional, dict) else None
        if isinstance(notification, dict):
            # notificationData wins: its businessKey is the invoice, not the biller code
            for key, value in notification.items():
                flat[key] = value

    body = payload.get("Body")
    stk = body.get("stkCallback") if isinstance(body, dict) else None
    if isinstance(stk, dict):
        for key, value in stk.items():
            if key != "CallbackMetadata":
                flat.setdefault(key, value)
        metadata = stk.get("CallbackMetadata")
        items = metadata.get("Item") if isinstance(metadata, dict) else None
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("Name") is not None:
                    flat.setdefault(item["Name"], item.get("Value"))

    return flat


def _ack_flat(transaction_id: str, message: str = "Notification received") -> dict:
    return {
        "transactionID": transaction_id or "",
        "statusCode": "0",
        "statusMessage": message,
    }


def _ack_till(message_id: str, conversation_id: str, transaction_id: str,
              status_code: str = "0", message: str = "Notification received") -> dict:
    return {
        "header": {
            "messageID": message_id or "",
            "originatorConversationID": conversation_id or "",
            "statusCode": status_code,
            "statusMessage": message,
        },
        "responsePayload": {"transactionInfo": {"transactionId": transaction_id or ""}},
    }


def _validation_response(invoice_name: str, payload: dict, config: frappe._dict) -> dict:
    transaction_id = _first(payload, "requestId", "messageId") or (invoice_name or "")
    # KCB (21-Jul-2026): creditAccountIdentifier must be the settlement ACCOUNT NUMBER (1350363375),
    # not the paybill/invoice base. Falls back to invoice_number_base if account not configured.
    credit_account = (config.get("custom_kcb_credit_account")
                      or config.get("custom_kcb_invoice_number_base") or "")
    if not invoice_name:
        return {
            "transactionID": transaction_id,
            "statusCode": "1",
            "statusMessage": "Bill not found",
            "CustomerName": "",
            "billAmount": "0",
            "currency": "KES",
            "billType": "PARTIAL",
            "creditAccountIdentifier": credit_account,
        }
    invoice = frappe.get_doc("Sales Invoice", invoice_name)
    return {
        "transactionID": transaction_id,
        "statusCode": "0",
        "statusMessage": "Success",
        "CustomerName": invoice.customer_name or invoice.customer,
        "billAmount": _as_money_string(invoice.outstanding_amount),
        "currency": invoice.currency or "KES",
        "billType": "PARTIAL",
        "creditAccountIdentifier": credit_account,
    }


def handle_callback(endpoint: str) -> dict:
    callback_type = CALLBACK_METHODS.get(endpoint, endpoint)
    payload = _flatten_kcb(_request_json())
    headers = _headers()
    company = _company_for_payload(payload)
    config = _company_config(company)
    transaction_reference = _transaction_reference(payload)
    invoice_reference = _invoice_reference(payload)
    invoice_name = _find_sales_invoice(invoice_reference)

    # An STK result callback has no invoice reference of its own. Recover the invoice
    # AND the company from the push we sent, otherwise _company_for_payload falls back
    # to the default company and a Mombasa payment lands in Kitale's books.
    if endpoint == "kcb_mpesa_callback":
        origin = _stk_origin(payload)
        if origin.get("linked_sales_invoice") and not invoice_name:
            invoice_name = origin["linked_sales_invoice"]
        if origin.get("company") and origin["company"] != company:
            company = origin["company"]
            config = _company_config(company)

    is_validation = endpoint == "kcb_validation"
    is_till = endpoint == "kcb_till_notification"
    message_id = _first(payload, "messageID", "messageId")
    conversation_id = _first(payload, "originatorConversationID")

    # Signature is verified over the raw request body inside _signature_status
    signature_ok, signature_message, signature_header = _signature_status(
        company, config, payload, headers, endpoint)
    if not signature_ok:
        _write_log(callback_type, company, payload, headers, signature_header,
                   "Rejected", signature_message, invoice_name)
        frappe.local.response["http_status_code"] = 401
        if is_validation:
            return _validation_response("", payload, config)
        if is_till:
            return _ack_till(message_id, conversation_id, "", "1", signature_message)
        return {"transactionID": "", "statusCode": "1", "statusMessage": signature_message}

    # Bill-Validation: synchronous lookup, no payment entry, return the bill details KCB needs
    if is_validation:
        status = "Verified" if invoice_name else "Pending Manual Review"
        _write_log(callback_type, company, payload, headers, signature_header, status,
                   "" if invoice_name else "Invoice not found for validation", invoice_name)
        return _validation_response(invoice_name, payload, config)

    # Duplicate notification -> acknowledge without re-processing
    if transaction_reference and _log_exists(transaction_reference):
        if is_till:
            return _ack_till(message_id, conversation_id, transaction_reference, "0", "Duplicate ignored")
        return _ack_flat(transaction_reference, "Duplicate ignored")

    # One STK push yields exactly one final result. Block replay of a CheckoutRequestID
    # so a captured id cannot be re-posted with a forged ResultCode 0.
    if endpoint == "kcb_mpesa_callback":
        replay_id = _first(payload, "CheckoutRequestID", "checkoutRequestID", "checkout_request_id")
        if replay_id and frappe.db.exists(
            "KCB Integration Log", {"callback_type": "M-Pesa", "checkout_request_id": replay_id}
        ):
            return _ack_flat(transaction_reference, "Duplicate ignored")

    # STK Push result callback: honour ResultCode (0/00 = paid, anything else = cancelled/failed)
    result_code = _first(payload, "ResultCode", "resultCode")
    if endpoint == "kcb_mpesa_callback" and result_code and result_code not in ("0", "00"):
        _write_log(callback_type, company, payload, headers, signature_header, "STK Push Failed",
                   f"ResultCode {result_code}: " + _first(payload, "ResultDesc", "resultDesc"), invoice_name)
        return _ack_flat(transaction_reference, "Callback received")

    # Notification (Account / Till / M-Pesa success / FT): create the Payment Entry
    payment_entry = ""
    processing_status = "Verified"
    error_message = ""
    posting_user = _posting_user(company, config, endpoint, payload)
    original_user = frappe.session.user
    try:
        # Elevated for this insert only - see _posting_user for why Guest cannot do it.
        # The finally puts the session back before anything else in the request runs,
        # including the logging below, so nothing outside this call is elevated.
        frappe.set_user(posting_user)
        try:
            payment_entry = _create_payment_entry(company, config, payload, invoice_name)
        finally:
            frappe.set_user(original_user)
        if payment_entry:
            processing_status = "Payment Entry Created"
        elif config.get("custom_kcb_auto_create_payments"):
            processing_status = "Pending Manual Review"
            error_message = "No matching Sales Invoice or missing configuration"
        else:
            processing_status = "Verified - Log Only"
    except Exception as exc:
        processing_status = "Payment Entry Failed"
        # str(exc) is empty for validations raised through frappe.throw/msgprint - the
        # real text lives in frappe.local.message_log. Without this the log said only
        # "Payment Entry Failed" and the cause was unrecoverable after the fact.
        parts = [str(exc) or exc.__class__.__name__]
        try:
            for entry in (getattr(frappe.local, "message_log", None) or []):
                text = entry.get("message") if isinstance(entry, dict) else str(entry)
                if text:
                    parts.append(str(text))
        except Exception:
            pass
        error_message = " | ".join(parts)[:1000]
        try:
            frappe.log_error(frappe.get_traceback(), "KCB Payment Entry Failed")
        except Exception:
            pass

    _write_log(callback_type, company, payload, headers, signature_header,
               processing_status, error_message, invoice_name, payment_entry)

    transaction_id = payment_entry or transaction_reference or invoice_name or ""
    if is_till:
        return _ack_till(message_id, conversation_id, transaction_id, "0", processing_status)
    return _ack_flat(transaction_id, processing_status)


@frappe.whitelist()
def kcb_stk_push(sales_invoice: str, phone_number: str = "", amount: str = ""):
    import requests

    invoice = frappe.get_doc("Sales Invoice", sales_invoice)
    invoice.check_permission("read")

    if invoice.docstatus != 1:
        frappe.throw("Submit the Sales Invoice before sending KCB M-Pesa request")

    company = invoice.company
    config = _company_config(company)
    if not config:
        frappe.throw("KCB Company configuration is missing")

    phone = _normalize_phone(phone_number or getattr(invoice, "contact_mobile", "") or getattr(invoice, "customer_mobile_no", ""))
    if not phone or not phone.startswith("254") or len(phone) < 12:
        frappe.throw("Enter customer M-Pesa phone number in 2547XXXXXXXX format")

    request_amount = flt(amount) if amount not in (None, "") else flt(invoice.outstanding_amount or invoice.rounded_total or invoice.grand_total)
    if request_amount <= 0:
        frappe.throw("Amount must be greater than zero")

    base = config.get("custom_kcb_invoice_number_base") or ""
    if not base:
        frappe.throw("KCB Invoice Number Base is missing on Company")

    callback_url = config.get("custom_kcb_mpesa_callback_url") or "https://kitale.c.frappe.cloud/api/method/kcb_mpesa_callback"
    invoice_number = f"{base}#{invoice.name}"
    message_id = f"PRAMO-{invoice.name}-{frappe.utils.now_datetime().strftime('%Y%m%d%H%M%S')}".replace("/", "-")

    payload = {
        "phoneNumber": phone,
        "amount": _as_money_string(request_amount),
        "invoiceNumber": invoice_number,
        "sharedShortCode": bool(
            1
            if config.get("custom_kcb_shared_shortcode") in (None, "")
            else config.get("custom_kcb_shared_shortcode")
        ),
        "orgShortCode": config.get("custom_kcb_org_shortcode") or "",
        "orgPassKey": config.get("custom_kcb_org_passkey") or "",
        "callbackUrl": callback_url,
        "transactionDescription": f"Invoice Payment {invoice.name}",
    }
    headers = {
        "Authorization": f"Bearer {_get_access_token(company, config)}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "routeCode": "207",
        "operation": "STKPush",
        "messageId": message_id,
    }
    payload["messageId"] = message_id

    response = requests.post(_stk_url(config), headers=headers, json=payload, timeout=45)
    try:
        response_payload = response.json()
    except Exception:
        response_payload = {"raw_response": response.text}

    status_code = str(response_payload.get("statusCode") or response_payload.get("ResponseCode") or "")
    ok = response.status_code < 400 and status_code in ("", "0", "200")
    processing_status = "STK Push Sent" if ok else "STK Push Failed"
    log_name = _insert_stk_log(company, invoice.name, payload, response_payload, processing_status)

    if not ok:
        frappe.throw(f"KCB STK Push failed: HTTP {response.status_code} {str(response_payload)[:300]}")

    return {
        "status": "SUCCESS",
        "message": response_payload.get("CustomerMessage")
        or response_payload.get("statusDescription")
        or "STK Push sent to customer phone",
        "sales_invoice": invoice.name,
        "invoice_number": invoice_number,
        "message_id": message_id,
        "log": log_name,
        "response": response_payload,
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


@frappe.whitelist()
def kcb_payment_status(checkout_request_id: str) -> dict:
    """Read-only poll of the async M-Pesa outcome for one STK push.

    STRICTLY read-only and additive: it only READS the KCB Integration Log row
    written by the callback and returns a status dict. It creates, modifies,
    submits and allocates nothing - every payment posting stays in the untouched
    callback path. The Sales Invoice Client Script calls this every few seconds
    after kcb_stk_push so the rep sees whether the customer paid without
    refreshing the desk. Logged-in staff only (no allow_guest). Never raises on
    'not found' - it returns the pending state.
    """
    pending = {"state": "pending", "detail": "Waiting for customer to approve",
               "amount": "", "payment_entry": "", "mpesa_receipt": "", "error": ""}
    if not checkout_request_id:
        return pending
    try:
        if not frappe.db.exists("DocType", "KCB Integration Log"):
            return pending
        rows = frappe.get_all(
            "KCB Integration Log",
            filters={
                "checkout_request_id": checkout_request_id,
                "callback_type": ["in", ["M-Pesa", "Funds Transfer"]],
            },
            fields=[
                "processing_status", "error_message", "linked_payment_entry",
                "transaction_reference", "retrieval_ref_number", "amount",
            ],
            order_by="creation desc",
            limit_page_length=1,
        )
    except Exception:
        return pending

    if not rows:
        return pending

    row = rows[0]
    status = (row.get("processing_status") or "").strip()
    detail = (row.get("error_message") or status or "").strip()
    payment_entry = row.get("linked_payment_entry") or ""
    receipt = row.get("transaction_reference") or row.get("retrieval_ref_number") or ""
    amount = row.get("amount")
    amount = "" if amount in (None, "") else str(amount)
    detail_lower = detail.lower()

    # SUCCESS: the callback landed as Processed. For an STK push that means the
    # customer approved and the money is confirmed (ResultCode 0). This path never
    # offers a resend - that is the double-charge guard.
    if status == "Processed":
        return {
            "state": "success",
            "detail": detail or "Payment received",
            "amount": amount,
            "payment_entry": payment_entry,
            "mpesa_receipt": receipt,
            "error": "",
        }

    # FAILED: the callback landed but the money did not post.
    if status == "Error":
        error = detail or "Payment failed"
        allow_resend = True
        # "Payment Entry Failed" is the one Error where the customer's money may
        # have arrived but posting threw - a blind resend there double-charges, so
        # the UI must NOT offer a one-tap resend for it (allow_resend=False).
        if "payment entry failed" in detail_lower:
            error = ("Customer may have paid but the Payment Entry did not post - "
                     "check the KCB Integration Log before resending. " + detail)
            allow_resend = False
        return {
            "state": "failed",
            "detail": detail,
            "amount": amount,
            "payment_entry": payment_entry,
            "mpesa_receipt": receipt,
            "error": error,
            "allow_resend": allow_resend,
        }

    # Non-terminal / config-hold (Pending Config, blank) -> keep polling.
    return {
        "state": "pending",
        "detail": detail or "Waiting for customer to approve",
        "amount": amount,
        "payment_entry": payment_entry,
        "mpesa_receipt": receipt,
        "error": "",
    }
