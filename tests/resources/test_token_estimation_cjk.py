from __future__ import annotations

from g3ku.runtime.context.summarizer import estimate_tokens, truncate_by_tokens, _count_cjk


def test_estimate_tokens_ascii_unchanged() -> None:
    # 纯 ASCII 行为与旧实现一致（~4 字符/token），不引入回归。
    assert estimate_tokens("a" * 400) == 100
    assert estimate_tokens("") == 0
    assert estimate_tokens("   ") == 0
    # 英文按词估算与按字符估算取大者
    text = "hello world foo bar " * 25  # 100 词、~500 字符
    assert estimate_tokens(text) >= 100


def test_estimate_tokens_cjk_higher_than_naive_quarter() -> None:
    # 中文按 ~1 字符/token，远高于旧的 len//4（旧实现严重低估、压缩闸门晚触发）。
    text = "中" * 100
    assert _count_cjk(text) == 100
    assert estimate_tokens(text) == 100  # 新：~1 token/字
    # 旧实现会是 100 // 4 = 25，新估算应显著更高
    assert estimate_tokens(text) > 100 // 4


def test_estimate_tokens_mixed_cjk_ascii() -> None:
    # 混合文本：CJK 部分按 1 字符/token，ASCII 部分按 4 字符/token。
    text = "中文内容" * 10 + "abcd" * 100  # 40 CJK + 400 ASCII
    est = estimate_tokens(text)
    # ≈ 40/1.0 + 400/4 = 140（紧凑后无空格）；给一个合理区间
    assert 120 <= est <= 170


def test_truncate_by_tokens_cjk_not_under_truncated() -> None:
    # 旧实现按 max_tokens*4 字符放行，对中文等于 ~4× 预算（截断不足）。
    text = "中" * 1000
    out = truncate_by_tokens(text, 100)
    # 截断后应接近 100 token（≈100 中文字符 + 省略号），而非旧实现的 ~400 字符
    assert len(out) <= 110
    assert out.endswith("...")
    # 未超预算时原样返回
    assert truncate_by_tokens("中" * 50, 100) == "中" * 50
    assert truncate_by_tokens("", 100) == ""
    assert truncate_by_tokens("abc", 0) == ""


def test_truncate_by_tokens_ascii_backward_compatible() -> None:
    # 纯 ASCII 的截断阈值与旧实现（max_tokens*4 字符）一致。
    text = "a" * 5000
    out = truncate_by_tokens(text, 100)
    # estimate=1250 > 100 → ratio=0.08 → budget=400 字符（与旧 max_tokens*4 一致）
    assert len(out) <= 401
    assert out.endswith("...")
