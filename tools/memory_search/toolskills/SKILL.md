# memory_search

Search long-term memory and return structured results grouped by context type (memory/resource/skill) plus a unified ranked view.
MUST CALL: when the answer depends on prior user/project facts not fully present in current turn, including remembered preferences, previous decisions, constraints, unresolved action items, or references like 'as discussed before'/'remember'.
AVOID CALL: for pure single-turn transformations (rewrite/translate/format), general world knowledge, simple greetings, or when current-turn content already contains all required facts.
CALL LIMIT: normally call at most once per user turn; call again only if tool outputs introduce new entities that require a second targeted lookup.
QUERY WRITING: include concrete entities and intent (person/project/date/decision), avoid vague queries like 'help me' or 'that thing'.

## Parameters
- `query`: Concrete lookup query with entities/intent. Avoid vague text.
- `limit`: Max number of unified results.
- `context_type`: Optional: restrict search to one context type.
- `include_l2`: Include L2 preview snippets when available.
- `session`: Optional session key override, e.g. cli:direct.

## Usage
Use `memory_search` only when it is the most direct way to complete the task.
