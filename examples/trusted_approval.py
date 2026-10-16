"""Policy construction only: importing this module never verifies or pays."""

from supplier_payment_assistant import SupplierPaymentTools


def tools_for_trusted_digests(trusted_invoice_digests: frozenset[str]):
    """Digests must come from authenticated ERP/operator data, never the model."""
    approved_digests = frozenset(trusted_invoice_digests)

    def policy(request):
        return request.canonical_sha256 in approved_digests

    return SupplierPaymentTools(approval_policy=policy)
