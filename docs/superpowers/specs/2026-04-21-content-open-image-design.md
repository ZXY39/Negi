# Content Open Image Multimodal Reopen Design

**Date:** 2026-04-21

**Status:** Draft for review

## Goal

Extend `content_open` so that agents can reopen an image from a retained `path` or `ref` and have the image's visual content attached directly to the next model request, without introducing a new `image_open` tool family.

The design must preserve the existing product behavior that ordinary file paths can remain in context across turns, while adding an explicit and controllable way for CEO/frontdoor, execution nodes, and acceptance nodes to re-read historical images with multimodal models.

## Scope

This design applies to:

- `content_open` and the shared `content(action=open)` implementation
- CEO/frontdoor runtime
- execution node runtime
- acceptance node runtime
- model capability gating for multimodal image access
- token preflight and token compression behavior for image-open follow-up sends
- prompt guidance for CEO, execution, and acceptance agents
- regression coverage and architecture docs for the new contract

This design does not change:

- the upload UI or attachment bubble rendering behavior
- the current-turn direct upload multimodal lane
- the `content_search` or `content_describe` contracts
- the `content_open` parameter schema shape
- prompt-cache family semantics beyond what is already implied by a larger live request

## Current Problem

Today, uploaded images can be attached directly to a current-turn CEO/frontdoor request when the selected binding has `image_multimodal_enabled=true`. That path is intentionally live-only and strips image blocks from durable baselines after the send.

That behavior is correct for current-turn uploads, but it leaves a gap for later turns:

- the image file path may still exist on disk and may still appear in ordinary context text
- the model can see or mention the path
- but there is no explicit tool contract that says "open this historical image and attach its pixels to the next request"

As a result, images do not behave like other durable file references. The path can remain visible, but the model cannot reliably convert that path back into direct visual access in a later turn.

## Desired End State

After rollout:

- `content_open` remains the only explicit image reopen entrypoint.
- Historical image paths can remain in ordinary context exactly as other file paths do.
- If an agent wants to directly inspect the image contents, it must call `content_open`.
- If the current model is not multimodal, opening an image fails with the exact message `非多模态模型无法打开图片`.
- If the current model is multimodal, `content_open` succeeds and schedules the image to be attached to the next provider-bound request only.
- That next request must include the exact explanatory text:
  `图片已通过 content_open 打开，视觉内容已附带在本轮上下文中`
- The attached image is live-only and single-use:
  - it is attached to only one request
  - it is consumed as part of the next send attempt
  - it is not written into durable transcript or durable request baselines
- The send carrying that image still goes through the normal token preflight and token-compression flow.
- If the image makes that one request too large, the runtime attempts compression before final send failure.
- If that request later fails, the following turn does not automatically reattach the image, so the conversation can continue on the existing durable text/path baseline without permanently inheriting the image size cost.

## Design Principles

1. Keep one tool contract.
   Historical image reopen should reuse `content_open`, not create an `image_open` branch in the tool catalog.

2. Keep the schema stable.
   The target type should drive behavior. The caller should not need to choose a special `image_mode`.

3. Keep visual data live-only.
   Image bytes or data URLs must never become durable baseline state.

4. Keep multimodal access explicit.
   Paths may persist in history, but raw image content is only available after an explicit `content_open`.

5. Keep compression and overflow behavior uniform.
   Image-open sends should use the same preflight and compression rules as ordinary multimodal sends.

## Design

### 1. `content_open` Remains The Sole Reopen Tool

`content_open` already exists as the concrete `content_navigation` executor for reading one excerpt from a `ref` or `path`. The current implementation is a thin wrapper over the shared `content(action=open)` path in [tool.py](/d:/NewProjects/G3KU/tools/content/main/tool.py).

This design keeps that shape:

- do not create `image_open`
- do not create an `open_image` content action
- do not fork governance, candidate selection, hydration, or prompt references into a second tool name

This preserves:

- existing `content_navigation` governance
- existing hydration and duplicate-call protection
- existing prompt vocabulary for all roles

### 2. Tool-Level Image Open Contract

When `content_open` targets a non-image resource, behavior stays unchanged.

When `content_open` targets an image file or image artifact, it enters an image-specific result contract.

The tool result should be structured JSON with at least:

- `ok`
- `operation="open"`
- `content_kind="image"`
- `mime_type`
- `requested_ref` and/or `requested_path`
- `resolved_ref` when ref mode canonicalizes to another artifact
- `summary`
- `multimodal_open_pending=true`
- a runtime-consumable image target descriptor such as canonical path and mime type

