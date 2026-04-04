from g3ku.config.schema import MemoryAssemblyConfig
from g3ku.runtime.frontdoor.state_models import initial_persistent_state


def test_memory_assembly_config_exposes_create_agent_and_summarizer_defaults() -> None:
    cfg = MemoryAssemblyConfig()

    assert cfg.frontdoor_create_agent_enabled is False
    assert cfg.frontdoor_create_agent_shadow_mode is False
    assert cfg.frontdoor_summarizer_enabled is True
    assert cfg.frontdoor_summarizer_model_key is None
    assert cfg.frontdoor_summarizer_trigger_message_count == 24
    assert cfg.frontdoor_summarizer_keep_message_count == 8


def test_initial_persistent_state_contains_summary_payload_and_runtime_marker() -> None:
    state = initial_persistent_state(user_input={"content": "hello", "metadata": {}})

    assert state["summary_text"] == ""
    assert state["summary_payload"] == {}
    assert state["summary_version"] == 0
    assert state["summary_model_key"] == ""
    assert state["agent_runtime"] == "langgraph"
