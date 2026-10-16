"""Opt-in agent tools; approval and cumulative fee limits remain operator-owned."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from decimal import Context, Decimal, InvalidOperation, MAX_EMAX, MIN_EMIN, localcontext
from hashlib import sha256
import json
import inspect
from threading import Lock
from types import MappingProxyType
from typing import Callable, Mapping

from pydantic import BaseModel, ConfigDict, StrictStr
import httpx

from . import client
from .contracts import PaymentRequest

DEFAULT_BASE_URL = "https://supplier-payment-verifier-production.up.railway.app"
MAX_RESERVED_USDC = Decimal("0.055")


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class ApprovalRequest:
    """Immutable normalized request and digest to compare with a trusted source."""

    unsigned_request: Mapping
    canonical_body: bytes
    canonical_sha256: str
    workflow_cap_usdc: Decimal


class _NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _RequestArguments(_NoArguments):
    unsigned_request_json: StrictStr


def _validated_request(unsigned_request_json):
    try:
        if type(unsigned_request_json) is not str:
            raise ValueError
        unsigned = json.loads(unsigned_request_json)
        if not isinstance(unsigned, dict) or "signature" in unsigned:
            raise ValueError
        request = PaymentRequest.model_validate({
            **unsigned,
            "signature": {"key_id": "validation-only", "signature_b64": "validation-only"},
        })
        canonical = request.signed_body()
        # Round-trip canonical bytes before approval; never create a key or sign here.
        normalized = json.loads(canonical)
    except (ValueError, TypeError, OverflowError):
        raise client.BuyerError("Invalid unsigned PaymentRequest JSON", {}) from None
    return normalized, canonical


class SupplierPaymentTools:
    """Reuse one instance across calls; limits are in-memory, not wallet-wide."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        approval_policy: Callable[[ApprovalRequest], bool] | None = None,
        workflow_cap_usdc: Decimal | str = MAX_RESERVED_USDC,
        max_paid_calls: int = 1,
    ):
        client.operation_urls(base_url)
        if approval_policy is not None and (
            not callable(approval_policy) or inspect.iscoroutinefunction(approval_policy)
            or inspect.iscoroutinefunction(getattr(approval_policy, "__call__", None))
        ):
            raise ValueError("approval_policy must be an operator-supplied synchronous callable")
        if isinstance(workflow_cap_usdc, bool) or not isinstance(workflow_cap_usdc, (str, Decimal)):
            raise ValueError("workflow_cap_usdc must be an exact decimal")
        try:
            cap = Decimal(workflow_cap_usdc)
        except InvalidOperation:
            raise ValueError("Invalid workflow cap") from None
        if not cap.is_finite() or not 0 < cap <= MAX_RESERVED_USDC:
            raise ValueError("workflow_cap_usdc must be positive and at most 0.055")
        if type(max_paid_calls) is not int or max_paid_calls < 1:
            raise ValueError("max_paid_calls must be a positive integer")
        self._base_url = base_url
        self._approval_policy = approval_policy
        self._workflow_cap = cap
        self._max_paid_calls = max_paid_calls
        self._lock = Lock()
        self._reserved = Decimal("0")
        self._paid_calls = 0

    @property
    def reserved_usdc(self) -> Decimal:
        with self._lock:
            return self._reserved

    @property
    def paid_calls(self) -> int:
        """Number of reserved paid attempts, not confirmed payments."""
        with self._lock:
            return self._paid_calls

    def offline_demo(self) -> dict:
        """Show a synthetic offline illustration; no verification or payment."""
        return client.offline_demo()

    def inspect_supplier(self, unsigned_request_json: str) -> dict:
        """Inspect empty fee quotes only; do not upload invoice data or pay."""
        normalized, _ = _validated_request(unsigned_request_json)
        return client.run_live(self._base_url, normalized, pay=False)

    def verify_supplier_paid(self, unsigned_request_json: str) -> dict:
        """Buy advisory verification only, subject to independent operator policy."""
        normalized, canonical = _validated_request(unsigned_request_json)
        if self._approval_policy is None:
            raise client.BuyerError("Paid verification is disabled", {})
        approval = ApprovalRequest(
            _freeze(deepcopy(normalized)), canonical, sha256(canonical).hexdigest(),
            self._workflow_cap,
        )
        # Callback stays outside the lock: it may inspect counters or re-enter tools.
        try:
            approved = self._approval_policy(approval)
        except (RuntimeError, ValueError, TypeError):
            raise client.BuyerError("Operator approval policy failed", {}) from None
        if inspect.isawaitable(approved):
            if inspect.iscoroutine(approved):
                approved.close()
            raise client.BuyerError("Operator approval must return synchronously", {})
        if approved is not True:
            raise client.BuyerError("Operator approval denied", {})
        with self._lock:
            # Budget arithmetic must not round down in an application's decimal context.
            precision = len(self._workflow_cap.as_tuple().digits) + len(str(self._paid_calls + 1)) + 1
            with localcontext(Context(prec=max(28, precision), Emin=MIN_EMIN, Emax=MAX_EMAX)):
                next_reserved = self._workflow_cap * (self._paid_calls + 1)
            if (
                self._paid_calls >= self._max_paid_calls
                or next_reserved > MAX_RESERVED_USDC
            ):
                raise client.BuyerError("Cumulative verification budget exhausted", {})
            self._reserved = next_reserved
            self._paid_calls += 1
        # Never refund or retry: an authorization may have escaped on any failure.
        try:
            return client.run_live(
                self._base_url, normalized, pay=True, max_usdc=self._workflow_cap,
            )
        except client.BuyerError as exc:
            raise client.BuyerError("Paid verification failed; reservation retained", exc.report) from None
        except (RuntimeError, ValueError, TypeError, httpx.HTTPError):
            raise client.BuyerError("Paid verification failed; reservation retained", {
                "reserved_usdc": str(self.reserved_usdc),
                "paid_calls": self.paid_calls,
                "settlement_status": "UNKNOWN",
            }) from None

    def _methods(self):
        methods = [
            ("offline_demo", self.offline_demo, _NoArguments),
            ("inspect_supplier", self.inspect_supplier, _RequestArguments),
        ]
        if self._approval_policy is not None:
            methods.append(("verify_supplier_paid", self.verify_supplier_paid, _RequestArguments))
        return methods

    def langchain_tools(self) -> list:
        """Return real StructuredTools without starting a model or enabling tracing."""
        try:
            from langchain_core.tools import StructuredTool
        except ModuleNotFoundError as exc:
            if exc.name != "langchain_core":
                raise
            raise ImportError("Install supplier-payment-assistant[langchain]") from None

        tools = []
        for name, method, schema in self._methods():
            async def offload(_method=method, **kwargs):
                return await asyncio.to_thread(_method, **kwargs)

            tools.append(StructuredTool.from_function(
                func=method, coroutine=offload, name=name,
                description=method.__doc__, args_schema=schema,
            ))
        return tools

    def openai_tools(self) -> list:
        """Return real Agents FunctionTools; invoke locally without Runner/model."""
        try:
            from agents import function_tool
        except ModuleNotFoundError as exc:
            if exc.name != "agents":
                raise
            raise ImportError("Install supplier-payment-assistant[openai]") from None

        def wrap(name, method, schema):
            # Explicit schemas also reject unexpected fields during local invocation.
            if schema is _NoArguments:
                async def invoke() -> dict:
                    return await asyncio.to_thread(method)
            else:
                async def invoke(unsigned_request_json: str) -> dict:
                    return await asyncio.to_thread(method, unsigned_request_json)
            tool = function_tool(
                invoke, name_override=name, description_override=method.__doc__,
                failure_error_function=None, strict_mode=True,
            )
            original = tool.on_invoke_tool

            async def strict_invoke(ctx, arguments):
                schema.model_validate_json(arguments, strict=True)
                return await original(ctx, arguments)

            tool.on_invoke_tool = strict_invoke
            return tool

        return [wrap(name, method, schema) for name, method, schema in self._methods()]
