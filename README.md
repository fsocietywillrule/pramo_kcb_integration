# Pramo KCB Integration

Custom Frappe app for PRAMO / KCB Buni integration.

## Purpose

- Receive KCB M-Pesa and Funds Transfer callbacks inside ERPNext.
- Verify KCB RSA signatures using the KCB public key stored on `Company`.
- Prevent duplicate transaction processing.
- Log every callback in `KCB Integration Log` when that DocType exists.
- Optionally create ERPNext Payment Entries only after `custom_kcb_auto_create_payments` is enabled.

## Production safety

By default this app is conservative:

- callback requests are logged;
- invalid or missing signatures are rejected in Production mode;
- Payment Entries are not created unless enabled on Company;
- submitted money movement is not performed by install/migration.

## Frappe Cloud deployment

1. Push this folder as a GitHub repository.
2. In Frappe Cloud, open the private bench.
3. Apps -> Add App -> Add from GitHub.
4. Enter the GitHub URL and branch.
5. Deploy the bench.
6. Install the app on the site.
7. Run a sandbox callback test before enabling auto payment creation.

