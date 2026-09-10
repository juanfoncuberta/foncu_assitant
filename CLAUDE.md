# CLAUDE.md

## Commit messages

- **Subject line**: ≤ ~72 characters; one imperative phrase summarising the change
  (e.g. `add tests for claude_code_executor edge cases`, not a list of modules).
- **Body**: only when it adds something the diff cannot show — the *why* behind
  a non-obvious decision. Most commits need no body.
- Never list module-by-module what changed; that is already visible in
  `git diff` / `git show`.
