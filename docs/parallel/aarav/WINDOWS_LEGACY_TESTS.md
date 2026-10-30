# Native Windows Legacy Qualification

An additional full `uv run pytest -q` was run on native Windows after fixing the
new contract test's duplicate basename. Result: **17 failed, 2001 passed,
164 skipped in 469.66 seconds**. This is not a green full-repository result.

The exact failed node IDs are retained in `windows-legacy-failures.json`. All
failing test files, `packages/core` and `apps/worker` are unchanged relative to
`73224ee9133133663a7f4df6925b8afac209d88b`. We did not reproduce the entire base
checkout independently, so this is not a blanket claim that every failure is
pre-existing or caused only by Windows.

Observed failure classes:

- Builder retries encountered access-denied cleanup; later assertions observed
  an earlier call's budget/secret outcome rather than a successful second call.
- Symlink fixtures failed with Windows error 1314 (missing symlink privilege).
- POSIX executable-mode and shell-hook assertions do not match native Windows
  mode/path behavior in this environment.
- A verifier Git revert refused a dirty policy file; tracing span order differed.
  These two need focused diagnosis rather than being dismissed as OS-only.

These paths belong to Muaz's preserved runtime lane. Do not disable security
assertions, elevate machine privileges or edit the other lane to make the count
green. Review Windows support versus the qualified Linux/container execution path
before claiming native customer-runtime support.

For contrast, the separate real-PostgreSQL platform suite passes all 59 tests on
this Windows host, including backup/restore and controlled-clock retention.
The hosted Linux checkpoint `22f90f5` passed its full Python suite with 2086 passed,
96 skipped; the separate hosted platform job passed 59 tests and four browser
checks. Different environments/skips are not interchangeable product evidence.
