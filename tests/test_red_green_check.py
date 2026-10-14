import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import red_green_check as rgc  # noqa: E402


def counts(red=0, other=0, green=0):
    return {"red": red, "other": other, "green": green}


def repo_with(commits):
    """A git repo built from [(message, {path: text})] commits, oldest first."""
    d = tempfile.mkdtemp(prefix="rgc-test-")
    rgc.git(d, "init", "-q")
    for msg, files in commits:
        for rel, text in files.items():
            os.makedirs(os.path.join(d, os.path.dirname(rel)) or d, exist_ok=True)
            with open(os.path.join(d, rel), "w", encoding="utf-8") as fh:
                fh.write(text)
        rgc.git(d, "add", "-A")
        rgc.git(d, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", msg)
    return d


def run_check(repo, **kw):
    out = io.StringIO()
    name, report = rgc.check(repo, "python3 -m unittest -q tests.test_webhook", "HEAD", out=out, **kw)
    return name, report, out.getvalue()


class Verdicts(unittest.TestCase):
    def test_each_verdict(self):
        self.assertEqual(rgc.verdict(counts(red=3), counts(green=3), 3)[0], "PROVEN")
        self.assertEqual(rgc.verdict(counts(green=3), counts(green=3), 3)[0], "NOT A REPRO")
        self.assertEqual(rgc.verdict(counts(red=3), counts(red=3), 3)[0], "FIX INCOMPLETE")
        self.assertEqual(rgc.verdict(counts(red=2, green=1), counts(green=3), 3)[0], "FLAKY")
        self.assertEqual(rgc.verdict(counts(red=3), counts(red=1, green=2), 3)[0], "FLAKY")

    def test_flaky_suggests_more_runs_only_below_100(self):
        self.assertIn("--runs 100", rgc.verdict(counts(red=2, green=1), counts(green=3), 3)[1])
        self.assertNotIn("--runs 100", rgc.verdict(counts(red=60, green=40), counts(green=100), 100)[1])

    def test_failing_for_another_reason_is_no_reproduction(self):
        name, why = rgc.verdict(counts(other=3), counts(green=3), 3)
        self.assertEqual(name, "NOT A REPRO")
        self.assertIn("never for the stated reason", why)

    def test_the_proven_line_is_exact(self):
        self.assertEqual(rgc.verdict(counts(red=3), counts(green=3), 3)[1],
                         "the test fails 3 of 3 runs before the fix and passes 3 of 3 after.")


class TestPaths(unittest.TestCase):
    def test_conventions(self):
        for p in ["tests/test_x.py", "test/a.py", "pkg/test_y.py", "x_test.go", "a.spec.ts", "b.test.jsx"]:
            self.assertTrue(rgc.TEST_PATH.search(p), p)
        for p in ["shop.py", "contest.py", "latest/notes.md", "src/testing_utils.py"]:
            self.assertFalse(rgc.TEST_PATH.search(p), p)


class DemoCases(unittest.TestCase):
    """The README's known cases, end to end."""

    def case(self, name):
        repo, test, runs = rgc.build_demo_repo(name)
        try:
            out = io.StringIO()
            verdict, _ = rgc.check(repo, test, "HEAD", runs=runs, out=out)
            return verdict, out.getvalue().splitlines()[-1]
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_proven(self):
        self.assertEqual(self.case("proven")[1],
                         "VERDICT: PROVEN: the test fails 3 of 3 runs before the fix and passes 3 of 3 after.")

    def test_not_a_repro(self):
        self.assertEqual(self.case("not-a-repro")[1],
                         "VERDICT: NOT A REPRO: the test passes 3 of 3 runs before the fix, so it never showed the bug.")

    def test_incomplete(self):
        self.assertEqual(self.case("incomplete")[1],
                         "VERDICT: FIX INCOMPLETE: the test fails 3 of 3 runs before the fix and still fails 3 of 3 after.")

    def test_race(self):
        self.assertEqual(self.case("race")[0], "FLAKY")

    def test_demo_exits_zero_when_every_case_earns_its_verdict(self):
        self.assertEqual(rgc.demo(out=io.StringIO()), 0)


class TestInTheFixCommit(unittest.TestCase):
    """An agent that adds the test and the fix in one commit: the test still runs on the old code."""

    def test_new_test_is_carried_back(self):
        repo = repo_with([
            ("bug", {"shop.py": rgc.SHOP_BUG}),
            ("test and fix together", {"tests/__init__.py": "", "tests/test_webhook.py": rgc.TEST_TWICE,
                                       "shop.py": rgc.FIXES["proven"]}),
        ])
        try:
            name, report, out = run_check(repo)
            self.assertEqual(name, "PROVEN")
            self.assertIn("tests/test_webhook.py", report["tests_added_by_fix"])
            self.assertIn("note   : the fix adds", out)
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_a_weakened_test_is_named(self):
        repo = repo_with([
            ("bug", {"shop.py": rgc.SHOP_BUG}),
            ("reproduce", {"tests/__init__.py": "", "tests/test_webhook.py": rgc.TEST_TWICE}),
            ("'fix': send the event once in the test", {"tests/test_webhook.py": rgc.TEST_ONCE}),
        ])
        try:
            name, report, out = run_check(repo)
            self.assertEqual(name, "NOT A REPRO")
            self.assertEqual(report["tests_edited_by_fix"], ["tests/test_webhook.py"])
            self.assertIn("warning: the fix also edits tests/test_webhook.py", out)
        finally:
            shutil.rmtree(repo, ignore_errors=True)


class Reason(unittest.TestCase):
    def test_a_red_run_for_another_reason_does_not_reproduce(self):
        # The test imports a guard only the fix adds, so before the fix it dies on ImportError.
        test = rgc.TEST_TWICE.replace("from shop import Shop", "from shop import Shop, dedupe_key")
        fixed = rgc.FIXES["proven"] + "\n\ndef dedupe_key(event):\n    return event[\"id\"]\n"
        repo = repo_with([
            ("bug", {"shop.py": rgc.SHOP_BUG}),
            ("reproduce", {"tests/__init__.py": "", "tests/test_webhook.py": test}),
            ("fix", {"shop.py": fixed}),
        ])
        try:
            self.assertEqual(run_check(repo)[0], "PROVEN")
            self.assertEqual(run_check(repo, reason="AssertionError")[0], "NOT A REPRO")
        finally:
            shutil.rmtree(repo, ignore_errors=True)


class Cli(unittest.TestCase):
    def test_json_and_exit_codes(self):
        repo, test, _ = rgc.build_demo_repo("proven")
        try:
            out = io.StringIO()
            sys_stdout, sys.stdout = sys.stdout, out
            try:
                code = rgc.main(["check", "--repo", repo, "--test", test, "--fix", "HEAD", "--json"])
            finally:
                sys.stdout = sys_stdout
            self.assertEqual(code, 0)
            report = json.loads(out.getvalue())
            self.assertEqual(report["verdict"], "PROVEN")
            self.assertEqual(report["before"], {"red": 3, "other": 0, "green": 0})
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_a_bad_commit_is_a_usage_error(self):
        repo, test, _ = rgc.build_demo_repo("proven")
        try:
            err = io.StringIO()
            sys_stderr, sys.stderr = sys.stderr, err
            try:
                code = rgc.main(["check", "--repo", repo, "--test", test, "--fix", "no-such-commit"])
            finally:
                sys.stderr = sys_stderr
            self.assertEqual(code, 2)
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    def test_parallel_jobs_give_the_same_verdict(self):
        repo, test, _ = rgc.build_demo_repo("proven")
        try:
            name, _ = rgc.check(repo, test, "HEAD", runs=4, jobs=2, out=io.StringIO())
            self.assertEqual(name, "PROVEN")
        finally:
            shutil.rmtree(repo, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
