"""Offline illustration or explicitly capped, two-step x402 REST buyer."""

import argparse
import base64
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from eth_account import Account
from x402 import x402ClientSync
from x402.http import x402HTTPClientSync
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact import ExactEvmClientScheme
from x402.schemas.errors import PaymentError

from .examples import DECISION_EXAMPLE, DECISION_RESULT_EXAMPLE
from .contracts import PaymentRequest

USDC_ASSETS = {
    "eip155:8453": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "eip155:84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
}
OPERATIONS = (("/v1/agent-keys", 5000), ("/v1/payment-decisions", 50000))
MAX_ATOMIC = 55000


class BuyerError(ValueError):
    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


def spend_cap(value):
    try:
        cap = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("max-usdc must be a positive decimal <= 0.055") from exc
    if not cap.is_finite() or not 0 < cap <= Decimal("0.055"):
        raise argparse.ArgumentTypeError("max-usdc must be a positive decimal <= 0.055")
    return cap


def within_budget(amount, cap):
    numerator, denominator = cap.as_integer_ratio()
    if amount > MAX_ATOMIC or amount * denominator > numerator * 1_000_000:
        raise ValueError("Combined x402 authorizations exceed --max-usdc or 0.055 USDC")


def validate_origin(url):
    if (
        any(c.isspace() or ord(c) < 32 for c in url)
        or "\\" in url or "#" in url or "%" in url
    ):
        raise ValueError("Unsafe URL")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("An HTTP(S) origin is required")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL credentials are forbidden")
    local = parts.hostname.lower() == "localhost"
    try:
        local = local or ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        pass
    if parts.scheme == "http" and not local:
        raise ValueError("HTTP is allowed only for localhost/loopback")
    return parts.scheme, parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)


def operation_urls(base_url):
    validate_origin(base_url)
    parts = urlsplit(base_url)
    if parts.path not in ("", "/") or parts.query or "?" in base_url:
        raise ValueError("base_url must be an origin, without a path or query")
    return [base_url.rstrip("/") + path for path, _ in OPERATIONS]


