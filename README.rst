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
* Real optional LangChain and OpenAI Agents tools with operator-owned approval.
* No LLM key or subscription required.

Install and try offline
-----------------------

Requires Python 3.12 or later:

.. code-block:: powershell

   python -m pip install supplier-payment-assistant
   supplier-payment-assistant --demo

``python -m supplier_payment_assistant`` accepts the same arguments.

The offline mode is ``SYNTHETIC_OFFLINE_ILLUSTRATION``. It generates an ephemeral
RSA signature over fictional input and shows an explicitly unverified
``FLAG_FOR_HUMAN_REVIEW`` example. It performs no live checks, key registration,
wallet access or network requests. No synthetic supplier identity should be
treated as real.

Inspect the real workflow without paying
----------------------------------------

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
-----------------------------------------

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

Agent-framework integrations (source version 0.2.0)
---------------------------------------------------

PyPI currently serves **0.1.0**; the integrations below are in the **0.2.0**
source tree until that version is released. Install from a reviewed checkout:

.. code-block:: powershell

   python -m pip install -e ".[langchain,openai]"
   python -m examples.framework_demo --framework langchain
   python -m examples.framework_demo --framework openai

These examples construct real framework objects and invoke only the synthetic
demo locally: no Agent runner, model/provider/API key, wallet or network.
Both extras are optional; the base package imports neither framework.

.. code-block:: python

   from supplier_payment_assistant import SupplierPaymentTools

   tools = SupplierPaymentTools()  # Keep ONE instance across calls.
   langchain_tools = tools.langchain_tools()  # Real StructuredTool objects.
   openai_tools = tools.openai_tools()        # Real Agents FunctionTool objects.

The default tools are ``offline_demo`` and ``inspect_supplier`` only.
``inspect_supplier(unsigned_request_json: str)`` accepts an unsigned JSON string
conforming to ``PaymentRequest`` and delegates to ``run_live(pay=False)``:
only empty quote probes are sent, never invoice data. It reads no wallet key.
Each tool returns a JSON-compatible dictionary. Async framework invocations
offload synchronous work using ``asyncio.to_thread``.

For example, an application can construct an OpenAI agent as follows (this
snippet does **not** run it or call a model):

.. code-block:: python

   from agents import Agent
   agent = Agent(
       name="Advisory supplier verification",
       instructions="Inspect quotes or show the synthetic demo; never pay invoices.",
       tools=tools.openai_tools(),
   )

For LangChain, an application can construct a tool-bound chat model without
invoking it (``langchain-openai`` is a separate application dependency):

.. code-block:: python

   from langchain_openai import ChatOpenAI
   advisory_model = ChatOpenAI(model="gpt-4.1-mini").bind_tools(tools.langchain_tools())
   # No invoke/stream/agent loop here.

**Privacy:** giving invoice JSON to an LLM may send private invoice/supplier
data to your chosen model/provider. Framework tracing or debug logging may
record tool inputs and results. This package enables no tracing, adds no
capturing, and makes no automatic model calls; operators must approve data use,
configure their own framework/provider privacy controls, and avoid logging raw
invoices or keys. Returning a paid verification report can also disclose data.

Paid verification is opt-in operator code, never a model argument:

.. code-block:: python

   from examples.trusted_approval import tools_for_trusted_digests

   # Supply exact SHA-256 digests from authenticated ERP/operator records,
   # never from an LLM, prompt, or untrusted incoming invoice.
   approved_digests = frozenset()  # Empty is deny-all until trusted records exist.
   tools = tools_for_trusted_digests(approved_digests)
   paid_tools = tools.openai_tools()

Only supplying an ``approval_policy`` callable exposes ``verify_supplier_paid``.
The callback receives an immutable ``ApprovalRequest`` containing a recursively
frozen, normalized ``unsigned_request``, ``canonical_body`` bytes, their
``canonical_sha256`` digest and ``workflow_cap_usdc``. Canonical bytes are exactly
``PaymentRequest.signed_body()`` with a validation-only signature placeholder;
validation happens before approval without creating a key or sending data.
Generate trusted digests independently from the same contract/canonical bytes.
The policy must return literal ``True``; truthy strings are denied. Callback
errors deny approval. Its immutable copy cannot mutate the actual normalized
request passed to the existing signer/workflow. No supplied signature or
unexpected request/tool fields are accepted. The model cannot supply approval,
payment flags, budget, URL, or wallet parameters. Base URL is trusted constructor
configuration, defaulting to the production origin.

