# red-green-check

Tells a proven bug fix from a flagged one: a test that fails before the fix and
passes after it, every run, before you merge what an AI agent wrote.

Point it at your repo, the test that reproduces the bug, and the commit that claims
the fix. It runs the test on the code before the fix and on the fix itself, 3 times
each, every run from a clean copy of the repo, and prints one verdict:

- **PROVEN**: fails every run before the fix, passes every run after
- **NOT A REPRO**: passes before the fix, so it never showed the bug
- **FIX INCOMPLETE**: fails before the fix and still fails after it
- **FLAKY**: anything mixed, a red or a green that does not hold on every run

It exits 0 on PROVEN and 1 on anything else, so it can gate a pull request.
Python 3.9 or newer and git, standard library only, nothing to install.

## First run: no key, no GPU

```
git clone https://github.com/paolo-perrone/red-green-check
cd red-green-check
python3 red_green_check.py demo
```

The demo builds four tiny repos, each a shop whose payment webhook ships an order,
and checks one agent fix in each. Stripe documents that a webhook endpoint "might
occasionally receive the same event more than once", so a handler that is not
idempotent ships the same order twice. The first case, an agent fix that deduplicates
by event ID, prints this (the two commit hashes change on every run, because the demo
builds its repos fresh):

```
== proven
red_green_check 1.0.0
repo   : demo repo, case proven
test   : python3 -m unittest -q tests.test_webhook
before : 6728164  red 3/3  green 0/3
after  : 78b943c  red 0/3  green 3/3
VERDICT: PROVEN: the test fails 3 of 3 runs before the fix and passes 3 of 3 after.
```

The other three cases end on these lines, and the demo's last line says whether every
case earned the verdict it was built for:

```
VERDICT: NOT A REPRO: the test passes 3 of 3 runs before the fix, so it never showed the bug.
VERDICT: FIX INCOMPLETE: the test fails 3 of 3 runs before the fix and still fails 3 of 3 after.
VERDICT: FLAKY: red 10 of 10 runs before the fix, green 7 of 10 after; rerun with --runs 100 before believing either side.
demo: every case earned the verdict it was built for.
```

- **not-a-repro**: the agent's test sends the event once, so it passes on the buggy code.
- **incomplete**: the fix checks a set of seen event IDs and never adds to it.
- **race**: two deliveries arrive from two threads at once, and the fix checks, works,
  then records, so both can pass the check. Its counts vary between machines; the
  word FLAKY does not. CI checks every line above except the race counts.

## Check a real fix

```
python3 red_green_check.py check \
  --repo . \
  --test "python3 -m pytest -q tests/test_webhooks.py::test_duplicate_event_ships_once" \
  --fix HEAD
```

- `--fix` is the commit that claims the fix. The code before it is its parent, or
  `--base` when the fix spans several commits (a pull request: `--base origin/main`).
- The test may be committed before the fix (the safe order: the agent never touches
  it) or added in the fix commit. Either way it runs against the old code: test files
  the fix adds or edits are copied into the before-copy for every run.
- If the fix edits a test that already existed, the output says so. That is the case
  where an agent can make a red test green by weakening it, so read that diff first.
- `--reason REGEX` counts a red run before the fix only when the test output matches,
  for example `--reason "AssertionError"`, so a test that fails on an import error
  does not count as reproducing the bug.
- `--runs N` sets runs per side (default 3). A race that fails 1 run in 50 needs
  about 100 runs to show up 87% of the time: 1 - 0.98^100 = 0.87. `--runs 100 --jobs 8`
  runs 100 per side on 8 parallel copies.
- `--jobs J` runs J copies in parallel per side, each its own clean worktree.
- Every run starts from a clean copy: tracked files reset and untracked files deleted,
  ignored ones included (`git clean -fdx`), so one run's caches never reach the next.
  `--keep-ignored` keeps ignored files between runs when rebuilding them is too slow.
- `--json` prints the counts and the verdict as JSON for CI.

Run it with your project's environment active: it runs your test command as is.

## In CI

`examples/agent-fix-check.yml` runs it on every pull request labelled `agent-fix`,
reading the reproduction command from the PR's `REPRO_TEST` file, and fails the
check on anything but PROVEN.

## Tested on

Tested 2026-10-13 on macOS 26 with Python 3.14 and git 2.39: the whole demo, all four
cases, runs in about 5 seconds. CI runs the unit tests and the demo on Ubuntu with
Python 3.9, 3.12 and 3.13 on every push. Supported through 2027-10. No dependencies,
so there is no lockfile to drift.
