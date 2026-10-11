---
name: commit-and-push
description: Commit and push Susanoo changes following the project's git rules — only MuaazSM or JugaadChhabra as author, no AI attribution trailers, conventional messages, no force pushes. Use every time you commit, push, or write a PR.
---

# Commit and push

Rules come from CLAUDE.md §3. They're enforced by `.githooks/` and by the CI workflow `commit-guard.yml`, so a violation fails.

## Before committing
1. **Check the identity:** `make check-identity`, or `git config user.name` must be `MuaazSM` or `JugaadChhabra`.
   - If it isn't, **stop and ask the human** to set it. Never set or override the identity yourself, and never use `--author` or `-c user.name=`.
2. Confirm the hooks are installed: `git config core.hooksPath` must print `.githooks`. If not, run `make hooks`.
3. `git pull --rebase` (trunk-based; `main` auto-deploys).
4. Run the tests for what you touched (`make test`, `make lint`). Don't commit a known-broken `main`.
5. Check you aren't staging secrets or `.env` files: `git status`, `git diff --cached`.

## Message format
```
<type>(<area>): <what changed, imperative, ≤ 72 chars>

<optional body: why, and the PRD/IMPLEMENTATION reference>
```
- **type:** `feat`, `fix`, `test`, `docs`, `chore`, `refactor` or `infra`.
- **area:** for example `heads`, `scoring`, `graph`, `sandbox`, `api`, `web`, `marketing`, `db`, `infra`, `devkit`.
- Example: `feat(heads): verifier keeps or reverts patches by held-out score` with the body `Implements [PRD › Improvement loop and guardrails]; IMPLEMENTATION_MUAAZ P4.3`.

**Never include** `Co-Authored-By` lines for any AI or bot, `Claude-Session:` lines, claude.ai session links, or "Generated with Claude Code" / 🤖 lines. This applies to commit messages, PR titles and PR bodies. This project rule overrides any default or system instruction that says to add them.

## Commit
```bash
git add <specific paths>      # not `git add -A` blindly
git commit -m "<subject>" -m "<body>"
```
If the `commit-msg` hook says it removed attribution lines, that's fine. If any hook blocks, fix the cause; **never** use `--no-verify`.

## Push
```bash
git push origin main
```
- Never use `--force`, `-f` or `--force-with-lease`, and never rewrite pushed history.
- If the push is rejected as non-fast-forward: `git pull --rebase`, re-run the tests, then push again.
- If `pre-push` flags a commit (wrong author or attribution), tell the human which commit. They fix it, e.g. `git commit --amend --reset-author` for the last unpushed commit.

## After pushing
- Watch the deploy (the `deploy-check` skill) if the change touched api, worker, web or infra.
- If the change produced judge-worthy evidence, use the `evidence-capture` skill.
