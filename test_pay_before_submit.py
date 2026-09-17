"""Offline check of the pay-before-submit flow. No Frappe bench, no site, no KRA:
frappe/erpnext are stubbed, so this proves the decision logic only.
Run: python test_pay_before_submit.py
"""
import sys
import types


# ---------- frappe stub ----------------------------------------------------
class _Throw(Exception):
    pass


class FakeInvoice:
    def __init__(self, docstatus=0, rounded_total=1160.0, grand_total=1160.0,
                 prevent_etims_submission=0, name="SINV-TEST-0001"):
        self.docstatus = docstatus
        self.rounded_total = rounded_total
        self.grand_total = grand_total
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
    frappe.get_attr = lambda path: calls["etims"].append  # send() records the name
    return frappe, utils, password, calls


def load_api(frappe, utils, password):
    crypto = types.ModuleType("pramo_kcb_integration.crypto")
    crypto.verify_rsa_signature = lambda *a: (True, "ok")
    pkg = types.ModuleType("pramo_kcb_integration")
    sys.modules.update({
        "frappe": frappe, "frappe.utils": utils, "frappe.utils.password": password,
        "pramo_kcb_integration": pkg, "pramo_kcb_integration.crypto": crypto,
    })
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "kcb_api", __file__.replace("test_pay_before_submit.py",
                                    "pramo_kcb_integration/api.py"))
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api


def flag_config(api, on=True):
    return api.frappe._dict({"custom_kcb_stk_from_draft": 1 if on else 0,
                             "custom_kcb_auto_create_payments": 1})


PAY = {"Amount": "1160", "CheckoutRequestID": "ws_CO_1"}


def main():
    # 1. exact amount -> submit
    inv = FakeInvoice(docstatus=0)
    frappe, utils, password, calls = make_frappe(inv)
    api = load_api(frappe, utils, password)
    ok, hold = api._maybe_submit_draft_invoice(flag_config(api), PAY, inv.name, "kcb_mpesa_callback")
    assert ok and not hold and inv.submitted, (ok, hold, inv.submitted)

    # 2. wrong amount -> held, NOT submitted
    inv = FakeInvoice(docstatus=0)
    frappe, utils, password, calls = make_frappe(inv)
    api = load_api(frappe, utils, password)
    ok, hold = api._maybe_submit_draft_invoice(flag_config(api), {"Amount": "500"}, inv.name, "kcb_mpesa_callback")
    assert not ok and "mismatch" in hold.lower() and not inv.submitted, (ok, hold)

    # 3. flag off -> held draft
    inv = FakeInvoice(docstatus=0)
    frappe, utils, password, calls = make_frappe(inv)
    api = load_api(frappe, utils, password)
    ok, hold = api._maybe_submit_draft_invoice(flag_config(api, on=False), PAY, inv.name, "kcb_mpesa_callback")
    assert not ok and hold and not inv.submitted, (ok, hold)

    # 4. second callback after submit (double-fire) -> no-op, no double submit
    inv = FakeInvoice(docstatus=1)
    frappe, utils, password, calls = make_frappe(inv)
    api = load_api(frappe, utils, password)
    ok, hold = api._maybe_submit_draft_invoice(flag_config(api), PAY, inv.name, "kcb_mpesa_callback")
    assert not ok and not hold and not inv.submitted, (ok, hold)

    # 5. non-STK endpoint never submits
    inv = FakeInvoice(docstatus=0)
    frappe, utils, password, calls = make_frappe(inv)
    api = load_api(frappe, utils, password)
    ok, hold = api._maybe_submit_draft_invoice(flag_config(api), PAY, inv.name, "kcb_till_notification")
    assert not ok and not hold and not inv.submitted

    # 6. eTIMS: sent normally, skipped on prevent flag, swallowed on failure
    inv = FakeInvoice(docstatus=1)
    frappe, utils, password, calls = make_frappe(inv)
    api = load_api(frappe, utils, password)
    assert api._send_to_etims(inv.name) == "" and calls["etims"] == [inv.name]
    inv.prevent_etims_submission = 1
    note = api._send_to_etims(inv.name)
    assert "skipped" in note and calls["etims"] == [inv.name]
    inv.prevent_etims_submission = 0
    frappe.get_attr = lambda path: (_ for _ in ()).throw(RuntimeError("slade down"))
    note = api._send_to_etims(inv.name)
    assert note.startswith("eTIMS send failed"), note

    # 7. push gate: draft + no flag -> old throw; draft + flag -> passes the gate
    sys.modules["requests"] = types.ModuleType("requests")  # imported inside kcb_stk_push
    inv = FakeInvoice(docstatus=0)
    inv.check_permission = lambda p: None
    frappe, utils, password, calls = make_frappe(inv)
    api = load_api(frappe, utils, password)
    api._company_config = lambda c: flag_config(api, on=False)
    try:
        api.kcb_stk_push(inv.name, "254700000000")
        raise AssertionError("draft push allowed without flag")
    except _Throw as e:
        assert "Submit the Sales Invoice" in str(e)
    api._company_config = lambda c: flag_config(api, on=True)
    inv.check_permission = lambda p: None
    try:
        api.kcb_stk_push(inv.name, "bad-phone")  # passes docstatus gate, dies on phone
        raise AssertionError("unreachable")
    except _Throw as e:
        assert "phone" in str(e).lower(), str(e)

    print("ALL PAY-BEFORE-SUBMIT CHECKS PASSED")


if __name__ == "__main__":
    main()
