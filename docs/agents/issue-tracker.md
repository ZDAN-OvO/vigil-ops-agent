# Issue tracker: GitHub

Issues and specs live in GitHub Issues for `ZDAN-OvO/vigil-ops-agent`.
Use the `gh` CLI with `--repo ZDAN-OvO/vigil-ops-agent` to avoid writing to another repository.
In the examples below, `<repo>` means `ZDAN-OvO/vigil-ops-agent`.

## Basic operations

- Publish a spec or implementation task: create a GitHub issue.
- Read: `gh issue view <number> --repo <repo> --json number,title,body,labels,comments`.
- List: `gh issue list --repo <repo> --state open --json number,title,body,labels,assignees`.
- Create: `gh issue create --repo <repo> --title "<title>" --body-file <body-file>`.
- Update: `gh issue edit <number> --repo <repo> --body-file <body-file>`.
- Add a label: `gh issue edit <number> --repo <repo> --add-label "<label>"`.
- Remove a label: `gh issue edit <number> --repo <repo> --remove-label "<label>"`.
- Comment: `gh issue comment <number> --repo <repo> --body-file <body-file>`.
- Close: post the resolution first, then run `gh issue close <number> --repo <repo>`.

Write multiline bodies to a temporary UTF-8 file and pass `--body-file` to preserve newlines.
Creating, commenting on, or closing issues requires authorization from the active task;
this configuration alone does not authorize publication.

## Parent issues and blockers

Prefer GitHub's native sub-issues and issue dependencies.
Use the installed `gh` version's supported CLI commands or GitHub API endpoints.
API operations that require an issue's numeric database ID must not receive its
`#number` or GraphQL `node_id` instead.

When native relationships are unavailable:

- Put `Part of #<parent>` at the top of the child issue body.
- Link child issues in a task list in the parent body.
- Put `Blocked by: #<number>` at the top of a blocked issue body.
- Treat a task as startable only after every blocker is closed.

## Pull requests as a triage surface

**PRs as a request surface: no.**

PRs deliver implementations and link to issues; external PRs are not included in
the triage request queue by default. Issues and PRs share a number space, so
confirm the object type before operating on a bare number.
Creating or updating a PR does not authorize merging or enabling auto-merge.

## Wayfinding

When a skill uses a map and frontier workflow:

- The map is an issue labelled `wayfinder:map`.
- Link child tickets to the map; use `wayfinder:<type>` for their task types.
- The frontier contains open, unassigned children with no open blockers.
- Record claims, conclusions, and resolution state in GitHub, not only in chat.
- This setup creates only the five configured triage labels. Create additional
  labels when the relevant skill needs them and the active task authorizes it.
