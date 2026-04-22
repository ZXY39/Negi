# China Channels Host Upstream Note

`subsystems/china_channels_host` is a mixed subtree:

- `src/vendor/*` mirrors runtime files synced from `openclaw-china`
- wrapper entrypoints such as `src/host.ts`, `src/runtime_bridge.ts`, `src/config.ts`, and the registry files are G3KU-native integration code

## Current upstream reference

- Repository: `https://github.com/BytePioneer-AI/openclaw-china.git`
- Local snapshot reference: `snapshot:openclaw-china-2026.3.22`
- Canonical sync map: `upstream_map.yaml`

## License and provenance

The upstream repository metadata and `package.json` currently declare `MIT`.

This directory is not a standalone relicensed project copy. Vendored upstream files keep their upstream provenance, while G3KU-native bridge files remain part of the main G3KU project.

For repository-level third-party attribution, see [../../THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md).

## Sync rule

When updating this subtree to a new upstream snapshot:

1. update `upstream_map.yaml`
2. update `channel_registry.json`
3. update this file if the upstream reference changes
4. update `../../THIRD_PARTY_NOTICES.md` if the upstream license or attribution surface changes
5. preserve any upstream `LICENSE`, `NOTICE`, or equivalent file if the upstream project starts shipping one
