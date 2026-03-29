# load_skill_context

Load full context for a currently visible skill.

## Parameters
- `skill_id`: The skill id to load.

## Returns
- `content` (full raw skill document)
- `level`
- `skill_id`
- `l0`
- `l1`

## Usage
Use `load_skill_context` only for a skill that is already listed in the node's visible skill inventory.
Do not use it to search for candidate skills.

## Workflow Notes

- Do not assume invisible skills exist; only load skill ids that are explicitly visible in the current turn.
- For skill installation, update, download, or online lookup requests related to ClawHub, if `clawhub-skill-manager` is visible, call `load_skill_context(skill_id="clawhub-skill-manager")` first and treat it as the only workflow entry.
- Skills coming from ClawHub should be treated as upstream material for third-party projects. Before adopting them into G3KU, evaluate whether their `SKILL.md`, trigger rules, resource descriptions, and tool assumptions need to be rewritten for G3KU.
