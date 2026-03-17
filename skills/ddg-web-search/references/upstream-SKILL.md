---
name: ddg-search
description: Web search without an API key using DuckDuckGo Lite via web_fetch. Use as a fallback when web_search fails with missing_brave_api_key error, or whenever you need to search the web and no search API is configured.
---

# ddg-search

Use DuckDuckGo Lite with `web_fetch` to search the web without any API key.

This is useful when:
- `web_search` fails with `missing_brave_api_key`
- no search API is configured
- you still need general web results via plain HTTP fetch

## Search URL format

Construct a DDG Lite URL:

```text
https://lite.duckduckgo.com/lite/?q=<URL-ENCODED QUERY>
```

Examples:
- `https://lite.duckduckgo.com/lite/?q=site%3Agithub.com%20openai%20codex`
- `https://lite.duckduckgo.com/lite/?q=latest%20python%203.13%20release`

Optional params:
- `kl=us-en` → region/language
- `s=30` → pagination offset

## How to use with web_fetch

1. URL-encode the search query.
2. Open the DDG Lite URL with `web_fetch`.
3. Extract result titles, links, and snippets from the returned HTML.
4. Summarize the top results.

## Suggested prompt pattern

Use this whenever search is needed but `web_search` is unavailable:

> Search the web for: <QUERY>
> If `web_search` is unavailable or missing an API key, use DuckDuckGo Lite via `web_fetch` instead.
> Return the top 5 relevant results with title, URL, and a one-line summary each.

## Notes

- DDG Lite returns HTML that is much easier to parse than modern JS-heavy search pages.
- Some links may be redirect URLs; follow them if necessary.
- If DDG blocks or rate-limits, try a narrower query or retry later.

## Limitations

- No time/date filtering (DDG Lite doesn't support `&df=` reliably via fetch)
- Text results only — no images or videos
- Results sourced from Bing (may differ from Google)
- Google search does NOT work via `web_fetch` (captcha / anti-bot)
