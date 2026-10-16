"""Synthetic fixtures only; no customer invoices or verified supplier identities."""

_LINE = [{"sku": "EXAMPLE-ITEM", "quantity": "1", "unit_price": "1000.00"}]

DECISION_EXAMPLE = {
    "transaction_id": "SYNTHETIC-TRANSACTION-1",
    "supplier": {"cro_number": "000000", "vat_number": "IE0000000A", "iban": "IE29AIBK93115212345678"},
    "invoice": {
        "po_number": "EXAMPLE-PO-1", "grn_number": "EXAMPLE-GRN-1", "invoice_number": "EXAMPLE-INVOICE-1",
        "amount": "1000.00", "currency": "EUR", "payee_name": "SYNTHETIC EXAMPLE SUPPLIER - NOT VERIFIED",
        "line_items": _LINE,
    },
    "purchase_order": {
        "po_number": "EXAMPLE-PO-1", "supplier_cro_number": "000000",
        "amount": "1000.00", "currency": "EUR", "line_items": _LINE,
    },
    "goods_receipt": {
        "grn_number": "EXAMPLE-GRN-1", "po_number": "EXAMPLE-PO-1", "supplier_cro_number": "000000",
        "received": [{"sku": "EXAMPLE-ITEM", "quantity": "1"}],
    },
    "signature": {"key_id": "synthetic-placeholder", "signature_b64": "NOT-A-REAL-SIGNATURE"},
}

DECISION_RESULT_EXAMPLE = {
    "agent_id": "SYNTHETIC-NOT-A-LIVE-VERIFICATION",
    "timestamp": "2026-10-16T00:00:00Z",
    "transaction_id": DECISION_EXAMPLE["transaction_id"],
    "supplier": DECISION_EXAMPLE["supplier"],
    "verification_results": {
        name: "UNAVAILABLE" for name in (
            "CRO", "VIES_VAT", "CONFIRMATION_OF_PAYEE", "ERP_THREE_WAY_MATCH", "A2A_SIGNATURE",
        )
    },
    "decision": "FLAG_FOR_HUMAN_REVIEW",
    "confidence_score": 0.0,
    "execution_payload": None,
    "reason_codes": ["SYNTHETIC_EXAMPLE_NOT_VERIFIED"],
    "vies_consultation_number": None,
    "vies_audit_receipt": None,
}
