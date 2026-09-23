"""VietQR payload vectors, built by hand from the EMVCo MPM and NAPAS VietQR layout.

The CRC is cross-checked with the standard library's independent CRC-CCITT implementation
(``binascii.crc_hqx`` with initial value 0xFFFF is CRC-16/CCITT-FALSE).
"""

from __future__ import annotations

import binascii
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from payment_module.adapters.sepay.provider import SePayProvider
from payment_module.adapters.sepay.vietqr import build_vietqr, crc16_ccitt_false
from payment_module.domain.enums import Environment, IntentStatus, ReceivingAccountStatus
from payment_module.domain.intent import IntentView
from payment_module.domain.money import AmountVnd
from payment_module.ports.provider import ReceivingAccountView

pytestmark = pytest.mark.contract

# Vector 1: BIN 970436, account 1017588888, 150000 dong, reference SUBK7M2QX
#   00 02 01                      payload format
#   01 02 12                      dynamic
#   38 54 ...                     merchant account (NAPAS)
#         00 10 A000000727        GUID
#         01 24 ...               beneficiary
#               00 06 970436      BIN
#               01 10 1017588888  account
#         02 08 QRIBFTTA          transfer to account
#   53 03 704                     VND
#   54 06 150000                  amount
#   58 02 VN                      country
#   62 13 ...                     additional data
#         08 09 SUBK7M2QX         purpose = payment reference
#   63 04 EE40                    CRC
VECTOR_1 = (
    "000201"
    "010212"
    "38540010A00000072701240006970436011010175888880208QRIBFTTA"
    "5303704"
    "5406150000"
    "5802VN"
    "62130809SUBK7M2QX"
    "6304EE40"
)
# Vector 2: BIN 970422, account 0123456789012 (13 digits), 5000 dong, reference TOP2A9C
#   38 57: 00 10 A000000727 | 01 27 (00 06 970422, 01 13 0123456789012) | 02 08 QRIBFTTA
#   54 04 5000; 62 11: 08 07 TOP2A9C; 63 04 D7D4
VECTOR_2 = (
    "000201"
    "010212"
    "38570010A00000072701270006970422011301234567890120208QRIBFTTA"
    "5303704"
    "54045000"
    "5802VN"
    "62110807TOP2A9C"
    "6304D7D4"
)


def parse_tlv(payload: str) -> dict[str, str]:
    """Split a payload into ``{tag: value}``, failing on any length mismatch."""
    fields: dict[str, str] = {}
    index = 0
    while index < len(payload):
        tag, length = payload[index : index + 2], int(payload[index + 2 : index + 4])
        value = payload[index + 4 : index + 4 + length]
        assert len(value) == length, f"truncated value for tag {tag}"
        assert tag not in fields, f"duplicate tag {tag}"
        fields[tag] = value
        index += 4 + length
    return fields


def test_crc_known_answer() -> None:
    assert crc16_ccitt_false(b"123456789") == 0x29B1


@pytest.mark.parametrize("data", [b"", b"123456789", VECTOR_1[:-4].encode(), b"\x00\xff" * 50])
def test_crc_matches_an_independent_implementation(data: bytes) -> None:
    assert crc16_ccitt_false(data) == binascii.crc_hqx(data, 0xFFFF)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("970436", "1017588888", 150_000, "SUBK7M2QX"), VECTOR_1),
        (("970422", "0123456789012", 5_000, "TOP2A9C"), VECTOR_2),
    ],
)
def test_vectors(args: tuple[str, str, int, str], expected: str) -> None:
    bank_bin, account, amount, reference = args
    payload = build_vietqr(
        bank_bin=bank_bin, account_number=account, amount_vnd=amount, payment_reference=reference
    )
    assert payload == expected

    fields = parse_tlv(payload)
    assert list(fields) == ["00", "01", "38", "53", "54", "58", "62", "63"]
    assert fields["00"] == "01"
    assert fields["01"] == "12"
    merchant = parse_tlv(fields["38"])
    assert merchant["00"] == "A000000727"
    assert merchant["02"] == "QRIBFTTA"
    assert parse_tlv(merchant["01"]) == {"00": bank_bin, "01": account}
    assert fields["53"] == "704"
    assert fields["54"] == str(amount)
    assert fields["58"] == "VN"
    assert parse_tlv(fields["62"]) == {"08": reference}
    assert fields["63"] == f"{binascii.crc_hqx(payload[:-4].encode(), 0xFFFF):04X}"


@pytest.mark.parametrize(
    "change",
    [
        {"bank_bin": "97O436"},
        {"amount_vnd": 0},
        {"amount_vnd": True},
        {"account_number": ""},
        {"account_number": "1" * 100},
        {"payment_reference": "SUBÉ"},
    ],
)
def test_invalid_input_is_rejected(change: dict[str, object]) -> None:
    args: dict[str, object] = {
        "bank_bin": "970436",
        "account_number": "1017588888",
        "amount_vnd": 150_000,
        "payment_reference": "SUBK7M2QX",
    } | change
    with pytest.raises(ValueError):
        build_vietqr(**args)  # type: ignore[arg-type]


def account(bank_bin: str | None) -> ReceivingAccountView:
    return ReceivingAccountView(
        id=uuid4(),
        tenant_id="t",
        merchant_id=uuid4(),
        environment=Environment.TEST,
        bank_code="VCB",
        account_number="1017588888",
        sub_account=None,
        account_name="CONG TY A",
        status=ReceivingAccountStatus.ACTIVE,
        bank_bin=bank_bin,
    )


def intent() -> IntentView:
    return IntentView(
        id=uuid4(),
        tenant_id="t",
        merchant_id=uuid4(),
        environment=Environment.TEST,
        receiving_account_id=uuid4(),
        amount=AmountVnd(150_000),
        status=IntentStatus.AWAITING_PAYMENT,
        payment_reference="SUBK7M2QX",
        expires_at=datetime(2026, 9, 22, tzinfo=UTC),
        host_ref_type="order",
        host_ref_id="o-1",
    )


def test_instruction_carries_the_qr_when_the_bin_is_known() -> None:
    instruction = SePayProvider().build_instruction(intent(), account("970436"))
    assert instruction.qr_payload == VECTOR_1
    assert instruction.payment_reference == "SUBK7M2QX"
    assert instruction.amount == AmountVnd(150_000)
    assert (instruction.bank_code, instruction.account_number) == ("VCB", "1017588888")
    assert instruction.account_name == "CONG TY A"


def test_instruction_has_no_qr_without_a_bin() -> None:
    assert SePayProvider().build_instruction(intent(), account(None)).qr_payload is None
