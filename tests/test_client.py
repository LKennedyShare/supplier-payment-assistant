import base64
from copy import deepcopy
from decimal import Decimal
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from x402.http import encode_payment_required_header, encode_payment_response_header
from x402.schemas import PaymentPayload, PaymentRequired, SettleResponse

from supplier_payment_assistant.examples import DECISION_EXAMPLE
from supplier_payment_assistant.contracts import PaymentRequest
from supplier_payment_assistant import client as buyer

BASE = "https://verifier.example"
NETWORK = "eip155:84532"


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    original = httpx.Client.send

    def guarded(client, *args, **kwargs):
        if not isinstance(client._transport, httpx.MockTransport):
            pytest.fail("Only mocked HTTP is permitted in buyer tests")
        return original(client, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", guarded)

@pytest.fixture
def unsigned():
    request = deepcopy(DECISION_EXAMPLE)
    request.pop("signature")
    return request


def required(index, **changes):
    option = {
        "scheme": "exact", "network": NETWORK,
        "asset": buyer.USDC_ASSETS[NETWORK],
        "amount": str(buyer.OPERATIONS[index][1]),
        "payTo": "0x" + "1" * 40, "maxTimeoutSeconds": 60,
        "extra": {"name": "USDC", "version": "2"},
    }
    option.update(changes)
    return PaymentRequired.model_validate({
        "x402Version": 2, "resource": {"url": BASE + buyer.OPERATIONS[index][0]},
        "accepts": [option],
    })


def challenge(index, quote=None):
    quote = quote or required(index)
    return httpx.Response(402, json=quote.model_dump(by_alias=True), headers={
        "PAYMENT-REQUIRED": encode_payment_required_header(quote),
    })


def settled(index, *, status=200, success=True, header="PAYMENT-RESPONSE"):
    settlement = SettleResponse.model_validate({
        "success": success, "network": NETWORK, "transaction": "0x" + "a" * 64,
        "amount": str(buyer.OPERATIONS[index][1]),
    })
    return httpx.Response(status, json={"registered": True} if index == 0 else {
        "decision": "FLAG_FOR_HUMAN_REVIEW", "execution_payload": None,
    }, headers={header: encode_payment_response_header(settlement)})


class FakeSDK:
    def __init__(self):
        self.signed = []

    def create_payment_payload(self, quote):
        self.signed.append(quote)
        return PaymentPayload.model_validate({
            "x402Version": 2, "accepted": quote.accepts[0],
            "resource": quote.resource, "payload": {"signature": "MOCK-NOT-ONCHAIN"},
        })


@pytest.fixture
def sdk(monkeypatch):
    sdk = FakeSDK()
    monkeypatch.setattr(buyer, "make_sdk", lambda network: sdk)
    return sdk


class Flow:
    def __init__(self, *, paid_responses=None, fresh_quotes=None, probe_status=None):
        self.calls = []
        self.paid_responses = paid_responses or {}
        self.fresh_quotes = fresh_quotes or {}
        self.probe_status = probe_status
        self.probes = [0, 0]

    def __call__(self, request):
        self.calls.append(request)
        index = [path for path, _ in buyer.OPERATIONS].index(request.url.path)
        if request.headers.get("PAYMENT-SIGNATURE"):
            assert json.loads(request.content)
            return self.paid_responses.get(index, settled(index))
        assert json.loads(request.content) == {}
        self.probes[index] += 1
        if self.probe_status:
            return httpx.Response(self.probe_status, headers={"Location": BASE}, json={})
        quote = self.fresh_quotes.get(index) if self.probes[index] > 1 else None
        return challenge(index, quote)


def test_offline_demo_no_network_wallet_or_private_key_output(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("offline demo attempted network/wallet access")

    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.setattr(buyer, "make_sdk", forbidden)
    monkeypatch.setenv("X402_CLIENT_PRIVATE_KEY", "DO-NOT-READ-THIS")
    assert buyer.main(["--demo"]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["mode"] == "SYNTHETIC_OFFLINE_ILLUSTRATION"
    assert result["live_registry_checks"] is False
    assert result["payment_made"] is False
    assert result["invoice_money_moved"] is False
    assert "NOT a real verification" in result["message"]
    assert "illustrative_response_NOT_VERIFIED" in result
    assert "PRIVATE KEY" not in output and "DO-NOT-READ-THIS" not in output


@pytest.mark.parametrize("numeric", [False, True])
def test_canonical_signature_verifies_public_signing_contract(unsigned, numeric):
    if numeric:
        for field in ("invoice", "purchase_order"):
            unsigned[field]["amount"] = 1000.0
            unsigned[field]["line_items"][0].update(quantity=1, unit_price=1000.0)
        unsigned["goods_receipt"]["received"][0]["quantity"] = 1
    registration, signed = buyer.sign_request(unsigned)
    parsed = PaymentRequest.model_validate(signed)

    assert parsed.signature.key_id == registration["key_id"]
    public_key = serialization.load_pem_public_key(registration["public_key_pem"].encode())
    public_key.verify(
        base64.b64decode(parsed.signature.signature_b64), parsed.signed_body(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    assert isinstance(signed["invoice"]["amount"], str)
    raw_body = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    if numeric:
        assert raw_body != parsed.signed_body()
    assert base64.b64decode(signed["signature"]["signature_b64"])
    other_registration, other = buyer.sign_request(unsigned)
    assert other["transaction_id"] == signed["transaction_id"] == unsigned["transaction_id"]
    assert other_registration["key_id"] != registration["key_id"]
    assert other_registration["public_key_pem"] != registration["public_key_pem"]


def test_inspect_never_loads_wallet_or_registers(unsigned, monkeypatch):
    def forbidden(*args):
        pytest.fail("inspection accessed wallet")

    monkeypatch.setattr(buyer, "make_sdk", forbidden)
    monkeypatch.delenv("X402_CLIENT_PRIVATE_KEY", raising=False)
    flow = Flow()
    result = buyer.run_live(BASE, unsigned, transport=httpx.MockTransport(flow))
    assert len(flow.calls) == 2
    assert all("PAYMENT-SIGNATURE" not in request.headers for request in flow.calls)
    assert result["authorized_usdc"] == result["confirmed_spent_usdc"] == "0"
    assert result["key_registered"] is result["payment_made"] is False
    assert result["expected_combined_usdc"] == "0.055"


def test_complete_two_step_mocked_settlement(unsigned, sdk):
    flow = Flow(paid_responses={1: settled(1, header="X-PAYMENT-RESPONSE")})
    result = buyer.run_live(BASE, unsigned, pay=True, transport=httpx.MockTransport(flow))
    assert len(flow.calls) == 6
    assert len(sdk.signed) == 2
    assert all(len(quote.accepts) == 1 for quote in sdk.signed)
    paid = [request for request in flow.calls if request.headers.get("PAYMENT-SIGNATURE")]
    registration, decision = [json.loads(request.content) for request in paid]
    assert registration["key_id"] == decision["signature"]["key_id"]
    assert result["authorized_usdc"] == result["confirmed_spent_usdc"] == "0.055"
    assert result["key_registered"] is result["payment_made"] is True
    assert result["invoice_money_moved"] is False


def test_official_sdk_signs_offchain_only(unsigned, monkeypatch):
    # Publicly predictable, unfunded test material; never a real wallet.
    monkeypatch.setenv("X402_CLIENT_PRIVATE_KEY", "0x" + "1" * 64)
    flow = Flow()
    result = buyer.run_live(BASE, unsigned, pay=True, transport=httpx.MockTransport(flow))
    assert result["confirmed_spent_usdc"] == "0.055"
    assert len(flow.calls) == 6


def test_over_cap_before_sdk_or_any_authorization(unsigned, monkeypatch):
    def forbidden(*args):
        pytest.fail("over-budget flow constructed a wallet")

    monkeypatch.setattr(buyer, "make_sdk", forbidden)
    flow = Flow()
    with pytest.raises(buyer.BuyerError, match="Combined") as error:
        buyer.run_live(BASE, unsigned, pay=True, max_usdc=Decimal("0.054"),
                       transport=httpx.MockTransport(flow))
    assert error.value.report["authorized_usdc"] == "0"
    assert len(flow.calls) == 2


def test_remaining_budget_checked_before_signing():
    with pytest.raises(ValueError, match="Combined"):
        buyer.validate_challenge(required(1), BASE + buyer.OPERATIONS[1][0],
                                 50000, Decimal("0.055"), authorized=6000)


@pytest.mark.parametrize("changes", [
    {"scheme": "upto"}, {"network": "eip155:1"},
    {"asset": "0x" + "2" * 40}, {"amount": "5001"},
    {"amount": "-5000"}, {"amount": "5e3"}, {"payTo": "0x" + "0" * 40},
    {"payTo": "not-an-address"}, {"maxTimeoutSeconds": 0},
    {"maxTimeoutSeconds": 301},
])
def test_unsafe_quote_rejected(changes):
    with pytest.raises(ValueError):
        buyer.validate_challenge(required(0, **changes), BASE + buyer.OPERATIONS[0][0],
                                 5000, Decimal("0.055"))


@pytest.mark.parametrize("url", [
    "http://example.com", "https://user:secret@example.com",
    "https://example.com/path", "https://example.com?foo=bar",
    "https://example.com#fragment", "https://example.com\\bad",
    "https://example.com/\n", "https://example.com/%2f",
])
def test_unsafe_origin_rejected(url):
    with pytest.raises(ValueError):
        buyer.operation_urls(url)


@pytest.mark.parametrize("base", ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"])
def test_local_http_allowed(base):
    assert buyer.operation_urls(base)[0] == base + "/v1/agent-keys"


@pytest.mark.parametrize("resource", [
    None, "https://attacker.example/v1/agent-keys",
    "http://verifier.example/v1/agent-keys",
    BASE + "/v1/payment-decisions",
])
def test_resource_rejected(resource):
    quote = required(0)
    quote = quote.model_copy(update={
        "resource": None if resource is None else quote.resource.model_copy(update={"url": resource}),
    })
    with pytest.raises(ValueError):
        buyer.validate_challenge(quote, BASE + buyer.OPERATIONS[0][0], 5000, Decimal("0.055"))


def test_ambiguous_and_legacy_quotes_rejected():
    for quote in [
        required(0).model_copy(update={"x402_version": 1}),
        required(0).model_copy(update={"accepts": []}),
        required(0).model_copy(update={"accepts": required(0).accepts * 2}),
    ]:
        with pytest.raises(ValueError):
            buyer.validate_challenge(quote, BASE + buyer.OPERATIONS[0][0], 5000, Decimal("0.055"))


def test_network_mismatch_before_first_signature(unsigned, sdk):
    def handler(request):
        index = 0 if request.url.path.endswith("agent-keys") else 1
        quote = required(index, **({
            "network": "eip155:8453", "asset": buyer.USDC_ASSETS["eip155:8453"],
        } if index else {}))
        return challenge(index, quote)

    with pytest.raises(buyer.BuyerError, match="same network"):
        buyer.run_live(BASE, unsigned, pay=True, transport=httpx.MockTransport(handler))
    assert not sdk.signed


@pytest.mark.parametrize("changes", [
    {"amount": "50001"},
    {"network": "eip155:8453", "asset": buyer.USDC_ASSETS["eip155:8453"]},
    {"scheme": "wildcard"},
])
def test_fresh_decision_quote_revalidated_after_registration(unsigned, sdk, changes):
    flow = Flow(fresh_quotes={1: required(1, **changes)})
    with pytest.raises(buyer.BuyerError) as error:
        buyer.run_live(BASE, unsigned, pay=True, transport=httpx.MockTransport(flow))
    assert len(sdk.signed) == 1
    assert error.value.report["key_registered"] is True
    assert error.value.report["authorized_usdc"] == "0.005"
    assert error.value.report["confirmed_spent_usdc"] == "0.005"


@pytest.mark.parametrize("status", [301, 302, 307, 308, 404, 429, 500, 200])
def test_probe_errors_no_paid_retry(unsigned, sdk, status):
    flow = Flow(probe_status=status)
    with pytest.raises(buyer.BuyerError):
        buyer.run_live(BASE, unsigned, pay=True, transport=httpx.MockTransport(flow))
    assert len(flow.calls) == 1 and not sdk.signed


@pytest.mark.parametrize("status", [302, 404, 429, 402, 500])
def test_paid_error_no_repeat_and_partial_budget(unsigned, sdk, status):
    flow = Flow(paid_responses={1: httpx.Response(status, json={"error": "not echoed"})})
    with pytest.raises(buyer.BuyerError, match=f"HTTP {status}") as error:
        buyer.run_live(BASE, unsigned, pay=True, transport=httpx.MockTransport(flow))
    assert len(sdk.signed) == 2 and len(flow.calls) == 6
    assert error.value.report["authorized_usdc"] == "0.055"
    assert error.value.report["confirmed_spent_usdc"] == "0.005"
    assert error.value.report["key_registered"] is True
    assert error.value.report["operations"][1]["settlement"] == "unconfirmed"


@pytest.mark.parametrize("index", [0, 1])
def test_missing_settlement_is_unconfirmed_stop(unsigned, sdk, index):
    flow = Flow(paid_responses={index: httpx.Response(200, json={"ok": True})})
    with pytest.raises(buyer.BuyerError, match="Missing settlement") as error:
        buyer.run_live(BASE, unsigned, pay=True, transport=httpx.MockTransport(flow))
    assert len(sdk.signed) == index + 1
    assert error.value.report["operations"][index]["settlement"] == "unconfirmed"
    assert error.value.report["payment_made"] is bool(index)
    assert error.value.report["key_registered"] is True


def test_failed_settlement_explicit_stop(unsigned, sdk):
    flow = Flow(paid_responses={0: settled(0, success=False)})
    with pytest.raises(buyer.BuyerError, match="Settlement failed") as error:
        buyer.run_live(BASE, unsigned, pay=True, transport=httpx.MockTransport(flow))
    assert len(sdk.signed) == 1
    assert error.value.report["operations"][0]["settlement"] == "failed"
    assert error.value.report["payment_made"] is False


def test_cli_supplied_json_and_nonzero_partial_failure(unsigned, sdk, monkeypatch, capsys):
    read_paths = []

    def read_json_source(path, *, encoding):
        read_paths.append(str(path))
        assert encoding == "utf-8"
        return json.dumps(unsigned)

    monkeypatch.setattr(buyer.Path, "read_text", read_json_source)
    original = buyer.run_live
    flow = Flow(paid_responses={1: settled(1, success=False)})
    monkeypatch.setattr(buyer, "run_live", lambda *args, **kwargs: original(
        *args, **kwargs, transport=httpx.MockTransport(flow),
    ))
    assert buyer.main([BASE, "--request", "caller-invoice.json", "--pay"]) == 1
    result = json.loads(capsys.readouterr().err.splitlines()[-1])
    assert read_paths == ["caller-invoice.json"]
    assert result["error"].startswith("Settlement failed")
    assert result["confirmed_spent_usdc"] == "0.005"
    assert result["authorized_usdc"] == "0.055"


def test_missing_wallet_never_authorizes(unsigned, monkeypatch):
    monkeypatch.delenv("X402_CLIENT_PRIVATE_KEY", raising=False)
    with pytest.raises(buyer.BuyerError, match="requires X402_CLIENT_PRIVATE_KEY") as error:
        buyer.run_live(BASE, unsigned, pay=True, transport=httpx.MockTransport(Flow()))
    assert error.value.report["authorized_usdc"] == "0"


def test_cli_requires_supplied_request_and_caps(capsys):
    for args in [[], [BASE], ["--demo", "--pay"], ["--demo", "--max-usdc", "0.056"],
                 ["--demo", "--max-usdc", "NaN"], ["--demo", "--max-usdc", "0"]]:
        with pytest.raises(SystemExit) as error:
            buyer.main(args)
        assert error.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("pay", ["false", "true", 1, 0, None])
def test_reusable_workflow_requires_boolean_authorization(unsigned, pay):
    with pytest.raises(ValueError, match="explicit boolean"):
        buyer.run_live(BASE, unsigned, pay=pay)


@pytest.mark.parametrize("cap", [True, False, 0.055, None, "NaN", "Infinity", "bad", "0", "0.056"])
def test_reusable_workflow_rejects_inexact_or_invalid_caps(unsigned, cap):
    with pytest.raises(ValueError):
        buyer.run_live(BASE, unsigned, max_usdc=cap)


def test_reusable_workflow_accepts_exact_string_cap_and_attributes_calls(unsigned):
    flow = Flow()
    result = buyer.run_live(BASE, unsigned, max_usdc="0.055", transport=httpx.MockTransport(flow))
    assert result["authorized_usdc"] == "0"
    assert all(r.headers["X-Client-Name"] == "supplier-payment-assistant" for r in flow.calls)
    assert all(r.headers["User-Agent"].startswith("supplier-payment-assistant/") for r in flow.calls)


@pytest.mark.parametrize("arguments", [
    ["-m", "supplier_payment_assistant"],
    ["-m", "supplier_payment_assistant.client"],
])
def test_module_entrypoints_run_offline(arguments):
    result = subprocess.run([sys.executable, *arguments, "--demo"], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    demo = json.loads(result.stdout)
    assert demo["mode"] == "SYNTHETIC_OFFLINE_ILLUSTRATION" and not demo["payment_made"]
