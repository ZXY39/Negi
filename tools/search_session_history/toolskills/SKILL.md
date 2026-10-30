# search_session_history

Use this when the user asks what was said earlier in this conversation, or when
you need a decision, number, name or preference from earlier turns that is no
longer visible in the current context. It reads the persisted transcript of
the current session, so it keeps working after `token_compression` and after a
restart.

## Inputs

- `keywords`: one to eight literal, case-insensitive terms. Regex syntax is not
  interpreted.
- `match`: `all` (default) requires every keyword in the same message; `any`
  requires at least one.
- `roles`: `user` / `assistant`, both by default.
- `limit` (default 8, max 20), `offset`, `order=recent|oldest` for pagination.
- `snippet_chars` (default 240, max 800): each snippet is centered on the
  matched text.
- `include_internal` (default false): also search heartbeat/cron prompt rows
  and UI-only follow-up archive rows.
- `include_current_turn` (default false): also search the current turn. The
  current user message is already in the transcript before the turn starts.
- `action=open` + `message_index`: read one full message from a hit. Copy
  `expected_turn_id` from the hit as well; if the transcript changed, the open
  fails with `stale_message_index` and the safe action is to search again.

## Output

Search returns `hits` with `message_index`, `turn_id`, `role`, `timestamp`,
`snippet` and `matched_keywords`, plus `total_matches` / `has_more` /
`next_offset`. Open returns the message `content` (capped at 4000 characters,
`truncated` and `remaining_chars` tell you when it was cut) and any stored
`attachments`.

## Boundaries

- Only the current session is searchable. Do not pass a session id or a file
  path, and do not use `content_search` on `sessions/`; the runtime binds the
  session key for you.
- The tool is owner-only. Third-party sessions are rejected even when the tool
  is visible to them.
- Tool-call arguments/results and attachment file contents are not indexed.
  For task tool output, use `content_open` / `content_search` on the task
  artifact instead.
- Matching is a literal substring, not tokenization or semantic search. If no
  hits arrive, try fewer terms, synonyms, or `match=any`.
- Interrupted turns may have no persisted assistant row, and messages removed
  by edit-resend/Fork, session clear or session delete are not searchable.
