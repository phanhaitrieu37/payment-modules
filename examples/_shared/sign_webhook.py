"""A SePay delivery signed the way SePay signs it, for the smokes of both example hosts.

Acceptance case "Reuse" (validation-and-acceptance.md). The HMAC comes from the installed
wheel, so the smokes check the package's own signature scheme.
"""

from __future__ import annotations

import json
import random
import time

from payment_module.adapters.sepay.provider import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign


def signed_delivery(
    secret: str, account_number: str, amount_vnd: int, code: str | None, content: str
) -> tuple[bytes, dict[str, str]]:
    """``(raw_body, headers)`` of an incoming Vietcombank transfer."""
    raw = json.dumps(
        {
            "id": random.randrange(10**8, 10**9),
            "gateway": "VCB",
            "transactionDate": time.strftime("%Y-%m-%d %H:%M:%S"),
            "accountNumber": account_number,
            "subAccount": None,
            "code": code,
            "content": content,
            "transferType": "in",
            "transferAmount": amount_vnd,
            "referenceCode": f"FT{random.randrange(10**10, 10**11)}",
        }
    ).encode()
    timestamp = int(time.time())
    headers = {
        "content-type": "application/json",
        TIMESTAMP_HEADER: str(timestamp),
        SIGNATURE_HEADER: sign(raw, secret, timestamp),
    }
    return raw, headers
