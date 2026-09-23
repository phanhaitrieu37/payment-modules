from __future__ import annotations

from uuid import uuid4

import pytest

from payment_module.domain.enums import Environment
from payment_module.domain.matching.reference_resolver import (
    ReferenceResolver,
    ResolutionKind,
    ScopeMismatch,
)
from payment_module.domain.reference import tokens_from

REFERENCE = "SUBK7Q2M9"


def test_no_matching_token(make_intent) -> None:
    intent = make_intent()
    result = ReferenceResolver().resolve(["HELLO"], {REFERENCE: intent})
    assert result.kind is ResolutionKind.NONE and result.intent is None


def test_unique_whole_token_match(make_intent) -> None:
    intent = make_intent()
    tokens = tokens_from(None, f"ck {REFERENCE.lower()} cam on")
    result = ReferenceResolver().resolve(tokens, {REFERENCE: intent})
    assert result.kind is ResolutionKind.UNIQUE and result.intent == intent


def test_same_intent_in_code_and_content_is_unique(make_intent) -> None:
    intent = make_intent()
    tokens = tokens_from(REFERENCE, f"memo {REFERENCE}")
    assert ReferenceResolver().resolve(tokens, {REFERENCE: intent}).kind is ResolutionKind.UNIQUE


def test_code_and_content_pointing_at_two_intents_is_ambiguous(make_intent) -> None:
    first = make_intent()
    second = make_intent(id=uuid4(), payment_reference="TOPA1B2C3")
    tokens = tokens_from(REFERENCE, "TOPA1B2C3")
    result = ReferenceResolver().resolve(tokens, {REFERENCE: first, "TOPA1B2C3": second})
    assert result.kind is ResolutionKind.AMBIGUOUS and result.intent is None


def test_same_prefix_other_reference_does_not_match(make_intent) -> None:
    # A prefix is not authentication: sharing "SUB" never selects an intent.
    intent = make_intent()
    tokens = tokens_from(None, "SUBK7Q2M8 SUB SUBK7Q2M9X")
    assert ReferenceResolver().resolve(tokens, {REFERENCE: intent}).kind is ResolutionKind.NONE


def test_candidates_must_be_keyed_by_their_reference(make_intent) -> None:
    with pytest.raises(ValueError):
        ReferenceResolver().resolve(["OTHER"], {"OTHER": make_intent()})


def test_scope_matches(make_tx, make_intent) -> None:
    assert ReferenceResolver().scope_mismatch(make_tx(), make_intent()) is None


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"tenant_id": "tenant-b"}, ScopeMismatch.TENANT),
        ({"environment": Environment.TEST}, ScopeMismatch.ENVIRONMENT),
        ({"receiving_account_id": uuid4()}, ScopeMismatch.RECEIVING_ACCOUNT),
        (
            {"merchant_id": uuid4(), "receiving_account_id": uuid4()},
            ScopeMismatch.RECEIVING_ACCOUNT,
        ),
    ],
)
def test_scope_mismatch_kinds(make_tx, make_intent, changes, expected) -> None:
    assert ReferenceResolver().scope_mismatch(make_tx(), make_intent(**changes)) is expected
