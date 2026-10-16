import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Barrier, get_ident

import httpx
import pytest
from pydantic import ValidationError

from supplier_payment_assistant import BuyerError, SupplierPaymentTools
from supplier_payment_assistant import client
from supplier_payment_assistant.contracts import PaymentRequest
from supplier_payment_assistant.examples import DECISION_EXAMPLE


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    original_connect = socket.socket.connect

    def forbidden(*args, **kwargs):
        pytest.fail("Integration tests must never connect to a live service/provider")

    def connect(sock, address):
        # Windows asyncio creates its internal wake-up socket pair on loopback.
        if sys._getframe(1).f_code.co_name == "_fallback_socketpair":
            return original_connect(sock, address)
        return forbidden(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def unsigned():
    body = deepcopy(DECISION_EXAMPLE)
    body.pop("signature")
    body["invoice"]["amount"] = 1000
    return body


@pytest.fixture
def request_json(unsigned):
    return json.dumps(unsigned)


def test_demo_and_inspection_no_wallet_or_data_upload(monkeypatch, request_json):
    from test_client import challenge

    def no_wallet(*args, **kwargs):
        pytest.fail("demo/inspection read a wallet key")

    monkeypatch.setattr(client.os.environ, "get", no_wallet)
    monkeypatch.setattr(client, "make_sdk", no_wallet)
    toolset = SupplierPaymentTools(base_url="https://verifier.example")
    assert toolset.offline_demo()["payment_made"] is False
    calls = []

    def respond(request):
        calls.append(request)
        assert json.loads(request.content) == {}
        assert "PAYMENT-SIGNATURE" not in request.headers
        return challenge(len(calls) - 1)

    real_run = client.run_live

    def mocked_origin(base_url, body, **kwargs):
        assert kwargs == {"pay": False}
        return real_run(base_url, body, transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(client, "run_live", mocked_origin)
    result = toolset.inspect_supplier(request_json)
    assert result["mode"] == "INSPECT_ONLY"
    assert result["authorized_usdc"] == "0"
    assert len(calls) == 2
    assert toolset.reserved_usdc == 0 and toolset.paid_calls == 0


def test_approval_normalized_frozen_and_canonical(monkeypatch, request_json):
    approvals = []
    received = []
    toolset = None

    def policy(approval):
        approvals.append(approval)
        assert toolset.reserved_usdc == 0  # Callback runs outside the lock.
        assert approval.unsigned_request["invoice"]["amount"] == "1000"
        with pytest.raises(TypeError):
            approval.unsigned_request["invoice"]["amount"] = "999999"
        with pytest.raises(TypeError):
            approval.unsigned_request["invoice"]["line_items"][0]["sku"] = "TAMPERED"
        with pytest.raises(TypeError):
            approval.unsigned_request["goods_receipt"]["received"][0] = {}
        with pytest.raises(FrozenInstanceError):
            approval.canonical_body = b"tampered"
        assert sha256(approval.canonical_body).hexdigest() == approval.canonical_sha256
        return True

    def run(base, body, **kwargs):
        assert toolset.reserved_usdc == Decimal("0.055")
        assert toolset.paid_calls == 1
        assert kwargs == {"pay": True, "max_usdc": Decimal("0.055")}
        received.append(body)
        return {"mode": "MOCKED_PAID_VERIFICATION", "payment_made": False}

    monkeypatch.setattr(client, "run_live", run)
    toolset = SupplierPaymentTools(approval_policy=policy)
    assert toolset.verify_supplier_paid(request_json)["payment_made"] is False
    assert json.loads(approvals[0].canonical_body) == received[0]
    model = PaymentRequest.model_validate({
        **received[0], "signature": {"key_id": "placeholder", "signature_b64": "placeholder"},
    })
    assert model.signed_body() == approvals[0].canonical_body
    assert "signature" not in received[0]


@pytest.mark.parametrize("value", [False, "true", "false", 1, None, {}, [True]])
def test_non_literal_approval_denied(monkeypatch, request_json, value):
    monkeypatch.setattr(client, "run_live", lambda *a, **k: pytest.fail("denied request ran"))
    tools = SupplierPaymentTools(approval_policy=lambda _: value)
    with pytest.raises(BuyerError, match="denied"):
        tools.verify_supplier_paid(request_json)
    assert tools.reserved_usdc == 0 and tools.paid_calls == 0


def test_policy_error_redacted_and_no_reservation(monkeypatch, request_json):
    def policy(_):
        raise RuntimeError("SECRET-INVOICE")

    tools = SupplierPaymentTools(approval_policy=policy)
    with pytest.raises(BuyerError, match="policy failed") as exc:
        tools.verify_supplier_paid(request_json)
    assert "SECRET" not in str(exc.value)
    assert tools.reserved_usdc == 0


@pytest.mark.parametrize("value", ["{bad", "[]", "null", "1", '"false"', {}, True])
@pytest.mark.parametrize("method", ["inspect_supplier", "verify_supplier_paid"])
def test_invalid_json_before_approval_or_signing(monkeypatch, value, method):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid request reached approval, signing or network")

    tools = SupplierPaymentTools(approval_policy=forbidden)
    monkeypatch.setattr(client, "run_live", forbidden)
    monkeypatch.setattr(client, "sign_request", forbidden)
    with pytest.raises(BuyerError, match="Invalid unsigned"):
        getattr(tools, method)(value)
    assert tools.reserved_usdc == 0


@pytest.mark.parametrize("field", ["signature", "pay", "approval", "max_usdc", "url", "wallet"])
def test_unknown_fields_rejected_before_approval(monkeypatch, unsigned, field):
    def forbidden(*args, **kwargs):
        pytest.fail("unvalidated fields reached operator/payment")

    unsigned[field] = "false"
    tools = SupplierPaymentTools(approval_policy=forbidden)
    monkeypatch.setattr(client, "run_live", forbidden)
    with pytest.raises(BuyerError):
        tools.verify_supplier_paid(json.dumps(unsigned))
    assert tools.reserved_usdc == 0


def test_nested_unknown_and_schema_error_before_policy(unsigned):
    unsigned["invoice"]["pay"] = True
    tools = SupplierPaymentTools(approval_policy=lambda _: pytest.fail("invalid schema approved"))
    with pytest.raises(BuyerError):
        tools.verify_supplier_paid(json.dumps(unsigned))
    del unsigned["invoice"]["pay"]
    unsigned["invoice"]["amount"] = "-1"
    with pytest.raises(BuyerError):
        tools.verify_supplier_paid(json.dumps(unsigned))


def test_paid_disabled_even_direct_call(request_json):
    tools = SupplierPaymentTools()
    with pytest.raises(BuyerError, match="disabled"):
        tools.verify_supplier_paid(request_json)
    assert tools.reserved_usdc == 0


def test_trusted_digest_example_allowlist(monkeypatch, request_json):
    from examples.trusted_approval import tools_for_trusted_digests

    captured = []
    capture = SupplierPaymentTools(approval_policy=lambda request: captured.append(request))
    with pytest.raises(BuyerError, match="denied"):
        capture.verify_supplier_paid(request_json)
    trusted_digests = {captured[0].canonical_sha256}
    tools = tools_for_trusted_digests(trusted_digests)
    trusted_digests.clear()
    monkeypatch.setattr(client, "run_live", lambda *a, **k: {"mode": "MOCKED"})
    assert tools.verify_supplier_paid(request_json) == {"mode": "MOCKED"}
    with pytest.raises(BuyerError, match="denied"):
        tools_for_trusted_digests(frozenset()).verify_supplier_paid(request_json)


def test_concurrent_full_reservation_only_one(monkeypatch, request_json):
    barrier = Barrier(2)
    runs = []

    def policy(_):
        barrier.wait(timeout=10)
        return True

    def run(*args, **kwargs):
        runs.append(kwargs)
        return {"settlement_status": "MOCKED"}

    tools = SupplierPaymentTools(approval_policy=policy, max_paid_calls=10)
    monkeypatch.setattr(client, "run_live", run)

    def attempt():
        try:
            return tools.verify_supplier_paid(request_json)
        except BuyerError:
            return "exhausted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))
    assert len(runs) == 1 and results.count("exhausted") == 1
    assert tools.reserved_usdc == Decimal("0.055") and tools.paid_calls == 1


@pytest.mark.parametrize("partial", [False, True])
def test_failure_consumes_full_reservation_no_retry(monkeypatch, request_json, partial):
    calls = []
    report = {"authorized_usdc": "0.005", "confirmed_spent_usdc": "0", "operations": []}

    def run(*args, **kwargs):
        calls.append(kwargs)
        if partial:
            raise BuyerError("SECRET-INVOICE", report)
        raise RuntimeError("SECRET-PRIVATE-KEY")

    monkeypatch.setattr(client, "run_live", run)
    tools = SupplierPaymentTools(approval_policy=lambda _: True)
    with pytest.raises(BuyerError, match="reservation retained") as exc:
        tools.verify_supplier_paid(request_json)
    assert "SECRET" not in str(exc.value)
    if partial:
        assert exc.value.report == report
    else:
        assert exc.value.report["settlement_status"] == "UNKNOWN"
    with pytest.raises(BuyerError, match="exhausted"):
        tools.verify_supplier_paid(request_json)
    assert len(calls) == 1 and tools.reserved_usdc == Decimal("0.055")


def test_max_calls_and_cumulative_ceiling_independent(monkeypatch, request_json):
    monkeypatch.setattr(client, "run_live", lambda *a, **k: {})
    tools = SupplierPaymentTools(
        approval_policy=lambda _: True, workflow_cap_usdc="0.02", max_paid_calls=10,
    )
    tools.verify_supplier_paid(request_json)
    tools.verify_supplier_paid(request_json)
    with pytest.raises(BuyerError, match="exhausted"):
        tools.verify_supplier_paid(request_json)
    assert tools.reserved_usdc == Decimal("0.04") and tools.paid_calls == 2
    tools = SupplierPaymentTools(approval_policy=lambda _: True, workflow_cap_usdc="0.01")
    tools.verify_supplier_paid(request_json)
    with pytest.raises(BuyerError):
        tools.verify_supplier_paid(request_json)
    assert tools.reserved_usdc == Decimal("0.01") and tools.paid_calls == 1


def test_budget_never_rounds_down(monkeypatch, request_json):
    monkeypatch.setattr(client, "run_live", lambda *a, **k: {})
    cap = Decimal("0.027500000000000000000000000001")
    tools = SupplierPaymentTools(
        approval_policy=lambda _: True, workflow_cap_usdc=cap, max_paid_calls=2,
    )
    with localcontext() as context:
        context.prec = 2
        tools.verify_supplier_paid(request_json)
        with pytest.raises(BuyerError, match="exhausted"):
            tools.verify_supplier_paid(request_json)
    assert tools.reserved_usdc == cap and tools.paid_calls == 1


@pytest.mark.parametrize("kwargs", [
    {"approval_policy": "true"}, {"max_paid_calls": True}, {"max_paid_calls": "1"},
    {"max_paid_calls": 0}, {"workflow_cap_usdc": True}, {"workflow_cap_usdc": 0.055},
    {"workflow_cap_usdc": "NaN"}, {"workflow_cap_usdc": "Infinity"},
    {"workflow_cap_usdc": "false"}, {"workflow_cap_usdc": "0.056"},
    {"workflow_cap_usdc": "0"}, {"base_url": "https://example.com/private"},
])
def test_invalid_operator_configuration(kwargs):
    with pytest.raises(ValueError):
        SupplierPaymentTools(**kwargs)


def framework_tools(name, tools):
    pytest.importorskip("langchain_core" if name == "langchain" else "agents")
    return tools.langchain_tools() if name == "langchain" else tools.openai_tools()


async def invoke(name, tool, arguments):
    if name == "langchain":
        return await tool.ainvoke(arguments)
    from agents.tool_context import ToolContext

    serialized = json.dumps(arguments)
    context = ToolContext(
        context=None, tool_name=tool.name, tool_call_id="offline-test",
        tool_arguments=serialized,
    )
    return await tool.on_invoke_tool(context, serialized)


@pytest.mark.parametrize("framework", ["langchain", "openai"])
def test_real_framework_objects_default_schema_and_demo(framework):
    tools = framework_tools(framework, SupplierPaymentTools())
    assert [tool.name for tool in tools] == ["offline_demo", "inspect_supplier"]
    expected_class = "StructuredTool" if framework == "langchain" else "FunctionTool"
    assert all(type(tool).__name__ == expected_class for tool in tools)
    for index, tool in enumerate(tools):
        schema = tool.args_schema.model_json_schema() if framework == "langchain" else tool.params_json_schema
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == ({"unsigned_request_json"} if index else set())
        if framework == "openai":
            assert tool.strict_json_schema is True
    result = asyncio.run(invoke(framework, tools[0], {}))
    assert result["mode"] == "SYNTHETIC_OFFLINE_ILLUSTRATION"
    assert result["payment_made"] is False and result["live_registry_checks"] is False
    json.dumps(result)


@pytest.mark.parametrize("framework", ["langchain", "openai"])
def test_real_framework_paid_normalized_invocation(monkeypatch, framework, request_json):
    approved = []
    monkeypatch.setattr(client, "run_live", lambda base, body, **kwargs: {
        "body": body, "paid": kwargs["pay"],
    })
    toolset = SupplierPaymentTools(approval_policy=lambda request: approved.append(request) is None)
    tools = framework_tools(framework, toolset)
    assert tools[-1].name == "verify_supplier_paid"
    result = asyncio.run(invoke(framework, tools[-1], {"unsigned_request_json": request_json}))
    assert result["paid"] is True
    assert result["body"] == json.loads(approved[0].canonical_body)
    assert toolset.reserved_usdc == Decimal("0.055")
    with pytest.raises(BuyerError):
        asyncio.run(invoke(framework, tools[-1], {"unsigned_request_json": request_json}))


@pytest.mark.parametrize("framework", ["langchain", "openai"])
@pytest.mark.parametrize("arguments", [
    {"pay": "false"}, {"approval": True}, {"max_usdc": "100"},
    {"url": "https://evil.example"}, {"wallet": "KEY"}, {"unsigned_request_json": {}},
])
def test_framework_strict_unknown_flags(monkeypatch, framework, arguments):
    monkeypatch.setattr(client, "run_live", lambda *a, **k: pytest.fail("bad flags ran"))
    tools = framework_tools(framework, SupplierPaymentTools())
    with pytest.raises((ValidationError, ValueError)):
        asyncio.run(invoke(framework, tools[1], arguments))


@pytest.mark.parametrize("framework", ["langchain", "openai"])
def test_framework_async_offload(monkeypatch, framework, request_json):
    loop_thread = get_ident()
    workers = []

    def run(*args, **kwargs):
        workers.append(get_ident())
        return {"mode": "MOCKED_INSPECTION"}

    monkeypatch.setattr(client, "run_live", run)
    tools = framework_tools(framework, SupplierPaymentTools())
    result = asyncio.run(invoke(framework, tools[1], {"unsigned_request_json": request_json}))
    assert result["mode"] == "MOCKED_INSPECTION"
    assert workers and all(worker != loop_thread for worker in workers)


@pytest.mark.parametrize("framework", ["langchain", "openai"])
def test_framework_errors_propagate_without_fallback(monkeypatch, framework):
    tools = framework_tools(framework, SupplierPaymentTools())
    with pytest.raises(BuyerError, match="Invalid unsigned"):
        asyncio.run(invoke(framework, tools[1], {"unsigned_request_json": "NOT-JSON"}))


def test_import_no_optional_frameworks_network_or_key_access():
    script = """
import builtins, os, socket, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    assert name.split('.')[0] not in ('agents', 'langchain_core'), name
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
def forbidden(*args, **kwargs):
    raise AssertionError('import attempted network')
socket.socket.connect = forbidden
socket.create_connection = forbidden
from supplier_payment_assistant import SupplierPaymentTools
import supplier_payment_assistant.tools
assert 'agents' not in sys.modules and 'langchain_core' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("framework,module", [("langchain", "langchain_core"), ("openai", "agents")])
def test_missing_extra_hint_only_for_top_level_missing(monkeypatch, framework, module):
    import builtins
    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name.split(".")[0] == module:
            raise ModuleNotFoundError("missing", name=module)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(ImportError, match=rf"\[{framework}\]"):
        getattr(SupplierPaymentTools(), f"{framework}_tools")()

    def broken_transitive(name, *args, **kwargs):
        if name.split(".")[0] == module:
            raise ModuleNotFoundError("broken framework dependency", name="transitive_dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_transitive)
    with pytest.raises(ModuleNotFoundError, match="broken framework dependency"):
        getattr(SupplierPaymentTools(), f"{framework}_tools")()


@pytest.mark.parametrize("framework", ["langchain", "openai"])
def test_offline_framework_example(framework):
    pytest.importorskip("langchain_core" if framework == "langchain" else "agents")
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["X402_CLIENT_PRIVATE_KEY"] = "NOT-A-WALLET"
    result = subprocess.run(
        [sys.executable, "-m", "examples.framework_demo", "--framework", framework],
        cwd=root, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    demo = json.loads(result.stdout)
    assert demo["mode"] == "SYNTHETIC_OFFLINE_ILLUSTRATION"
    assert demo["payment_made"] is False
    assert "NOT-A-WALLET" not in result.stdout
