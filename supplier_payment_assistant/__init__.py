"""Client-only supplier-verification workflow; never executes invoice payments."""

__all__ = ["BuyerError", "PaymentRequest", "SupplierPaymentTools", "offline_demo", "run_live", "sign_request"]


def __getattr__(name):
    if name == "SupplierPaymentTools":
        from .tools import SupplierPaymentTools

        return SupplierPaymentTools
    if name == "PaymentRequest":
        from .contracts import PaymentRequest

        return PaymentRequest
    if name in ("BuyerError", "offline_demo", "run_live", "sign_request"):
        from . import client

        return getattr(client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
