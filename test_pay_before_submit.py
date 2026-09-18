"""Offline check of the pay-before-submit + partial-payments flow. No Frappe
bench, no site, no KRA: frappe/erpnext are stubbed, so this proves the decision
logic only.
Run: python test_pay_before_submit.py
"""
import sys
import types


class _Throw(Exception):
    pass


class FakeInvoice:
    def __init__(self, docstatus=0, rounded_total=1160.0, grand_total=1160.0,
                 outstanding_amount=0.0, prevent_etims_submission=0,
                 name="SINV-TEST-0001"):
        self.docstatus = docstatus
        self.rounded_total = rounded_total
        self.grand_total = grand_total
        self.outstanding_amount = outstanding_amount
        self.prevent_etims_submission = prevent_etims_submission
        self.name = name
        self.company = "PRAMO TRADERS LIMITED"
        self.submitted = False

    def submit(self):
        self.submitted = True
        self.docstatus = 1


def make_frappe(invoice):
    frappe = types.ModuleType("frappe")

    class _dict(dict):
        def __getattr__(self, k):
            return self.get(k)

    frappe._dict = _dict

    db = types.SimpleNamespace()
    db.get_value = lambda dt, name, field=None, **kw: getattr(invoice, field) if field else None
    db.exists = lambda *a, **k: True
    db.sql = lambda *a, **k: []
    db.commit = lambda: None
    db.rollback = lambda: None
    frappe.db = db

    frappe.get_doc = lambda dt, name=None: invoice
    frappe.throw = lambda msg, *a, **k: (_ for _ in ()).throw(_Throw(msg))
    frappe.log_error = lambda *a, **k: None
    frappe.get_traceback = lambda: "trace"
    frappe.whitelist = lambda *a, **k: (lambda f: f)
    frappe.session = types.SimpleNamespace(user="rep@pramo.com")
    frappe.set_user = lambda u: None
    frappe.local = types.SimpleNamespace(request=None, message_log=[])
    frappe.form_dict = {}
    frappe.defaults = types.SimpleNamespace(get_user_default=lambda k: "")
    frappe.get_all = lambda *a, **k: []

    utils = types.ModuleType("frappe.utils")
    utils.flt = lambda v, p=None: float(v or 0)
    utils.now_datetime = lambda: None
    utils.today = lambda: "2026-09-18"
    frappe.utils = utils

    password = types.ModuleType("frappe.utils.password")
    password.get_decrypted_password = lambda *a, **k: ""

    calls = {"etims": []}
    frappe.get_attr = lambda path: calls["etims"].append
    return frappe, utils, password, calls


def load_api(frappe, utils, password):
    crypto = types.ModuleType("pramo_kcb_integration.crypto")
    crypto.verify_rsa_signature = lambda *a: (True, "ok")
    pkg = types.ModuleType("pramo_kcb_integration")
    sys.modules.update({
        "frappe": frappe, "frappe.utils": utils, "frappe.utils.password": password,
        "pramo_kcb_integration": pkg, "pramo_kcb_integration.crypto": crypto,
        "requests": types.ModuleType("requests"),
    })
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "kcb_api", __file__.replace("test_pay_before_submit.py",
                                    "pramo_kcb_integration/api.py"))
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api


def flag_config(api, on=True, partial=False):
    return api.frappe._dict({"custom_kcb_stk_from_draft": 1 if on else 0,
                             "custom_kcb_allow_partial": 1 if partial else 0,
                             "custom_kcb_auto_create_payments": 1})


def fresh(docstatus=0, **kw):
    inv = FakeInvoice(docstatus=docstatus, **kw)
    frappe, utils, password, calls = make_frappe(inv)
    api = load_api(frappe, utils, password)
    return inv, api, calls


PAY_EXACT = {"Amount": "1160", "CheckoutRequestID": "ws_CO_1"}
PAY_PART = {"Amount": "800", "CheckoutRequestID": "ws_CO_2"}
PAY_OVER = {"Amount": "1500", "CheckoutRequestID": "ws_CO_3"}
CB = "kcb_mpesa_callback"


