# Skill Registry — portfolio-mcp-server

> Project key: `portfolio-mcp-server`
> Folder: `/home/harri/development/projects/portfolio/mcp-server-playground/`
> Init date: 2026-08-04
>
> This file is the **delegator's index**, not a generated summary. Subagents
> read the full `SKILL.md` at the listed path before doing work — never rely
> on this file as the runtime contract.

## Sources scanned

- `/home/harri/.agents/skills`
- `/home/harri/.config/opencode/skills`

## Skills relevant to this project

| Skill | Trigger / description | Scope | Path |
| --- | --- | --- | --- |
| `sdd-explore` | Explore SDD ideas before committing to a change. Trigger: orchestrator launches exploration or requirement clarification. | user | `/home/harri/.config/opencode/skills/sdd-explore/SKILL.md` |
| `sdd-propose` | Create an SDD change proposal with intent, scope, and approach. Trigger: orchestrator launches proposal work for a change. | user | `/home/harri/.config/opencode/skills/sdd-propose/SKILL.md` |
| `sdd-spec` | Write SDD delta specs with requirements and scenarios. Trigger: orchestrator launches spec work for a change. | user | `/home/harri/.config/opencode/skills/sdd-spec/SKILL.md` |
| `sdd-design` | Create the SDD technical design and architecture approach. Trigger: orchestrator launches design for a change. | user | `/home/harri/.config/opencode/skills/sdd-design/SKILL.md` |
| `sdd-tasks` | Break an SDD change into implementation tasks. Trigger: orchestrator launches task planning for a change. | user | `/home/harri/.config/opencode/skills/sdd-tasks/SKILL.md` |
| `sdd-apply` | Implement SDD tasks from specs and design. Trigger: orchestrator launches apply for one or more change tasks. | user | `/home/harri/.config/opencode/skills/sdd-apply/SKILL.md` |
| `sdd-verify` | Execute tests and prove implementation matches specs, design, and tasks. | user | `/home/harri/.config/opencode/skills/sdd-verify/SKILL.md` |
| `sdd-archive` | Archive a completed SDD change by syncing delta specs. Trigger: orchestrator launches archive after implementation and verification. | user | `/home/harri/.config/opencode/skills/sdd-archive/SKILL.md` |
| `frontend-design` | Guidance for distinctive, intentional visual design when building new UI or reshaping an existing one. Helps with aesthetic direction, typography, and making choices that don't read as templated defaults. | user | `/home/harri/.agents/skills/frontend-design/SKILL.md` |
| `comment-writer` | Write warm, direct collaboration comments. Trigger: PR feedback, issue replies, reviews, Slack messages, or GitHub comments. | user | `/home/harri/.config/opencode/skills/comment-writer/SKILL.md` |
| `branch-pr` | Create Gentle AI pull requests with issue-first checks. Trigger: creating, opening, or preparing PRs for review. | user | `/home/harri/.config/opencode/skills/branch-pr/SKILL.md` |
| `chained-pr` | Trigger: PRs over 400 lines, stacked PRs, review slices. Split oversized changes into chained PRs that protect review focus. | user | `/home/harri/.config/opencode/skills/chained-pr/SKILL.md` |
| `work-unit-commits` | Plan commits as reviewable work units. Trigger: implementation, commit splitting, chained PRs, or keeping tests and docs with code. | user | `/home/harri/.config/opencode/skills/work-unit-commits/SKILL.md` |
| `judgment-day` | Trigger: judgment day, dual review, adversarial review, juzgar. Run explicit blind dual review with at most two scoped fix/re-judgment rounds. | user | `/home/harri/.config/opencode/skills/judgment-day/SKILL.md` |
| `cognitive-doc-design` | Design docs that reduce cognitive load. Trigger: writing guides, READMEs, RFCs, onboarding, architecture, or review-facing docs. | user | `/home/harri/.config/opencode/skills/cognitive-doc-design/SKILL.md` |
| `issue-creation` | Create Gentle AI issues with issue-first checks. Trigger: creating GitHub issues, bug reports, or feature requests. | user | `/home/harri/.config/opencode/skills/issue-creation/SKILL.md` |

## Skills NOT relevant to this project (skip by default)

| Skill | Reason |
| --- | --- |
| `go-testing` | Stack is Python; no Go code. |
| `skill-creator`, `skill-improver`, `skill-registry` | Meta-skills for maintaining the registry itself, not for project work. |

## Loading protocol for delegators

1. Match the task's `Trigger / description` against the relevant-skills table.
2. Pass only the matching `Path` values under `## Skills to load before work`.
3. The subagent reads those exact `SKILL.md` files before reading, writing, reviewing, testing, or creating artifacts.
4. If a task is purely exploratory or doesn't match any skill above, proceed without skill injection and report `skill_resolution: none`.

## Convention files in this project

- `openspec/config.yaml` — SDD session preflight, testing capabilities, cross-phase invariants. **Read this on every change start.**
- `pyproject.toml` — Python deps, ruff config, pytest config, coverage thresholds.
- `config/projects.manifest.yaml` — single source of truth for what the indexer can read.
- `.pre-commit-config.yaml` — local hooks (Layer 4 of the 5-layer security model).
- `.github/workflows/` — CI gates: test, lint, secret-scan, deploy.
