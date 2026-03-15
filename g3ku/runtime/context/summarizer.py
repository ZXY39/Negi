from __future__ import annotations

import json
import re
from pathlib import Path


def estimate_tokens(text: str) -> int:
    compact = ' '.join(str(text or '').split())
    if not compact:
        return 0
    by_chars = max(1, len(compact) // 4)
    by_words = max(1, int(len(compact.split()) * 1.3))
    return max(by_chars, by_words)


def truncate_by_tokens(text: str, max_tokens: int) -> str:
    value = str(text or '').strip()
    if not value:
        return ''
    if max_tokens <= 0:
        return ''
    budget_chars = max(32, max_tokens * 4)
    if len(value) <= budget_chars:
        return value
    return value[: max(0, budget_chars - 3)].rstrip() + '...'


def _normalize_lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or '').splitlines() if line.strip()]


def _best_heading(text: str) -> str:
    for line in _normalize_lines(text):
        if line.startswith('#'):
            return line.lstrip('#').strip()
    return ''


def _first_sentence(text: str) -> str:
    compact = ' '.join(str(text or '').split())
    if not compact:
        return ''
    parts = re.split(r'(?<=[。！？.!?])\s+', compact)
    return parts[0].strip() if parts else compact[:160].strip()


def summarize_l0(text: str, *, title: str = '', description: str = '', limit: int = 160) -> str:
    candidates = [
        str(title or '').strip(),
        _best_heading(text),
        _first_sentence(description),
        _first_sentence(text),
    ]
    value = next((item for item in candidates if item), '')
    return truncate_by_tokens(value, max(1, limit // 4)) if value else ''


def summarize_l1(text: str, *, title: str = '', description: str = '', limit: int = 640) -> str:
    lines = _normalize_lines(text)
    parts: list[str] = []
    if title:
        parts.append(f'Title: {title.strip()}')
    if description:
        parts.append(f'Summary: {description.strip()}')
    if lines:
        body = []
        for line in lines[:12]:
            cleaned = line.lstrip('#').strip()
            if cleaned:
                body.append(cleaned)
        if body:
            parts.append('Key Points: ' + ' | '.join(body))
    value = '\n'.join(part for part in parts if part).strip()
    return truncate_by_tokens(value, max(1, limit // 4)) if value else ''


def score_query(query: str, *texts: str) -> float:
    raw_query = ' '.join(str(query or '').lower().split())
    if not raw_query:
        return 0.0
    score = 0.0
    terms = [term for term in re.split(r'[^\w\u4e00-\u9fff]+', raw_query) if term]
    haystack = ' '.join(' '.join(str(text or '').lower().split()) for text in texts)
    if not haystack:
        return 0.0
    if raw_query in haystack:
        score += 6.0
    for term in terms:
        if term in haystack:
            score += 1.5 if len(term) > 2 else 0.5
    return score


def window_extract(query: str, text: str, window: int = 3, *, max_chars: int = 1200) -> str:
    value = str(text or '').strip()
    if not value:
        return ''
    if window <= 0:
        return value[:max_chars]
    normalized = ' '.join(value.split())
    sentences = [s.strip() for s in re.split(r'[。！？.!?]+(?:\s+|$)', normalized) if s.strip()]
    if len(sentences) <= 1:
        return value[:max_chars]
    lowered = str(query or '').lower().strip()
    idx = 0
    for i, sent in enumerate(sentences):
        if lowered and lowered in sent.lower():
            idx = i
            break
    lo = max(0, idx - window)
    hi = min(len(sentences), idx + window + 1)
    return '. '.join(sentences[lo:hi])[:max_chars]


def layered_body_payload(
    *,
    body: str,
    level: str = 'l1',
    query: str = '',
    max_tokens: int | None = None,
    title: str = '',
    description: str = '',
    path: str = '',
) -> dict[str, str]:
    normalized_level = str(level or 'l1').strip().lower()
    if normalized_level not in {'l0', 'l1', 'l2'}:
        normalized_level = 'l1'
    l0 = summarize_l0(body, title=title, description=description)
    l1 = summarize_l1(body, title=title, description=description)
    effective_tokens = max(32, int(max_tokens or 300))
    if normalized_level == 'l0':
        content = truncate_by_tokens(l0, effective_tokens)
    elif normalized_level == 'l1':
        content = truncate_by_tokens(l1 or l0, effective_tokens)
    else:
        excerpt = window_extract(query, body, window=3, max_chars=max(200, effective_tokens * 4)) if query else str(body or '')
        content = truncate_by_tokens(excerpt or body, effective_tokens)
    return {
        'level': normalized_level,
        'content': content,
        'l0': l0,
        'l1': l1,
        'path': str(Path(path)) if path else '',
    }


async def summarize_layered_model_first(
    text: str,
    *,
    title: str = '',
    description: str = '',
    model_key: str | None = None,
    l0_limit: int = 160,
    l1_limit: int = 640,
) -> tuple[str, str]:
    heuristic_l0 = summarize_l0(text, title=title, description=description, limit=l0_limit)
    heuristic_l1 = summarize_l1(text, title=title, description=description, limit=l1_limit)
    source_text = str(text or '').strip()
    if not source_text:
        return heuristic_l0, heuristic_l1

    try:
        from g3ku.config.live_runtime import get_runtime_config
        from g3ku.providers.chatmodels import build_chat_model

        config, _revision, _changed = get_runtime_config(force=False)
        explicit_model_key = str(model_key or '').strip()
        if explicit_model_key:
            model = build_chat_model(config, model_key=explicit_model_key)
        else:
            model = build_chat_model(config, role='ceo')
        prompt = (
            'You are generating layered retrieval context for an internal agent catalog.\n'
            'Return strict JSON with keys l0 and l1 only.\n'
            f'l0 must be <= {l0_limit} chars.\n'
            f'l1 must be <= {l1_limit} chars.\n'
            'l0 = one-line semantic summary.\n'
            'l1 = short structured overview focused on usage and scope.\n'
        )
        body = json.dumps(
            {
                'title': str(title or ''),
                'description': str(description or ''),
                'content': truncate_by_tokens(source_text, max(512, l1_limit * 2 // 4)),
            },
            ensure_ascii=False,
        )
        response = await model.ainvoke(
            [
                {'role': 'system', 'content': prompt},
                {'role': 'user', 'content': body},
            ]
        )
        raw = getattr(response, 'content', response)
        if isinstance(raw, list):
            parts: list[str] = []
            for item in raw:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text_part = item.get('text') or item.get('content') or ''
                    if isinstance(text_part, str):
                        parts.append(text_part)
            raw = '\n'.join(parts)
        text_out = str(raw or '').strip()
        parsed = json.loads(text_out)
        if not isinstance(parsed, dict):
            return heuristic_l0, heuristic_l1
        l0 = truncate_by_tokens(str(parsed.get('l0') or heuristic_l0), max(1, l0_limit // 4))
        l1 = truncate_by_tokens(str(parsed.get('l1') or heuristic_l1), max(1, l1_limit // 4))
        return l0 or heuristic_l0, l1 or heuristic_l1
    except Exception:
        return heuristic_l0, heuristic_l1