The summary should make the explicit behavior clear to the model and to logs. The stable summary text should say that the image has been opened through `content_open` and will be attached to the current round's next model send.

For image targets, line-range selectors are accepted for schema compatibility but should not affect the image payload. The implementation may ignore them or include a note in the returned payload that line-range selection is not applicable to images.

### 3. Non-Multimodal Rejection Contract

The tool must reject image targets when the active model is not multimodal.

The exact user/model-facing error text must be:

`非多模态模型无法打开图片`

This should be a normal structured tool failure, not a provider failure and not a best-effort text fallback.

The gating must happen twice:

1. tool execution time
   So the model gets an immediate and actionable error instead of believing the image was opened.

2. send assembly time
   So a model switch, restore drift, or resumed request cannot accidentally attach an image under a non-multimodal route.

### 4. Runtime Capability Surface

`content_open` currently receives runtime metadata through `__g3ku_runtime`. The runtime layer must expose an explicit capability bit that tells the tool whether the current active send model supports image multimodal access.

Recommended shape:

- `image_multimodal_enabled: true|false`
- `provider_model`
- optional `model_key`

This should come from the actual route that will be used for the next send, not from an approximate or UI-only guess.

CEO/frontdoor already has a model-binding-owned `image_multimodal_enabled` rule. Execution and acceptance nodes need an equivalent route-level view so `content_open` sees the same answer the send path will later enforce.

### 5. One-Round Live-Only Image Overlay

The tool result itself should not contain a huge data URL or inline image bytes. Instead, it should return a runtime-consumable descriptor and let the runtime-managed delivery lane build the actual multimodal request overlay.

The runtime should maintain a pending image-open overlay list scoped to the current active turn or loop state.

Required behavior:

- a successful image `content_open` adds one pending image-open item
- if the same exact image target is opened multiple times before the next send, the runtime should deduplicate by canonical target identity to avoid unnecessary bloat
- the pending overlay is consumed by the next provider-bound request assembly only
- consumption happens on first send attempt, not only on successful response
- after consumption, the overlay is cleared

This gives the desired "image only lasts one round" semantics.

### 6. Provider Request Assembly

When the runtime consumes pending image-open overlays, it should extend the next model request with:

1. a text block containing exactly:
   `图片已通过 content_open 打开，视觉内容已附带在本轮上下文中`

2. one image block per pending opened image

The exact block shape should reuse the same provider-facing multimodal conventions that the current CEO upload lane already uses:

- runtime-side `image_url` / similar neutral multimodal blocks
- provider adapters convert those into provider-specific `input_image` payloads as they already do today

This keeps the new path aligned with existing multimodal conversion and debugging infrastructure.

### 7. Durable Baseline Rules

The live-only image overlay must not be written into:

- durable transcript history
- `frontdoor_request_body_messages`
- node durable frame `messages`
- completed continuity sidecars
- reopened-session baselines

Durable state may still retain:

- the ordinary path text that existed before the reopen
- the structured `content_open` tool call/result record
- the ref/path summary that tells maintainers or later turns what was opened

This preserves the historical path while preventing durable multimodal bloat.

### 8. Compression And Overflow Semantics

The request that consumes a pending opened image must go through the same final send gate as any other provider-bound request.

That means:

- include the injected image blocks in the preflight estimate
- if the request crosses the configured trigger, run the existing token-compression path first
- only fail after compression if the final request still cannot fit safely

This is especially important for your intended behavior:

- the opened image may make one request very large
- that one request may still compress and succeed
- or it may fail
- but because the image overlay is one-round live-only, the next round falls back to the durable text/path baseline and is not forced to pay the image cost again unless the model explicitly reopens the image

### 9. CEO/Frontdoor And Node Runtime Alignment

The design must be implemented in both runtime families, not only in CEO/frontdoor.

For CEO/frontdoor:

- reuse the existing multimodal provider-shape helpers and frontdoor preflight/compression lane
- treat image-open overlays as another live-only request overlay, parallel to but separate from current-turn upload expansion

For execution and acceptance nodes:

- add the same live-only image-open overlay lane in `ReActToolLoop`
- inject the image only into the next model request
- keep node durable message history free of the image bytes

The same success/failure/consumption semantics should hold in both lanes.

## Prompt Contract

Prompt guidance must make a clear distinction between current-turn uploads and historical image reopen.

### CEO/frontdoor

Keep the current rule:

- do not use `content_open` or `exec` for the same current-turn image that is already attached directly in this request

Add a second rule:

