# OpenClaw China Sync Maintenance

## Goal

`subsystems/china_channels_host` mirrors the upstream OpenClaw China runtime while keeping the G3KU bridge contract isolated in a small native layer.

## Source Of Truth

- upstream snapshot: `D:/projects/openclaw-china-2026.3.22`
- sync map: `subsystems/china_channels_host/upstream_map.yaml`
- shared runtime registry: `subsystems/china_channels_host/channel_registry.json`
- Node runtime root: `subsystems/china_channels_host/src`
- subtree provenance note: `subsystems/china_channels_host/UPSTREAM.md`
- repository-level third-party notice: `THIRD_PARTY_NOTICES.md`

The sync map now describes the full runtime boundary:

- `src/vendor/*`: direct upstream runtime files
- `src/*.ts` and wrapper entrypoints: G3KU-native files

## What We Intentionally Do Not Sync

The G3KU subtree still excludes non-runtime upstream content where possible:

- standalone plugin packaging metadata
- installer and setup surfaces outside the runtime dependency chain

Vendor directories may retain upstream test files as read-only references, but they are not part of the sync-map upgrade gate and are excluded from the Node build. If an upstream channel entrypoint imports a small onboarding/runtime helper, vendor that helper rather than rewriting the vendor file.

## Upgrade Procedure

1. Replace the upstream snapshot reference in `channel_registry.json` and `upstream_map.yaml`.
2. Re-copy the upstream runtime files into `src/vendor/*`.
3. Re-run the sync-map audit: every vendor runtime file must appear in `upstream_map.yaml`.
4. Update `UPSTREAM.md` and `THIRD_PARTY_NOTICES.md` if the snapshot reference, upstream license declaration, or attribution surface changed.
5. Review G3KU-native files only: `src/config.ts`, `src/host.ts`, `src/runtime_bridge.ts`, wrapper files, Python registry/config/admin glue, and docs.
6. Rebuild the Node host and rerun Python/JS checks.

## Validation Checklist

- `npm run build` passes in `subsystems/china_channels_host`
- `py -3 -m py_compile` passes for the Python bridge/config/admin files
- `node --check` passes for the frontend resource pages touched by channel ids/templates
- `GET /api/china-bridge/channels` returns all canonical ids from the registry
- `g3ku china-bridge doctor` reports the same canonical ids as the registry

## Drift Policy

If a vendor file must diverge from upstream for G3KU-specific reasons:

- prefer moving the difference into the native layer instead of editing the vendor file
- if a vendor edit is unavoidable, document it in commit/PR notes and mark the mapping entry accordingly later
- keep channel ids canonical and registry-driven across Python, Node, UI, and docs

This keeps future upgrades incremental instead of forcing another architecture rewrite.
