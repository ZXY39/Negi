# OpenClaw China Sync Maintenance

## Goal

`subsystems/china_channels_host` keeps selected communication logic extracted from upstream OpenClaw China sources, while staying fully hosted inside G3KU.

## Source Of Truth

- sync map: `subsystems/china_channels_host/upstream_map.yaml`
- runtime root: `subsystems/china_channels_host/src`

The sync map documents which files are:

- direct copies
- adapted copies
- G3KU-native bridge files

## What We Intentionally Do Not Sync

The G3KU subtree excludes non-runtime upstream content, including:

- onboarding and setup CLI flows
- OpenClaw plugin installer surfaces
- tool and skill wrappers
- upstream test files

When upstream changes touch those areas, review them for ideas, but do not reintroduce them into the runtime subtree unless G3KU needs them for production behavior.

## Upgrade Procedure

1. Pick the upstream tag or commit to adopt.
2. Update `upstream_map.yaml` with the new `ref`.
3. Diff only the mapped runtime files.
4. Port changes into the matching local files.
5. Re-run Python bridge tests and Node build validation.

## Validation Checklist

- `load_config()` rejects legacy `channels.*`
- `g3ku web` starts without legacy channel framework
- `npm run build` passes in `subsystems/china_channels_host`
- `g3ku china-bridge doctor` reports the expected subsystem state

## Drift Policy

If a local runtime file diverges for G3KU-specific reasons:

- keep the local file path stable
- document the reason in commit notes or PR text
- preserve the upstream mapping entry as `adapted_copy`

This keeps future upgrades incremental instead of requiring another architecture rewrite.