- if a historical image is only present as a `path` or `ref` in context and direct visual inspection is needed, call `content_open`

### Execution and acceptance nodes

Add matching guidance:

- if an earlier turn or artifact only left an image path/ref and the node needs direct visual inspection, use `content_open`
- if the current route is non-multimodal, opening the image will fail and the node should not loop on that same call

## Files And Responsibilities

### Shared content tool path

- [tool.py](/d:/NewProjects/G3KU/tools/content/main/tool.py)
  Add image-target detection, multimodal capability gating, and the structured image-open payload.

- [resource.yaml](/d:/NewProjects/G3KU/tools/content_open/resource.yaml)
  Update tool description to reflect the image reopen contract without changing parameters.

- [SKILL.md](/d:/NewProjects/G3KU/tools/content_open/toolskills/SKILL.md)
  Update operator/model guidance for historical image reopen semantics.

### Content navigation helpers

- [navigation.py](/d:/NewProjects/G3KU/g3ku/content/navigation.py)
  Add any required helper methods for canonical image target resolution, mime detection, and path extraction from `ref`.

### CEO/frontdoor runtime

- [\_ceo_support.py](/d:/NewProjects/G3KU/g3ku/runtime/frontdoor/_ceo_support.py)
  Extend runtime-managed tool-result handling so image-open results can promote a live-only multimodal overlay.

- [\_ceo_runtime_ops.py](/d:/NewProjects/G3KU/g3ku/runtime/frontdoor/_ceo_runtime_ops.py)
  Reuse existing multimodal request building and preflight/compression behavior for the next-send image overlay.

### Execution and acceptance runtime

- [react_loop.py](/d:/NewProjects/G3KU/main/runtime/react_loop.py)
  Add the same live-only overlay lane for node sends.

### Prompt files

- [ceo_frontdoor.md](/d:/NewProjects/G3KU/g3ku/runtime/prompts/ceo_frontdoor.md)
- [node_execution.md](/d:/NewProjects/G3KU/main/prompts/node_execution.md)
- [acceptance_execution.md](/d:/NewProjects/G3KU/main/prompts/acceptance_execution.md)

### Architecture docs after implementation

- `docs/architecture/tool-and-skill-system.md`
- `docs/architecture/runtime-overview.md`
- `docs/architecture/web-and-admin.md`

These should be updated only when the runtime behavior lands, not merely because this spec exists.

## Risks

### Risk: Durable image leakage

If the new image-open overlay is written into durable state, the "one round only" contract fails immediately and future turns inherit unnecessary context bloat.

Mitigation:

- keep image-open overlays in explicit live-only fields
- strip them before any durable baseline write
- add regression tests that inspect stored baselines directly

### Risk: Capability drift between tool call and send

If `content_open` checks multimodal support only once, a later route change could still produce an invalid send.

Mitigation:

- gate at tool time and again at send assembly time

### Risk: Compression bypass

If image-open overlays bypass normal preflight, oversized requests may fail unexpectedly and behave differently from current upload flows.

Mitigation:

- integrate only through the existing provider-bound request assembly path
- do not create a side-channel send mechanism

### Risk: Automatic multi-turn image carryover

If retry or resume logic keeps a consumed image-open overlay around, later turns will repeatedly pay the image token cost.

Mitigation:

- define consumption on first send attempt
- clear the overlay before provider dispatch
- verify failure-path cleanup in tests

### Risk: Prompt confusion between current-turn uploads and historical reopen

The model could start opening a current-turn image that is already attached directly.

Mitigation:

- keep the existing "current-turn direct image first" guidance
- add a separate historical-image reopen rule instead of replacing the current one

## Verification Requirements

The implementation is complete only if all of the following are true:

- `content_open` still behaves identically for non-image targets
- image opens on non-multimodal routes fail with `非多模态模型无法打开图片`
- image opens on multimodal routes schedule exactly one next-send image overlay
- the next-send overlay includes the exact text `图片已通过 content_open 打开，视觉内容已附带在本轮上下文中`
- the send with that overlay still participates in token preflight and token compression
- durable baselines do not retain image bytes or multimodal blocks from the reopen
- the next round after that send does not automatically reattach the image
- CEO/frontdoor, execution nodes, and acceptance nodes all follow the same contract

## Recommended Implementation Order

1. shared `content_open` image contract and gating
2. node runtime live-only overlay consumption
3. CEO/frontdoor live-only overlay consumption
4. prompt updates
5. architecture doc updates
6. regression test expansion across tool, frontdoor, and node paths
