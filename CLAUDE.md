# CLAUDE.md — remedy-server

The PDF remediation engine. Workspace-level context (corpus layout, the Stage 0–4
pipeline, veraPDF clause meanings) lives in the parent `../CLAUDE.md`.

## Agent skills

### Issue tracker

Issues live in GitHub Issues on `projectremedyai/remedy-server`, via the `gh` CLI.
See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, each label string equal to its name.
See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — `CONTEXT.md` + `docs/adr/` at the repo root.
See `docs/agents/domain.md`.
