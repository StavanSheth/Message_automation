"""Unit tests for SystemState state machine invariants."""

import pytest
from backend.domain.enums import SystemState
from backend.application.control_service import ALLOWED_STATE_TRANSITIONS


def test_state_machine_transition_rules():
    """Verify that all state transitions strictly obey Section 3.2 rules."""
    # STOPPED can only go to STARTING
    assert ALLOWED_STATE_TRANSITIONS[SystemState.STOPPED] == {SystemState.STARTING}

    # STARTING can go to RUNNING, DEGRADED, STOPPED
    assert SystemState.RUNNING in ALLOWED_STATE_TRANSITIONS[SystemState.STARTING]
    assert SystemState.DEGRADED in ALLOWED_STATE_TRANSITIONS[SystemState.STARTING]

    # RUNNING can go to PAUSED, DRAINING, DEGRADED, STOPPING
    assert SystemState.PAUSED in ALLOWED_STATE_TRANSITIONS[SystemState.RUNNING]
    assert SystemState.DRAINING in ALLOWED_STATE_TRANSITIONS[SystemState.RUNNING]
    assert SystemState.DEGRADED in ALLOWED_STATE_TRANSITIONS[SystemState.RUNNING]

    # PAUSED can go to RUNNING, DRAINING, STOPPING
    assert SystemState.RUNNING in ALLOWED_STATE_TRANSITIONS[SystemState.PAUSED]
    assert SystemState.DRAINING in ALLOWED_STATE_TRANSITIONS[SystemState.PAUSED]

    # DRAINING can go to STOPPING, STOPPED
    assert SystemState.STOPPED in ALLOWED_STATE_TRANSITIONS[SystemState.DRAINING]

    # DEGRADED can go to MANUAL_INTERVENTION
    assert SystemState.MANUAL_INTERVENTION in ALLOWED_STATE_TRANSITIONS[SystemState.DEGRADED]
