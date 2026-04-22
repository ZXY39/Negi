# Third-Party Notices

This repository includes a vendored China channel runtime subtree under `subsystems/china_channels_host`.

## openclaw-china

- Upstream project: `openclaw-china`
- Upstream repository: `https://github.com/BytePioneer-AI/openclaw-china.git`
- Local integrated subtree: `subsystems/china_channels_host`
- Current local snapshot reference: `snapshot:openclaw-china-2026.3.22`
- Snapshot metadata sources:
  - `subsystems/china_channels_host/upstream_map.yaml`
  - `subsystems/china_channels_host/channel_registry.json`

The current upstream repository metadata and published `package.json` declare the project license as `MIT`.

G3KU keeps its own project license, but vendored third-party code remains subject to the upstream license and attribution requirements that apply to that code.

Maintainer notes:

- Files under `subsystems/china_channels_host/src/vendor/*` are synced from upstream runtime sources unless a local note says otherwise.
- Files under `subsystems/china_channels_host/src/*` outside the vendor subtree are G3KU-native bridge and wrapper code.
- When syncing to a new upstream snapshot, update the snapshot references in:
  - `subsystems/china_channels_host/upstream_map.yaml`
  - `subsystems/china_channels_host/channel_registry.json`
  - `subsystems/china_channels_host/UPSTREAM.md`
  - this file when the upstream license or provenance details change
- If the upstream project adds or changes a `LICENSE`, `NOTICE`, or similar copyright file in a later snapshot, preserve that file alongside the synced subtree and update this notice accordingly.
