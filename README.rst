Supplier Payment Assistant
==========================

A client-only buyer workflow for the `Supplier Payment Decision Agent
<https://supplier-payment-verifier-production.up.railway.app/>`_.
It registers an ephemeral RSA public key, signs a caller-supplied invoice/PO/GRN
request and obtains an advisory supplier-payment decision.

**It never purchases goods or pays supplier invoices.** Optional x402 fees buy
verification only. There is no backend, database, customer invoice or credential
in this repository.

* Completely offline, clearly labelled synthetic demonstration.
* Inspection first: quotes without a wallet or uploading invoice data.
* Explicit payment authorization with a combined **0.055 USDC maximum**.
* Installable CLI, JSON reports, public request models and Python interface.
* No LLM key or subscription required.

Install and try offline
----------------------

Requires Python 3.12 or later:

.. code-block:: powershell

   python -m pip install "git+https://github.com/LKennedyShare/supplier-payment-assistant.git"
   supplier-payment-assistant --demo

``python -m supplier_payment_assistant`` accepts the same arguments.

The offline mode is ``SYNTHETIC_OFFLINE_ILLUSTRATION``. It generates an ephemeral
RSA signature over fictional input and shows an explicitly unverified
``FLAG_FOR_HUMAN_REVIEW`` example. It performs no live checks, key registration,
wallet access or network requests. No synthetic supplier identity should be
treated as real.

Inspect the real workflow without paying
---------------------------------------

Prepare your own unsigned JSON request in a local ``invoice.json`` file.
Required top-level fields:

* ``transaction_id``: stable for the same business request.
* ``supplier``: CRO number, VAT number and IBAN.
* ``invoice``: invoice/PO/GRN references, amount, currency, payee name and lines.
* ``purchase_order``: PO reference, supplier, amount, currency and lines.
* ``goods_receipt``: GRN/PO references, supplier and received lines.

Do not include ``signature``: the client creates it. The ``PaymentRequest`` model
in ``supplier_payment_assistant.contracts`` defines exact fields, constraints
and canonical signing bytes. The live service also publishes
``/schemas/decision.schema.json``. The model checks structure, not registry
status or account ownership.

.. code-block:: powershell

   supplier-payment-assistant https://supplier-payment-verifier-production.up.railway.app --request .\invoice.json

The default is inspection only. It sends **empty quote probes** for both
operations, not your invoice, public key or signature. No wallet is needed and
no on-chain payment is signed.

Explicitly authorize the two service fees
----------------------------------------

The workflow expects these prices:

1. RSA public-key registration: 0.005 USDC.
2. Five-gate invoice verification: 0.05 USDC.

Set ``X402_CLIENT_PRIVATE_KEY`` in your local shell to a throwaway wallet funded
with only a small USDC balance on the quoted network. Then explicitly run:

.. code-block:: powershell

   supplier-payment-assistant https://supplier-payment-verifier-production.up.railway.app --request .\invoice.json --pay --max-usdc 0.055

Base mainnet uses real USDC. Both quotes are checked before the first payment,
and each fresh quote and remaining budget is checked before signing. Only
x402 v2 exact USDC on Base/Base Sepolia is allowed. Unexpected prices,
redirects, wrong assets, mixed networks and mismatched resources stop the flow.

At most one paid retry per operation is authorized. There are **no automatic
payment retries**. The 0.055 USDC cap is per invocation, not a daily wallet
limit. The invoice amount is unrelated to this fee budget.

Registration and verification use the same payer wallet/tenant. The RSA key is
ephemeral and not written to disk: a new run creates a new key ID and may pay
registration again. Repeating an unchanged transaction ID may return the stored
business decision, but do not assume service fees are free on replay.

Missing or failed settlement metadata stops the flow. A partial-failure report
separates authorized fees, confirmed settlement responses and key registration.
Settlement confirmation trusts the service's receipt; the client does not
independently query a blockchain or verify decision attestations.

Never put a private key in source, request JSON, prompts or logs. Reports may
contain private supplier and invoice details: do not publish them. The
``invoice*.json``, ``request*.json``, key, environment and database patterns are
ignored by Git as an additional safeguard, not a substitute for reviewing files.

Use from another agent
----------------------

.. code-block:: python

   import json
   from pathlib import Path
   from supplier_payment_assistant import run_live

   unsigned = json.loads(Path("invoice.json").read_text(encoding="utf-8"))
   report = run_live(
       "https://supplier-payment-verifier-production.up.railway.app",
       unsigned,
   )  # Inspection only: no payment or invoice upload.

Payment additionally requires the boolean ``pay=True`` and an exact cap such
as ``max_usdc="0.055"`` (or a ``Decimal``). Truthy strings such as ``"false"``
are rejected. ``BuyerError.report`` preserves partial workflow status; the CLI
writes errors to stderr and exits nonzero.

The reusable ``sign_request`` function normalizes decimal fields with
``PaymentRequest`` before signing compact, sorted-key JSON without the
``signature`` field. Signatures use RSA-PSS, SHA-256, MGF1(SHA-256) and
digest-length salt. Hand-signing the original JSON without normalization can
produce different bytes.

Requests identify as ``supplier-payment-assistant``. The label is not a
verified customer identity; the payer wallet establishes the service tenant.

Assurance boundaries
--------------------

``EXECUTE_PAYMENT``, ``REJECT_PAYMENT`` and ``FLAG_FOR_HUMAN_REVIEW`` are
**advisory result codes**, not bank instructions this client executes.

* IBAN checksum/name checks are not bank Confirmation of Payee or ownership.
* The three-way match compares supplied documents, not authenticated ERP data.
* RSA signatures establish who signed supplied data, not whether it is true.
* Confidence 1.0 is a rule result, not a guarantee of payment safety.
* Keep customer-controlled approval, account-ownership and bank-payment checks.

Development
-----------

.. code-block:: powershell

   git clone https://github.com/LKennedyShare/supplier-payment-assistant.git
   cd supplier-payment-assistant
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -e ".[test]"
   python -m pytest -q

Tests use mocked HTTP/facilitator responses and predictable unfunded test wallet
material only. The CI workflow runs tests and the offline demonstration; it
never authorizes a real payment.

License
-------

MIT licensed; see ``LICENSE``. This covers this buyer client's source code,
not the separate verification service, registry data or API usage terms.