``workflow_cap_usdc`` defaults to ``Decimal("0.055")`` and ``max_paid_calls`` to
one. Before any paid workflow, a lock atomically reserves the **full workflow
cap**, with a cumulative ceiling of **0.055 USDC per tools object**. Approval is
called outside that lock. Denial reserves nothing and sends no requests.
``tools.reserved_usdc`` and ``tools.paid_calls`` count reserved amounts/attempts,
**not confirmed settlement**. Failures, rejected payments, partial completion
and unconfirmed settlement never refund the reservation: authorizations might
have escaped. There are no automatic paid retries. ``BuyerError.report`` retains
partial workflow status; errors are propagated rather than converted to success.
The existing client performs all signing and fee settlement.

**These limits are in-memory, per object, not persistent or wallet-wide daily
limits. Recreating objects or restarting processes resets them. Do not create a
new tools object per call. Production requires durable external approval and
budget enforcement across processes and wallets.** This is advisory verification
only, not a complete autonomous invoice-payment runner.

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
   python -m pip install -e ".[test,langchain,openai]"
   python -m pytest -q

Tests invoke real optional framework objects locally, with no model calls.
Tests use mocked HTTP/facilitator responses and predictable unfunded test wallet
material only. The CI workflow runs tests and the offline demonstration; it
never authorizes a real payment.

Release and unpaid discovery automation
---------------------------------------

Version 0.2.0 adds optional framework adapters. Trusted publishing is configured for this
repository without stored API tokens. The publisher identity is:

* PyPI project: ``supplier-payment-assistant``
* GitHub owner: ``LKennedyShare``
* Repository: ``supplier-payment-assistant``
* Workflow filename: ``publish.yml``
* Environment: ``pypi`` (exact, fixed GitHub environment name)

The GitHub environment is configured. No API token or password
is used. No manual approval per release is required by default; environment
protection rules can impose approval if the owner chooses.
Publishing a GitHub release with a matching version tag builds that
version's wheel and sdist, runs mocked tests, the installed offline CLI and
strict Twine checks, then publishes via OIDC. Only ``release.published`` triggers
publication, not a PR or manual workflow run. Tags must exactly match
``v<pyproject.version>`` and package identity is checked before build.
PyPI versions are immutable: future releases need a new version and matching
tag. Duplicate uploads fail rather than silently skipping existing files.
The initial published version is ``0.1.0``; do not reuse that release tag.

``discovery.yml`` runs nightly at 03:41 UTC and supports manual dispatch.
It uses Python's standard library only, no SDK, wallet, secrets or funds.
An independent job installs the published PyPI package and runs only its offline
synthetic demo, outside a repository checkout; it never invokes a paid command.
It reads public health/OpenAPI/MCP/agent cards/homepage/buyers metadata and sends
one empty-body IBAN REST quote probe, one empty-arguments MCP IBAN quote probe
and one A2A ``message/send`` with ``operation=check_iban`` and ``request={}``.
It never registers keys, signs, pays, executes verification or uploads invoices
or supplier identities. No output URLs are followed.
All requests identify as ``scout-distribution-monitor``: diagnostics, not
customers, purchases, demand or independent adoption.

Run locally with ``python scripts/check_discovery.py --service supplier --output discovery.json``.
The JSON, Actions artifact and step summary contain only fixed check names and
statuses, never response bodies, headers or private data. Requests refuse
redirects, cap bodies at 2 MB and use 15-second socket timeouts; failures exit
nonzero. A missing PyPI project is explicitly ``pending``, without masking
service failures. The scheduled workflow uses ``--require-pypi`` to enforce
PyPI availability and repository/MIT/name metadata.
The homepage and ``/buyers.txt`` must link this public client repository.

GitHub schedules can be delayed or dropped and public-repository schedules
may be disabled after 60 days without repository activity. This is not
guaranteed continuous uptime monitoring; review Actions and re-enable schedules
when needed. No artificial activity, stars or promotional posting is generated.

License
-------

MIT licensed; see ``LICENSE``. This covers this buyer client's source code,
not the separate verification service, registry data or API usage terms.