def sign_request(unsigned):
    """Use precisely the server model's decimal serialization and canonical bytes."""
    if not isinstance(unsigned, dict):
        raise ValueError("Request must be a JSON object")
    if "signature" in unsigned:
        raise ValueError("--request must contain an unsigned request (omit signature)")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_id = "buyer-" + uuid4().hex
    # Preserve the caller's transaction_id: retries must not invent new transactions.
    request = PaymentRequest.model_validate({
        **unsigned, "signature": {"key_id": key_id, "signature_b64": "pending"},
    })
    signature = key.sign(
        request.signed_body(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    body = request.model_dump(mode="json")
    body["signature"]["signature_b64"] = base64.b64encode(signature).decode()
    registration = {
        "key_id": key_id,
        "public_key_pem": key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode(),
    }
    return registration, body


def offline_demo():
    unsigned = deepcopy(DECISION_EXAMPLE)
    unsigned.pop("signature")
    registration, signed = sign_request(unsigned)
    return {
        "mode": "SYNTHETIC_OFFLINE_ILLUSTRATION",
        "message": "SYNTHETIC: illustrative only, NOT a real verification or payment authorization. "
                   "No live gates ran; no key was registered; no wallet or network was used.",
        "live_registry_checks": False,
        "payment_made": False,
        "invoice_money_moved": False,
        "public_key": registration,
        "signed_synthetic_request": signed,
        "illustrative_response_NOT_VERIFIED": deepcopy(DECISION_RESULT_EXAMPLE),
    }


def check_status(response, *, challenge=False):
    status = response.status_code
    if 300 <= status < 400:
        raise ValueError(f"HTTP {status}: ALL redirects are refused")
    if challenge and status == 402:
        return
    if status == 402:
        raise ValueError("HTTP 402: paid retry rejected; no automatic retry")
    if status == 429:
        raise ValueError("HTTP 429: rate limited; no automatic retry")
    if status == 404:
        raise ValueError("HTTP 404: route/data not found; no automatic retry")
    if not 200 <= status < 300:
        raise ValueError(f"HTTP {status}: operation failed; no automatic retry")
    if challenge:
        raise ValueError("Expected an unsigned HTTP 402 challenge; no payment authorized")


def validate_challenge(required, url, expected_atomic, cap, authorized=0, network=None):
    if required.x402_version != 2 or len(required.accepts) != 1:
        raise ValueError("Require x402 v2 with one unambiguous exact USDC quote")
    resource = required.resource
    if not resource or not resource.url:
        raise ValueError("Challenge must identify its resource URL")
    if validate_origin(resource.url) != validate_origin(url) or resource.url != url:
        raise ValueError("Challenge resource must be the exact same-origin operation URL")
    option = required.accepts[0]
    asset = USDC_ASSETS.get(option.network)
    if (
        option.scheme != "exact" or asset is None
        or option.asset.lower() != asset.lower()
        or (network is not None and option.network != network)
    ):
        raise ValueError("Require consistent Base/Base Sepolia network and exact USDC asset/scheme")
    if (
        not re.fullmatch(r"[0-9]+", option.amount)
        or int(option.amount) != expected_atomic
    ):
        raise ValueError("Unexpected quoted price (expected 0.005 + 0.05 USDC)")
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", option.pay_to) or int(option.pay_to, 16) == 0:
        raise ValueError("Invalid payTo address")
    if not 0 < option.max_timeout_seconds <= 300:
        raise ValueError("Unsafe payment authorization lifetime (must be 1..300 seconds)")
    within_budget(authorized + int(option.amount), cap)
    return option


def make_sdk(network):
    key = os.environ.get("X402_CLIENT_PRIVATE_KEY")
    if not key:
        raise ValueError("--pay requires X402_CLIENT_PRIVATE_KEY")
    try:
        account = Account.from_key(key)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid X402_CLIENT_PRIVATE_KEY") from exc
    sdk = x402ClientSync()
    sdk.register(network, ExactEvmClientScheme(EthAccountSigner(account)))
    return sdk


def run_live(base_url, unsigned, *, pay=False, max_usdc=Decimal("0.055"), transport=None):
    if type(pay) is not bool:
        raise ValueError("pay must be an explicit boolean; inspection is the default")
    urls = operation_urls(base_url)
    if isinstance(max_usdc, bool) or not isinstance(max_usdc, (str, Decimal)):
        raise ValueError("max-usdc must be an exact decimal string or Decimal")
    try:
        max_usdc = Decimal(max_usdc)
    except InvalidOperation as exc:
        raise ValueError("max-usdc must be a finite positive decimal at most 0.055") from exc
    if not max_usdc.is_finite() or not 0 < max_usdc <= Decimal("0.055"):
        raise ValueError("--max-usdc must be positive and at most 0.055")
    registration, signed = sign_request(unsigned)
    report = {
        "mode": "PAY" if pay else "INSPECT_ONLY",
        "transaction_id": signed["transaction_id"],
        "max_usdc": str(max_usdc),
        "expected_combined_usdc": "0.055",
        "authorized_usdc": "0",
        "confirmed_spent_usdc": "0",
        "payment_made": False,
        "invoice_money_moved": False,
        "key_registered": False,
        "operations": [],
        "message": "x402 fees buy verification only, NOT execution_payload invoice money. "
                   "This client never executes invoice payments. "
                   "Inspection sends empty quote probes: no wallet signatures or key registration.",
    }
    authorized = confirmed = 0
    http = x402HTTPClientSync(x402ClientSync())

    def quote(client, index, network=None):
        # Empty input cannot register a key or submit an invoice if a route is ungated.
        response = client.post(urls[index], json={})
        check_status(response, challenge=True)
        required = http.get_payment_required_response(response.headers.get, response.json())
        option = validate_challenge(
            required, urls[index], OPERATIONS[index][1], max_usdc, authorized, network,
        )
        return required, option

    try:
        with httpx.Client(transport=transport, follow_redirects=False, timeout=30, trust_env=False,
                          headers={"X-Client-Name": "supplier-payment-assistant",
                                   "User-Agent": "supplier-payment-assistant/0.1"}) as client:
            quotes = [quote(client, index) for index in range(2)]
            network = quotes[0][1].network
            if quotes[1][1].network != network:
                raise ValueError("Both steps must quote the same network")
            within_budget(sum(int(option.amount) for _, option in quotes), max_usdc)
            report["quotes"] = [
                required.model_dump(by_alias=True, exclude_none=True) for required, _ in quotes
            ]
            if not pay:
                return report
            sdk = make_sdk(network)
            http = x402HTTPClientSync(sdk)
            report["message"] = "At most one paid retry per operation; never moves invoice money."
            for index, body in enumerate((registration, signed)):
                required, option = quote(client, index, network)
                # Pin the fresh quote and reserve its entire authorization before signing.
                within_budget(authorized + int(option.amount), max_usdc)
                authorized += int(option.amount)
                report["authorized_usdc"] = str(Decimal(authorized) / 1_000_000)
                operation = {"path": OPERATIONS[index][0], "settlement": "unconfirmed"}
                report["operations"].append(operation)
                headers = http.encode_payment_signature_header(sdk.create_payment_payload(
                    required.model_copy(update={"accepts": [option]}),
                ))
                response = client.post(urls[index], json=body, headers=headers, timeout=60)
                operation["http_status"] = response.status_code
                if index == 0 and 200 <= response.status_code < 300:
                    report["key_registered"] = True
                if not (response.headers.get("PAYMENT-RESPONSE") or response.headers.get("X-PAYMENT-RESPONSE")):
                    check_status(response)
                    raise ValueError("Missing settlement metadata: payment unconfirmed; STOP, do not auto-repeat")
                settlement = http.get_payment_settle_response(response.headers.get)
                if not settlement.success:
                    operation["settlement"] = "failed"
                    raise ValueError("Settlement failed; STOP, no automatic retry")
                if (
                    settlement.network != network or not settlement.transaction
                    or (settlement.amount is not None and settlement.amount != option.amount)
                ):
                    raise ValueError("Settlement metadata mismatch: payment unconfirmed; STOP")
                confirmed += int(option.amount)
                report["confirmed_spent_usdc"] = str(Decimal(confirmed) / 1_000_000)
                report["payment_made"] = True
                operation["settlement"] = "confirmed"
                operation["transaction"] = settlement.transaction
                check_status(response)
                operation["result"] = response.json()
            return report
    except (ValueError, PaymentError, httpx.HTTPError) as exc:
        # Do not print server error bodies, SDK exceptions, or wallet material.
        message = str(exc) if type(exc) is ValueError else "Transport/SDK/response error; STOP, no automatic retry"
        raise BuyerError(message, report) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", nargs="?", help="HTTPS origin; HTTP only on localhost/loopback")
    parser.add_argument("--request", type=Path, help="caller-supplied unsigned PaymentRequest JSON")
    parser.add_argument("--demo", action="store_true", help="completely offline SYNTHETIC illustration")
    parser.add_argument("--pay", action="store_true", help="authorize at most one paid retry per operation")
    parser.add_argument("--max-usdc", type=spend_cap, default=Decimal("0.055"))
    args = parser.parse_args(argv)
    if args.demo:
        if args.pay or args.request or args.base_url:
            parser.error("--demo cannot be combined with live arguments")
        result = offline_demo()
    else:
        if not args.base_url or not args.request:
            parser.error("live inspection/payment requires base_url and --request; use --demo offline")
        try:
            unsigned = json.loads(args.request.read_text(encoding="utf-8"))
            if not isinstance(unsigned, dict):
                raise ValueError("Request must be a JSON object")
            result = run_live(args.base_url, unsigned, pay=args.pay, max_usdc=args.max_usdc)
        except BuyerError as exc:
            print(json.dumps({"error": str(exc), **exc.report}), file=sys.stderr)
            return 1
        except (OSError, ValueError):
            print(json.dumps({"error": "Invalid/unreadable unsigned request or origin; no automatic retry"}), file=sys.stderr)
            return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
