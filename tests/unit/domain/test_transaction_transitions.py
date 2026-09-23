from __future__ import annotations

import itertools

import pytest

from payment_module.domain.enums import MatchState
from payment_module.domain.errors import IllegalTransition
from payment_module.domain.transaction import transition_match_state

M = MatchState
ALLOWED = {
    (M.RECORDED, M.SETTLED),
    (M.RECORDED, M.IN_REVIEW),
    (M.RECORDED, M.NOT_APPLICABLE),
    (M.IN_REVIEW, M.SETTLED),
    (M.IN_REVIEW, M.CLOSED_EXTERNAL),
    (M.IN_REVIEW, M.DUPLICATE_OF),
    # A rematch that still needs review keeps the state.
    (M.IN_REVIEW, M.IN_REVIEW),
}
ALL_PAIRS = list(itertools.product(MatchState, MatchState))


@pytest.mark.parametrize(("current", "target"), sorted(ALLOWED))
def test_allowed_match_transitions(current: MatchState, target: MatchState) -> None:
    assert transition_match_state(current, target) is target


@pytest.mark.parametrize(("current", "target"), [pair for pair in ALL_PAIRS if pair not in ALLOWED])
def test_every_other_match_transition_is_illegal(current: MatchState, target: MatchState) -> None:
    with pytest.raises(IllegalTransition):
        transition_match_state(current, target)


def test_settled_cannot_go_back_to_review() -> None:
    with pytest.raises(IllegalTransition) as caught:
        transition_match_state(M.SETTLED, M.IN_REVIEW)
    assert (caught.value.current, caught.value.target) == ("settled", "in_review")
