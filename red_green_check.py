#!/usr/bin/env python3
"""red_green_check.py: is the bug your agent says it fixed proven fixed?

Runs one reproduction test on the code before a fix and on the fix itself, N times
each, every run from a clean copy of the repo, and prints one verdict:

  PROVEN          fails every run before the fix, passes every run after
  NOT A REPRO     passes before the fix, so it never showed the bug
  FIX INCOMPLETE  fails every run before the fix and still fails every run after
  FLAKY           anything mixed: a red or a green that does not hold on every run

  python3 red_green_check.py check --repo . --test "pytest -q tests/test_x.py::test_y" --fix HEAD
  python3 red_green_check.py demo        # four bundled cases, no key, no GPU

Exit 0 on PROVEN, 1 on any other verdict, 2 on a usage or git error.
Standard library only, Python 3.9+, and git.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

VERSION = "1.0.0"

# A path is a test file when a directory is named test or tests, or the file name
# follows the usual test conventions of Python, Go, JS and TS.
TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*$|[^/]*_test\.[a-z]+$|[^/]*\.(spec|test)\.[jt]sx?$)")


class GitError(Exception):
    pass


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def resolve(repo, rev):
    return git(repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}")


def test_files_in_fix(repo, base, fix):
    """(added, edited): the test files the fix adds, and the existing ones it changes."""
    added, edited = [], []
    for line in git(repo, "diff", "--name-status", base, fix).splitlines():
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        if not TEST_PATH.search(path):
            continue
        if status.startswith("A"):
            added.append(path)
        elif status[0] in "MR":
            edited.append(path)
    return added, edited


def verdict(before, after, runs):
    """The verdict and its one-line reason. `before` and `after` count runs as red (failed
    for the stated reason, or for any reason when none was given), other (failed for
    another reason) and green (passed)."""
    if before["green"] == runs:
        return "NOT A REPRO", (f"the test passes {runs} of {runs} runs before the fix, "
                               f"so it never showed the bug.")
    if before["other"] == runs:
        return "NOT A REPRO", (f"the test fails {runs} of {runs} runs before the fix, "
                               f"but never for the stated reason.")
    if before["red"] == runs and after["green"] == runs:
        return "PROVEN", (f"the test fails {runs} of {runs} runs before the fix "
                          f"and passes {runs} of {runs} after.")
    after_red = after["red"] + after["other"]
    if before["red"] == runs and after_red == runs:
        return "FIX INCOMPLETE", (f"the test fails {runs} of {runs} runs before the fix "
                                  f"and still fails {runs} of {runs} after.")
    more = "; rerun with --runs 100 before believing either side." if runs < 100 else "."
    return "FLAKY", (f"red {before['red']} of {runs} runs before the fix, green {after['green']} "
                     f"of {runs} after{more}")


class Copy:
    """One clean worktree of the repo at one commit, reset before every run."""

    def __init__(self, repo, commit, pristine):
        self.repo, self.commit, self.pristine = repo, commit, pristine
        self.path = tempfile.mkdtemp(prefix="red-green-")
        git(repo, "worktree", "add", "--detach", "--force", self.path, commit)

    def reset(self, inject):
        git(self.path, "checkout", "--force", "--detach", self.commit)
        git(self.path, "clean", "-fdx" if self.pristine else "-fd")
        for rel, data in inject.items():
            dest = os.path.join(self.path, rel)
            os.makedirs(os.path.dirname(dest) or self.path, exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(data)

    def remove(self):
        subprocess.run(["git", "-C", self.repo, "worktree", "remove", "--force", self.path],
                       capture_output=True)
        shutil.rmtree(self.path, ignore_errors=True)


def run_side(repo, commit, test, runs, jobs, timeout, reason, pristine, inject):
    """Run the test `runs` times at `commit`, on `jobs` parallel clean copies."""
    copies = [Copy(repo, commit, pristine) for _ in range(max(1, min(jobs, runs)))]
    counts = {"red": 0, "other": 0, "green": 0}
    try:
        def one(i):
            copy = copies[i % len(copies)]
            copy.reset(inject)
            env = dict(os.environ, RED_GREEN_RUN=str(i))
            try:
                r = subprocess.run(test, shell=True, cwd=copy.path, env=env,
                                   capture_output=True, text=True, timeout=timeout)
                ok, out = r.returncode == 0, r.stdout + r.stderr
            except subprocess.TimeoutExpired:
                ok, out = False, f"timed out after {timeout}s"
            if ok:
                return "green"
            if reason and not re.search(reason, out):
                return "other"
            return "red"
        # Runs on one copy must not overlap, so each copy takes every len(copies)-th run.
        def lane(start):
            return [one(i) for i in range(start, runs, len(copies))]
        with ThreadPoolExecutor(max_workers=len(copies)) as pool:
            for results in pool.map(lane, range(len(copies))):
                for res in results:
                    counts[res] += 1
    finally:
        for copy in copies:
            copy.remove()
    return counts


def check(repo, test, fix, base=None, runs=3, jobs=1, timeout=600, reason=None,
          pristine=True, label=None, out=None, as_json=False):
    """Run the check and print its report. Returns (verdict, report dict)."""
    out = out or sys.stdout
    repo = os.path.abspath(repo)
    fix_sha = resolve(repo, fix)
    base_sha = resolve(repo, base) if base else resolve(repo, fix_sha + "^")
    added, edited = test_files_in_fix(repo, base_sha, fix_sha)
    inject = {}
    for rel in added + edited:
        inject[rel] = subprocess.run(["git", "-C", repo, "show", f"{fix_sha}:{rel}"],
                                     capture_output=True).stdout
    before = run_side(repo, base_sha, test, runs, jobs, timeout, reason, pristine, inject)
    after = run_side(repo, fix_sha, test, runs, jobs, timeout, reason, pristine, {})
    name, why = verdict(before, after, runs)
    report = {"version": VERSION, "repo": label or repo, "test": test, "runs": runs,
              "base": base_sha, "fix": fix_sha, "before": before, "after": after,
              "tests_added_by_fix": added, "tests_edited_by_fix": edited,
              "verdict": name, "reason": why}
    if as_json:
        print(json.dumps(report, indent=2), file=out)
        return name, report
    p = lambda s="": print(s, file=out)
    p(f"red_green_check {VERSION}")
    p(f"repo   : {label or repo}")
    p(f"test   : {test}")
    if reason:
        p(f"reason : a red run before the fix counts only when the output matches /{reason}/")
    p(f"before : {base_sha[:7]}  red {before['red']}/{runs}  "
      + (f"other {before['other']}/{runs}  " if reason else "") + f"green {before['green']}/{runs}")
    p(f"after  : {fix_sha[:7]}  red {after['red'] + after['other']}/{runs}  green {after['green']}/{runs}")
    if added:
        p(f"note   : the fix adds {', '.join(added)}; the before runs use that test against the old code")
    for rel in edited:
        p(f"warning: the fix also edits {rel}, which existed before it. The before runs use the fix's "
          f"version; read that diff to check the agent did not weaken the test.")
    p(f"VERDICT: {name}: {why}")
    return name, report


# ------------------------------------------------------------------ the bundled demo

SHOP_BUG = '''"""A tiny shop: a payment webhook ships the order it pays for."""


class Shop:
    def __init__(self):
        self.shipments = []

    def handle_webhook(self, event):
        if event["type"] == "payment_intent.succeeded":
            self.shipments.append(event["data"]["object"]["metadata"]["order_id"])
'''

FIXES = {
    # Deduplicate by event ID, the guard Stripe's docs give for duplicate deliveries.
    "proven": SHOP_BUG.replace(
        "        self.shipments = []\n",
        "        self.shipments = []\n        self.seen = set()\n").replace(
        '        if event["type"] == "payment_intent.succeeded":\n',
        '        if event["id"] in self.seen:\n            return\n        self.seen.add(event["id"])\n'
        '        if event["type"] == "payment_intent.succeeded":\n'),
    # Checks the set and never adds to it, so the second delivery still ships.
    "incomplete": SHOP_BUG.replace(
        "        self.shipments = []\n",
        "        self.shipments = []\n        self.seen = set()\n").replace(
        '        if event["type"] == "payment_intent.succeeded":\n',
        '        if event["id"] in self.seen:\n            return\n'
        '        if event["type"] == "payment_intent.succeeded":\n'),
    # Checks, works, then records: two deliveries at the same moment both pass the check.
    "race": SHOP_BUG.replace(
        '"""A tiny shop: a payment webhook ships the order it pays for."""\n',
        '"""A tiny shop: a payment webhook ships the order it pays for."""\nimport time\n').replace(
        "        self.shipments = []\n",
        "        self.shipments = []\n        self.seen = set()\n").replace(
        '        if event["type"] == "payment_intent.succeeded":\n',
        '        if event["id"] in self.seen:\n            return\n        time.sleep(0.02)\n'
        '        self.seen.add(event["id"])\n'
        '        if event["type"] == "payment_intent.succeeded":\n'),
}
FIXES["not-a-repro"] = FIXES["proven"]

TEST_TWICE = '''import unittest

from shop import Shop

EVENT = {"id": "evt_1", "type": "payment_intent.succeeded",
         "data": {"object": {"metadata": {"order_id": "ord_42"}}}}


class DuplicateWebhook(unittest.TestCase):
    def test_same_event_twice_ships_once(self):
        shop = Shop()
        shop.handle_webhook(EVENT)
        shop.handle_webhook(EVENT)  # Stripe can deliver the same event more than once
        self.assertEqual(shop.shipments, ["ord_42"])


if __name__ == "__main__":
    unittest.main()
'''

# The agent's "regression test" sends the event once, so it passes on the buggy code.
TEST_ONCE = TEST_TWICE.replace(
    "        shop.handle_webhook(EVENT)\n        shop.handle_webhook(EVENT)  # Stripe can deliver the same event more than once\n",
    "        shop.handle_webhook(EVENT)\n")

# Two deliveries from two threads; the second starts after a stagger drawn per run, so
# whether it lands inside the first one's check-to-record window changes run to run.
TEST_RACE = '''import os
import random
import threading
import time
import unittest

from shop import Shop

EVENT = {"id": "evt_1", "type": "payment_intent.succeeded",
         "data": {"object": {"metadata": {"order_id": "ord_42"}}}}


class ConcurrentDuplicateWebhook(unittest.TestCase):
    def test_same_event_at_once_ships_once(self):
        random.seed(int(os.environ.get("RED_GREEN_RUN", "0")) * 7919)
        stagger = random.uniform(0.0, 0.04)
        shop = Shop()
        first = threading.Thread(target=shop.handle_webhook, args=(EVENT,))
        second = threading.Thread(target=lambda: (time.sleep(stagger), shop.handle_webhook(EVENT)))
        first.start(); second.start(); first.join(); second.join()
        self.assertEqual(shop.shipments, ["ord_42"])


if __name__ == "__main__":
    unittest.main()
'''

CASES = {
    "proven":      (TEST_TWICE, "test_webhook", 3, "PROVEN"),
    "not-a-repro": (TEST_ONCE, "test_webhook", 3, "NOT A REPRO"),
    "incomplete":  (TEST_TWICE, "test_webhook", 3, "FIX INCOMPLETE"),
    "race":        (TEST_RACE, "test_webhook", 10, "FLAKY"),
}


def build_demo_repo(case):
    """A three-commit repo: the buggy shop, the agent's reproduction test, the agent's fix."""
    test_src, module, runs, _expected = CASES[case]
    d = tempfile.mkdtemp(prefix=f"red-green-demo-{case}-")
    def w(rel, text):
        os.makedirs(os.path.join(d, os.path.dirname(rel)) or d, exist_ok=True)
        with open(os.path.join(d, rel), "w", encoding="utf-8") as fh:
            fh.write(text)
    def commit(msg):
        git(d, "add", "-A")
        git(d, "-c", "user.name=demo", "-c", "user.email=demo@example.com",
            "commit", "-q", "-m", msg)
    git(d, "init", "-q")
    w("shop.py", SHOP_BUG)
    commit("shop: ship the order on payment_intent.succeeded")
    w("tests/__init__.py", "")
    w(f"tests/{module}.py", test_src)
    commit("agent: reproduce the duplicate shipment as a test")
    w("shop.py", FIXES[case])
    commit("agent: fix duplicate shipment")
    return d, f"python3 -m unittest -q tests.{module}", runs


def demo(out=None):
    """Check one agent fix in each bundled case. Exit 0 when every case earns the verdict
    it was built for, so the demo doubles as a self-check of the tool on a fresh machine."""
    out = out or sys.stdout
    print(f"red_green_check {VERSION}: four agent fixes to one webhook bug\n", file=out)
    misses = []
    for case, (_src, _mod, _runs, expected) in CASES.items():
        repo, test, runs = build_demo_repo(case)
        try:
            print(f"== {case}", file=out)
            name, _ = check(repo, test, "HEAD", runs=runs, label=f"demo repo, case {case}", out=out)
            if name != expected:
                misses.append(f"{case} earned {name}, built for {expected}")
        finally:
            shutil.rmtree(repo, ignore_errors=True)
        print(file=out)
    if misses:
        print("demo: " + "; ".join(misses), file=out)
        return 1
    print("demo: every case earned the verdict it was built for.", file=out)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="red_green_check.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="check one fix in your repo")
    c.add_argument("--repo", default=".", help="the git repo (default: the current directory)")
    c.add_argument("--test", required=True, help="the shell command that runs the reproduction test")
    c.add_argument("--fix", required=True, help="the commit that claims the fix")
    c.add_argument("--base", help="the code before the fix (default: the fix's parent)")
    c.add_argument("--runs", type=int, default=3, help="runs per side (default 3)")
    c.add_argument("--jobs", type=int, default=1, help="parallel clean copies per side (default 1)")
    c.add_argument("--timeout", type=int, default=600, help="seconds per run before it counts as red")
    c.add_argument("--reason", help="regex a failing run's output must match to count as red before the fix")
    c.add_argument("--keep-ignored", action="store_true",
                   help="keep files git ignores between runs (default: delete them, so runs stay independent)")
    c.add_argument("--json", action="store_true", help="print the report as JSON")
    sub.add_parser("demo", help="four bundled cases, no key, no GPU")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "demo":
            return demo()
        if a.runs < 1 or a.jobs < 1:
            ap.error("--runs and --jobs must be at least 1")
        name, _ = check(a.repo, a.test, a.fix, base=a.base, runs=a.runs, jobs=a.jobs,
                        timeout=a.timeout, reason=a.reason, pristine=not a.keep_ignored, as_json=a.json)
        return 0 if name == "PROVEN" else 1
    except GitError as e:
        print(f"red_green_check: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