def main():
    # 1. exact amount, partial flag off -> submit, no note (original flow unchanged)
    inv, api, _ = fresh()
    ok, hold, note = api._maybe_submit_draft_invoice(flag_config(api), PAY_EXACT, inv.name, CB)
    assert ok and not hold and not note and inv.submitted

    # 2. partial, partial flag OFF -> held draft (original guard unchanged)
    inv, api, _ = fresh()
    ok, hold, note = api._maybe_submit_draft_invoice(flag_config(api), PAY_PART, inv.name, CB)
    assert not ok and "mismatch" in hold.lower() and not inv.submitted

    # 3. partial, partial flag ON -> SUBMITS (Partly Paid comes from ERPNext outstanding)
    inv, api, _ = fresh()
    ok, hold, note = api._maybe_submit_draft_invoice(flag_config(api, partial=True), PAY_PART, inv.name, CB)
    assert ok and not hold and not note and inv.submitted

    # 4. exact, partial flag ON -> submits, no note
    inv, api, _ = fresh()
    ok, hold, note = api._maybe_submit_draft_invoice(flag_config(api, partial=True), PAY_EXACT, inv.name, CB)
    assert ok and not hold and not note and inv.submitted

    # 5. overpay, partial flag ON -> submits + overpayment note (D2: book, flag, don't hold)
    inv, api, _ = fresh()
    ok, hold, note = api._maybe_submit_draft_invoice(flag_config(api, partial=True), PAY_OVER, inv.name, CB)
    assert ok and not hold and "Overpayment" in note and "340.0" in note and inv.submitted, note

    # 6. overpay, partial flag OFF -> held (unchanged)
    inv, api, _ = fresh()
    ok, hold, note = api._maybe_submit_draft_invoice(flag_config(api), PAY_OVER, inv.name, CB)
    assert not ok and "mismatch" in hold.lower() and not inv.submitted

    # 7. zero amount -> held either way
    inv, api, _ = fresh()
    ok, hold, note = api._maybe_submit_draft_invoice(flag_config(api, partial=True),
                                                     {"Amount": "0", "CheckoutRequestID": "x"}, inv.name, CB)
    assert not ok and "zero" in hold.lower() and not inv.submitted

    # 8. pay-before-submit flag off -> held draft
    inv, api, _ = fresh()
    ok, hold, note = api._maybe_submit_draft_invoice(flag_config(api, on=False, partial=True), PAY_EXACT, inv.name, CB)
    assert not ok and hold and not inv.submitted

    # 9. second callback after submit (double-fire) -> no-op
    inv, api, _ = fresh(docstatus=1)
    ok, hold, note = api._maybe_submit_draft_invoice(flag_config(api, partial=True), PAY_PART, inv.name, CB)
    assert not ok and not hold and not inv.submitted

    # 10. non-STK endpoint never submits
    inv, api, _ = fresh()
    ok, hold, note = api._maybe_submit_draft_invoice(flag_config(api, partial=True), PAY_EXACT, inv.name, "kcb_till_notification")
    assert not ok and not hold and not inv.submitted

    # 11. eTIMS: sent normally, skipped on prevent flag, swallowed on failure
    inv, api, calls = fresh(docstatus=1)
    assert api._send_to_etims(inv.name) == "" and calls["etims"] == [inv.name]
    inv.prevent_etims_submission = 1
    assert "skipped" in api._send_to_etims(inv.name) and calls["etims"] == [inv.name]
    inv.prevent_etims_submission = 0
    api.frappe.get_attr = lambda path: (_ for _ in ()).throw(RuntimeError("down"))
    assert api._send_to_etims(inv.name).startswith("eTIMS send failed")

    # 12. push gate: draft without pay-before-submit flag -> old throw
    inv, api, _ = fresh()
    inv.check_permission = lambda p: None
    api._company_config = lambda c: flag_config(api, on=False)
    try:
        api.kcb_stk_push(inv.name, "254700000000")
        raise AssertionError("draft push allowed without flag")
    except _Throw as e:
        assert "Submit the Sales Invoice" in str(e)

    # 13. push gate D1: amount above due -> reject with clear message (draft, flag on)
    inv, api, _ = fresh()
    inv.check_permission = lambda p: None
    api._company_config = lambda c: flag_config(api, partial=True)
    try:
        api.kcb_stk_push(inv.name, "254700000000", amount="2000")
        raise AssertionError("overpay push allowed")
    except _Throw as e:
        assert "exceeds the amount due" in str(e), str(e)

    # 14. push: partial amount below due passes the amount gates (dies later on token config)
    inv, api, _ = fresh()
    inv.check_permission = lambda p: None
    api._company_config = lambda c: flag_config(api, partial=True)
    try:
        api.kcb_stk_push(inv.name, "254700000000", amount="800")
        raise AssertionError("unreachable")
    except _Throw as e:
        assert "exceeds" not in str(e) and "Amount must be" not in str(e), str(e)

    # 15. push on SUBMITTED invoice: due = outstanding (overpay vs outstanding rejected)
    inv, api, _ = fresh(docstatus=1, outstanding_amount=360.0)
    inv.check_permission = lambda p: None
    api._company_config = lambda c: flag_config(api, partial=True)
    try:
        api.kcb_stk_push(inv.name, "254700000000", amount="500")
        raise AssertionError("overpay vs outstanding allowed")
    except _Throw as e:
        assert "exceeds the amount due" in str(e), str(e)

    print("ALL PARTIAL-PAYMENT CHECKS PASSED (15)")


if __name__ == "__main__":
    main()
