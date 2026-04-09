from __future__ import annotations


def test_build_query_rewrite_cache_key_depends_only_on_raw_query_and_revision_inputs() -> None:
    from g3ku.runtime.context.frontdoor_query_rewriter import build_query_rewrite_cache_key

    first = build_query_rewrite_cache_key(
        raw_query="find the right browser workflow",
        exposure_revision="exp:rev-1",
        rewrite_prompt_revision="rewrite:prompt:v1",
    )
    second = build_query_rewrite_cache_key(
        raw_query="find the right browser workflow",
        exposure_revision="exp:rev-1",
        rewrite_prompt_revision="rewrite:prompt:v1",
    )
    changed_exposure = build_query_rewrite_cache_key(
        raw_query="find the right browser workflow",
        exposure_revision="exp:rev-2",
        rewrite_prompt_revision="rewrite:prompt:v1",
    )
    changed_raw_query = build_query_rewrite_cache_key(
        raw_query="find the right filesystem workflow",
        exposure_revision="exp:rev-1",
        rewrite_prompt_revision="rewrite:prompt:v1",
    )
    changed_rewrite_prompt = build_query_rewrite_cache_key(
        raw_query="find the right browser workflow",
        exposure_revision="exp:rev-1",
        rewrite_prompt_revision="rewrite:prompt:v2",
    )

    assert first == second
    assert first != changed_exposure
    assert first != changed_raw_query
    assert first != changed_rewrite_prompt
