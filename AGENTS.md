# Agent conventions

Guidance for AI agents (and humans) working in this repo.

## Documentation

- **Don't name docs in all caps.** Use lowercase kebab-case filenames for
  documentation you create — `branding.md`, `deploy-notes.md`, not
  `BRANDING.md` or `DEPLOY_NOTES.md`.
- Exception: the conventional root files keep their standard casing —
  `README.md`, `LICENSE`, `AGENTS.md`.

## Commits

- **Never add a `Co-Authored-By` trailer** to commit messages for AI assistants.

## Branching and releases

- **`main` is protected — don't commit to it directly.** A ruleset requires
  changes to land through a pull request, and blocks force-pushes and branch
  deletion. A direct `git push origin main` is rejected by the server. Work on
  a branch and open a PR (`gh pr create`); no approving review is required, so
  you can merge your own once CI looks good.
- **`v*` tags are immutable.** A tag ruleset blocks deleting or moving them,
  because pushing one publishes a signed, notarized release that users execute.
  To fix a bad release, cut the next version — don't retag.
- CI is not a merge gate. The workflows are path-filtered, so a PR only
  triggers the jobs whose paths it touches; read the results rather than
  assuming a green tick means everything ran.
