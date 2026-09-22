"""VietQR payload (EMVCo QR Code Specification for Payment Systems, Merchant-Presented Mode,
with the NAPAS VietQR merchant account template).

Every data object is ``ID (2 digits) + length (2 digits) + value``. The payload built here:

====  =================================  ====================================================
ID    Meaning                            Value
====  =================================  ====================================================
00    Payload format indicator           ``01``
01    Point of initiation method         ``12`` (dynamic: the amount is fixed)
38    Merchant account information       ``00`` GUID ``A000000727`` (NAPAS),
      (NAPAS template)                   ``01`` beneficiary: ``00`` bank BIN, ``01`` account,
                                         ``02`` service code ``QRIBFTTA`` (transfer to account)
53    Transaction currency               ``704`` (VND, ISO 4217)
54    Transaction amount                 whole dong, digits only
58    Country code                       ``VN``
62    Additional data field              ``08`` purpose of transaction = payment reference
63    CRC                                CRC-16/CCITT-FALSE over everything up to and
                                         including ``6304``, 4 upper-case hex digits
====  =================================  ====================================================
"""

from __future__ import annotations

NAPAS_GUID = "A000000727"
SERVICE_TO_ACCOUNT = "QRIBFTTA"
CURRENCY_VND = "704"
COUNTRY_VN = "VN"
_MAX_VALUE_LENGTH = 99


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: polynomial 0x1021, initial value 0xFFFF, no reflection, no xor-out."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


def tlv(tag: str, value: str) -> str:
    """One data object; ``value`` must be printable ASCII of at most 99 characters."""
    if len(tag) != 2 or not tag.isdigit():
        raise ValueError(f"tag {tag!r} must be two digits")
    if not value or len(value) > _MAX_VALUE_LENGTH:
        raise ValueError(f"value of tag {tag} must be 1 to {_MAX_VALUE_LENGTH} characters")
    if not value.isascii() or not value.isprintable():
        raise ValueError(f"value of tag {tag} must be printable ASCII")
    return f"{tag}{len(value):02d}{value}"


def build_vietqr(
    *, bank_bin: str, account_number: str, amount_vnd: int, payment_reference: str
) -> str:
    """The QR string a banking app scans to prefill beneficiary, amount and memo."""
    if not bank_bin.isascii() or not bank_bin.isdigit():
        raise ValueError("bank BIN must be ASCII digits")
    if isinstance(amount_vnd, bool) or not isinstance(amount_vnd, int) or amount_vnd <= 0:
        raise ValueError("amount must be a positive whole number of dong")
    beneficiary = tlv("00", bank_bin) + tlv("01", account_number)
    merchant_account = (
        tlv("00", NAPAS_GUID) + tlv("01", beneficiary) + tlv("02", SERVICE_TO_ACCOUNT)
    )
    body = (
        tlv("00", "01")
        + tlv("01", "12")
        + tlv("38", merchant_account)
        + tlv("53", CURRENCY_VND)
        + tlv("54", str(amount_vnd))
        + tlv("58", COUNTRY_VN)
        + tlv("62", tlv("08", payment_reference))
        + "6304"
    )
    return f"{body}{crc16_ccitt_false(body.encode('ascii')):04X}"
