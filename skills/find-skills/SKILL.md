---
name: find-skills
description: Helps users discover installable agent skills from the open ecosystem and install them into the Negi workspace through the skill-installer tool.
---

# Find Skills

This skill helps you discover and install skills from the open agent skills ecosystem.

## When to Use This Skill

Use this skill when the user:

- Asks "how do I do X" where X might be a common task with an existing skill
- Says "find a skill for X" or "is there a skill for X"
- Asks "can you do X" where X is a specialized capability
- Expresses interest in extending agent capabilities
- Wants to search for tools, templates, or workflows
- Mentions they wish they had help with a specific domain (design, testing, deployment, etc.)

## Discovery vs Installation

In Negi these are two separate steps:

- **Discovery**: look for candidate skills on skills.sh, GitHub, or the open web.
- **Installation**: a GitHub skill is installed by the `skill-installer` tool. It copies the repo path into `<workspace>/skills/<skill_id>/` and generates a `resource.yaml` when the upstream skill has none.

Do not use `npx skills add` (especially with `-g`). User-level/global installs live outside `<workspace>/skills/`, and Negi only discovers skills under the workspace skills directory — each installed skill also needs a `resource.yaml`. A package that only exists on a non-GitHub registry should be treated as upstream material and routed to `skill-creator` for adaptation, not installed as-is.

## How to Help Users Find Skills

### Step 1: Understand What They Need

When a user asks for help with something, identify:

1. The domain (e.g., React, testing, design, deployment)
2. The specific task (e.g., writing tests, creating animations, reviewing PRs)
3. Whether this is a common enough task that a skill likely exists

### Step 2: Check the Leaderboard First

Before searching further, check the [skills.sh leaderboard](https://skills.sh/) to see if a well-known skill already exists for the domain. The leaderboard ranks skills by total installs, surfacing the most popular and battle-tested options.

For example, top skills for web development include:
- `vercel-labs/agent-skills` — React, Next.js, web design (100K+ installs each)
- `anthropics/skills` — Frontend design, document processing (100K+ installs)

### Step 3: Search for Skills

Use web search or `web_fetch` to search skills.sh and GitHub. Good queries:

- `"agent skill" <task> site:skills.sh`
- `"SKILL.md" <task> site:github.com`

For example:

- User asks "how do I make my React app faster?" → search for `react performance skill`
- User asks "can you help me with PR reviews?" → search for `pr review skill`
- User asks "I need to create a changelog" → search for `changelog skill`

Once you find a candidate, open the repository and locate the directory that contains `SKILL.md`. That directory path is what `skill-installer` needs.

### Step 4: Verify Quality Before Recommending

**Do not recommend a skill based solely on search results.** Always verify:

1. **Install count** — Prefer skills with 1K+ installs. Be cautious with anything under 100.
2. **Source reputation** — Official sources (`vercel-labs`, `anthropics`, `microsoft`) are more trustworthy than unknown authors.
3. **GitHub stars** — Check the source repository. A skill from a repo with <100 stars should be treated with skepticism.

### Step 5: Present Options to the User

When you find relevant skills, present them to the user with:

1. The skill name and what it does
2. The install count and source
3. The exact GitHub `owner/repo/path` to install
4. A link to learn more at skills.sh or GitHub

Example response:

```
I found a skill that might help! The "react-best-practices" skill provides
React and Next.js performance optimization guidelines from Vercel Engineering.
(185K installs)

It's at: vercel-labs/agent-skills/skills/react-best-practices
```

### Step 6: Install Through skill-installer

If the user wants to proceed, call the `skill-installer` tool. Two equivalent shapes:

- `url`: `https://github.com/<owner>/<repo>/tree/<ref>/<path/to/skill>`
- `repo`: `<owner>/<repo>`, `path`: `<path/to/skill>`, optional `ref` (default `main`)

Optional parameters:

- `name`: only used when the upstream skill has no `resource.yaml`; it becomes the local skill id.
- `dest`: must stay inside the current workspace; defaults to `skills/<basename>`.
- `method`: `auto | download | git` (default `auto`).

After installation, verify before reporting success:

1. Call `load_skill_context(skill_id="<skill_id>")` and handle the four states: `ok=true` (usable), `skill_repair_required` (fix the manifest/dependencies, then reload), `skill_deleted` (registration failed — check `resource_refresh` / `catalog`), `skill_not_permitted` (RBAC role issue — do not reinstall).
2. Read the install result's `resource_refresh` and `catalog`. A failed `resource_refresh` means the skill is not registered in the calling process; a failed `catalog` must be reported and retried.
3. Review the generated `resource.yaml`: `description` ≤160 characters, `trigger.keywords` covers real user phrasings, and `requires.*` reflects what is actually installed.

## Common Skill Categories

When searching, consider these common categories:

| Category        | Example Queries                          |
| --------------- | ---------------------------------------- |
| Web Development | react, nextjs, typescript, css, tailwind |
| Testing         | testing, jest, playwright, e2e           |
| DevOps          | deploy, docker, kubernetes, ci-cd        |
| Documentation   | docs, readme, changelog, api-docs        |
| Code Quality    | review, lint, refactor, best-practices   |
| Design          | ui, ux, design-system, accessibility     |
| Productivity    | workflow, automation, git                |

## Tips for Effective Searches

1. **Use specific keywords**: "react testing" is better than just "testing"
2. **Try alternative terms**: If "deploy" doesn't work, try "deployment" or "ci-cd"
3. **Check popular sources**: Many skills come from `vercel-labs/agent-skills` or `ComposioHQ/awesome-claude-skills`

## When No Skills Are Found

If no relevant skills exist:

1. Acknowledge that no existing skill was found
2. Offer to help with the task directly using your general capabilities
3. Suggest creating a local skill with `skill-creator` (not `npx skills init`)

Example:

```
I searched for skills related to "xyz" but didn't find any matches.
I can still help you with this task directly! Would you like me to proceed?

If this is something you do often, we can create a local skill for it with skill-creator.
```
