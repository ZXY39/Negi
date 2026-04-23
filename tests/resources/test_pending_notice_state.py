from main.runtime.pending_notice_state import (
    PENDING_NOTICE_STATE_KEY,
    RESUME_MODE_ORDINARY,
    RESUME_MODE_WAIT_FOR_CHILDREN,
    clear_pending_notice_state,
    normalize_pending_notice_state,
    set_pending_notice_state,
)


def test_pending_notice_state_key_contract() -> None:
    assert PENDING_NOTICE_STATE_KEY == "pending_notice_state"


def test_normalize_pending_notice_state_defaults_to_ordinary() -> None:
    assert normalize_pending_notice_state(None) == {
        "resume_mode": RESUME_MODE_ORDINARY,
        "epoch_id": "",
        "holding_round_id": "",
        "updated_at": "",
    }


def test_normalize_pending_notice_state_coerces_unknown_mode_to_ordinary() -> None:
    assert normalize_pending_notice_state(
        {
            "resume_mode": "unexpected-mode",
            "epoch_id": "epoch:1",
            "holding_round_id": "round-1",
            "updated_at": "2026-04-24T00:00:00+08:00",
        }
    ) == {
        "resume_mode": RESUME_MODE_ORDINARY,
        "epoch_id": "epoch:1",
        "holding_round_id": "round-1",
        "updated_at": "2026-04-24T00:00:00+08:00",
    }


def test_set_pending_notice_state_overwrites_mode_and_round_id() -> None:
    current = {
        "resume_mode": RESUME_MODE_ORDINARY,
        "epoch_id": "epoch:old",
        "holding_round_id": "",
        "updated_at": "2026-04-24T00:00:00+08:00",
    }

    updated = set_pending_notice_state(
        current,
        resume_mode=RESUME_MODE_WAIT_FOR_CHILDREN,
        epoch_id="epoch:new",
        holding_round_id="round-1",
        updated_at="2026-04-24T01:00:00+08:00",
    )

    assert updated == {
        "resume_mode": RESUME_MODE_WAIT_FOR_CHILDREN,
        "epoch_id": "epoch:new",
        "holding_round_id": "round-1",
        "updated_at": "2026-04-24T01:00:00+08:00",
    }


def test_set_pending_notice_state_coerces_unknown_mode_to_ordinary() -> None:
    updated = set_pending_notice_state(
        None,
        resume_mode="not-a-real-mode",
        epoch_id="epoch:new",
        holding_round_id="round-1",
        updated_at="2026-04-24T01:00:00+08:00",
    )

    assert updated == {
        "resume_mode": RESUME_MODE_ORDINARY,
        "epoch_id": "epoch:new",
        "holding_round_id": "round-1",
        "updated_at": "2026-04-24T01:00:00+08:00",
    }


def test_clear_pending_notice_state_resets_to_default_shape() -> None:
    updated = clear_pending_notice_state(
        {
            "resume_mode": RESUME_MODE_WAIT_FOR_CHILDREN,
            "epoch_id": "epoch:new",
            "holding_round_id": "round-1",
            "updated_at": "2026-04-24T01:00:00+08:00",
        }
    )

    assert updated == {
        "resume_mode": RESUME_MODE_ORDINARY,
        "epoch_id": "",
        "holding_round_id": "",
        "updated_at": "",
    }
