"""Public request contract and canonical signing bytes, not the decision engine."""

import json
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Supplier(_Strict):
    cro_number: str = Field(min_length=1, max_length=16)
    vat_number: str = Field(min_length=1, max_length=20)
    iban: str = Field(min_length=1, max_length=40)


class LineItem(_Strict):
    sku: str = Field(min_length=1)
    quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(ge=0)


class Invoice(_Strict):
    po_number: str = Field(min_length=1)
    grn_number: str = Field(min_length=1)
    invoice_number: str = Field(min_length=1)
    amount: Decimal = Field(gt=0)
    currency: Currency
    line_items: tuple[LineItem, ...] = Field(min_length=1)
    payee_name: str = Field(min_length=1, max_length=300)


class PurchaseOrder(_Strict):
    po_number: str = Field(min_length=1)
    supplier_cro_number: str = Field(min_length=1)
    amount: Decimal = Field(gt=0)
    currency: Currency
    line_items: tuple[LineItem, ...] = Field(min_length=1)


class ReceivedLine(_Strict):
    sku: str = Field(min_length=1)
    quantity: Decimal = Field(ge=0)


class GoodsReceipt(_Strict):
    grn_number: str = Field(min_length=1)
    po_number: str = Field(min_length=1)
    supplier_cro_number: str = Field(min_length=1)
    received: tuple[ReceivedLine, ...] = Field(min_length=1)


class SignatureEnvelope(_Strict):
    key_id: str = Field(min_length=1)
    signature_b64: str = Field(min_length=1)
    mtls_cert_fingerprint: str | None = None


class PaymentRequest(_Strict):
    transaction_id: str = Field(min_length=1, max_length=64)
    supplier: Supplier
    invoice: Invoice
    purchase_order: PurchaseOrder
    goods_receipt: GoodsReceipt
    signature: SignatureEnvelope

    def signed_body(self) -> bytes:
        body = self.model_dump(mode="json", exclude={"signature"})
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
