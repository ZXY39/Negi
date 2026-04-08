from g3ku.runtime.frontdoor.state_models import initial_persistent_state


def test_initial_persistent_state_tracks_frontdoor_stage_state() -> None:
    state = initial_persistent_state(user_input={"content": "hello", "metadata": {}})

    assert state["route_kind"] == "direct_reply"
    assert state["frontdoor_stage_state"] == {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [],
    }


def test_initial_persistent_state_tracks_compression_state() -> None:
    state = initial_persistent_state(user_input={"content": "hello", "metadata": {}})

    assert state["compression_state"] == {
        "status": "",
        "text": "",
        "source": "",
        "needs_recheck": False,
    }
