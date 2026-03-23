---
name: ddg-search
description: Web search without an API key using DuckDuckGo Lite via web_fetch. Only use this skill in runtimes where `web_fetch` is actually visible.
---

# DuckDuckGo Search via web_fetch

Search the web using DuckDuckGo Lite's HTML interface, parsed via `web_fetch`. No API key or package install required.

If the current workspace/runtime does not expose `web_fetch`, do not use this skill.

## How to Search

```
web_fetch(url="https://lite.duckduckgo.com/lite/?q=QUERY", max_chars=8000)
```

- URL-encode the query — use `+` for spaces
- `web_fetch` in this workspace does not support `extractMode`; it already returns extracted readable text by default
- Increase `max_chars` for more results

## Region Filtering

Append `&kl=REGION` for regional results:

- `au-en` — Australia
- `us-en` — United States
- `uk-en` — United Kingdom
- `de-de` — Germany
- `fr-fr` — France

Full list: https://duckduckgo.com/params

### Example — Australian search

```
web_fetch(url="https://lite.duckduckgo.com/lite/?q=best+coffee+melbourne&kl=au-en", max_chars=8000)
```

## Reading Results

Results appear as numbered items with title, snippet, and URL. Skip entries marked "Sponsored link" (ads) — organic results follow.

## Search-then-Fetch Pattern

1. **Search** — query DDG Lite for a list of results
2. **Pick** — identify the most relevant URLs
3. **Fetch** — use `web_fetch` on those URLs to read full content

## Tips

- First 1-2 results may be ads — skip to organic results
- For exact phrases, wrap in quotes: `q=%22exact+phrase%22`
- Add specific terms to narrow results (site name, year, location)

## Limitations

- No time/date filtering (DDG Lite doesn't support `&df=` reliably via fetch)
- Text results only — no images or videos
- Results sourced from Bing (may differ from Google)
- Google search does NOT work via web_fetch (captcha blocked)
