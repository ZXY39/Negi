from __future__ import annotations


def _field(value, name: str):
    if isinstance(value, dict):
        return value[name]
    return getattr(value, name)


def test_frontdoor_prompt_cache_key_ignores_dynamic_appendix_changes_when_stable_messages_match() -> None:
    from g3ku.runtime.frontdoor.prompt_cache_contract import build_frontdoor_prompt_contract

    stable_messages = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "原始用户问题"},
    ]
    tool_schemas = [
        {
            "name": "memory_write",
            "description": "",
            "parameters": {"type": "object"},
        }
    ]

    first = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=stable_messages,
        dynamic_appendix_messages=[
            {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory A"}
        ],
        tool_schemas=tool_schemas,
        cache_family_revision="exp:rev-1",
    )
    second = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=stable_messages,
        dynamic_appendix_messages=[
            {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory B"}
        ],
        tool_schemas=tool_schemas,
        cache_family_revision="exp:rev-1",
    )

    assert _field(first, "prompt_cache_key") == _field(second, "prompt_cache_key")
    assert _field(first, "stable_prefix_hash") == _field(second, "stable_prefix_hash")
    assert _field(first, "dynamic_appendix_hash") != _field(second, "dynamic_appendix_hash")
    assert _field(first, "request_messages") != _field(second, "request_messages")


def test_frontdoor_prompt_cache_key_changes_when_capability_revision_changes() -> None:
    from g3ku.runtime.frontdoor.prompt_cache_contract import build_frontdoor_prompt_contract

    stable_messages = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "原始用户问题"},
    ]
    tool_schemas = [
        {
            "name": "memory_write",
            "description": "",
            "parameters": {"type": "object"},
        }
    ]

    first = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=stable_messages,
        dynamic_appendix_messages=[
            {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory A"}
        ],
        tool_schemas=tool_schemas,
        cache_family_revision="exp:rev-1",
    )
    second = build_frontdoor_prompt_contract(
        scope="ceo_frontdoor",
        provider_model="openai:gpt-4.1",
        stable_messages=stable_messages,
        dynamic_appendix_messages=[
            {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory A"}
        ],
        tool_schemas=tool_schemas,
        cache_family_revision="exp:rev-2",
    )

    assert _field(first, "prompt_cache_key") != _field(second, "prompt_cache_key")
