#!/usr/bin/env python3
"""Exercise workspace isolation with a private runner and a gated fake tool.

Run on Linux with:
    python3 -B -m unittest discover -s vivado_runner/tests -p test_workspace_runtime.py -v

Every runtime, testcase, lock, and subprocess log lives in a TemporaryDirectory.
The fake GalaxCore never reads or executes a real testcase.
"""

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
REQUIRED_TOOLS = ("bash", "flock", "setsid", "python3", "ps", "awk", "sed", "find")
REPORT_NAMES = (
    "runTime_Summary", "list_fail_to_run", "stat_summary",
    "execution_report.txt", "execution_report.json",
)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux process groups are required")
class WorkspaceRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        missing = [name for name in REQUIRED_TOOLS if shutil.which(name) is None]
        if missing:
            raise unittest.SkipTest("Missing Linux tools: " + ", ".join(missing))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="runner-workspaces-")
        self.root = Path(self.temporary.name)
        self.checkout = self.root / "shared-runner" / "test2"
        self.checkout.mkdir(parents=True)
        self.runner = self.checkout / "vivado_runner"
        self.processes = []
        self.workspaces = []
        self.sequence = 0
        for name in ("run.sh", "flow_config"):
            shutil.copy2(str(REPOSITORY / name), str(self.checkout / name))
        for name in ("lib", "config", "templates"):
            shutil.copytree(
                str(REPOSITORY / "vivado_runner" / name),
                str(self.runner / name),
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        # A Windows checkout may contain CRLF scripts. Normalize only the
        # private Linux fixture, never the source checkout.
        for path in self.checkout.rglob("*"):
            if path.is_file() and (path.suffix in (".sh", ".conf", ".template", ".py")
                                   or path.name == "flow_config"):
                content = path.read_text(encoding="utf-8")
                with path.open("w", encoding="utf-8", newline="\n") as stream:
                    stream.write(content)

        # All template keys are present so ensure_flow_config_complete cannot
        # silently append an enabled comparison module during a test.
        keys = []
        template = self.runner / "templates" / "flow_config.template"
        for line in template.read_text().splitlines():
            fields = line.split("#", 1)[0].split()
            if fields:
                keys.append(fields[0])
        self.flow = self.checkout / "flow_config"
        self.flow.write_text("".join(key + " 0\n" for key in keys))

        self.fake_tool = self.root / "fake-galaxcore"
        self.fake_tool.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import os
            from pathlib import Path
            import signal
            import sys
            import time

            case = Path.cwd()

            def stop(signum, frame):
                (case / '.fake_signal').write_text(str(signum))
                sys.exit(128 + signum)

            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            (case / '.fake_pid').write_text(str(os.getpid()))
            (case / '.fake_started').write_text(str(case))
            print('Fake workspace: ' + str(case.parent), flush=True)
            while not (case / '.fake_release').exists():
                time.sleep(0.05)
            (case / '.fake_completed').write_text('done')
            print('Runtime: 1', flush=True)
            """))
        self.fake_tool.chmod(0o755)

    def tearDown(self):
        # Test failures must not leave the runner's detached case sessions alive.
        for item in self.processes:
            if item["process"].poll() is None:
                item["process"].send_signal(signal.SIGTERM)
        for item in self.processes:
            proc = item["process"]
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
            item["stream"].close()
        for workspace in self.workspaces:
            pid_file = workspace / "case1" / ".fake_pid"
            if pid_file.is_file():
                pid = int(pid_file.read_text())
                command = Path("/proc") / str(pid) / "cmdline"
                try:
                    # Check ownership before killing a detached process by PID.
                    if str(self.fake_tool).encode() in command.read_bytes().split(b"\0"):
                        os.kill(pid, signal.SIGKILL)
                except (FileNotFoundError, ProcessLookupError):
                    pass
        self.temporary.cleanup()

    def workspace(self, name):
        workspace = self.root / name / "test2"
        case = workspace / "case1"
        case.mkdir(parents=True)
        (case / "run.tcl").write_text("# The fake tool does not execute Tcl.\n")
        self.workspaces.append(workspace)
        return workspace

    def environment(self, workspace, namespace=None):
        prefixes = ("PJTEST_", "DTS_", "GALAXCORE_", "VIVADO_RUNNER_", "RUN_SH_")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(prefixes)}
        env.update({
            "HOME": str(self.root / "home"),
            "USER": "workspace_runtime_test",
            "RUN_SH_LOCK_DIR": str(self.root / "locks"),
            "GALAXCORE_WORKSPACE_ROOT": str(workspace),
            "GALAXCORE_MAX_CASE_LOG_MB": "1",
            "GALAXCORE_SUPPRESS_PIN_REDEFINITION": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LC_ALL": "C",
        })
        if namespace is not None:
            env["VIVADO_RUNNER_NAMESPACE"] = namespace
        return env

    def identity(self, workspace, namespace=None):
        completed = subprocess.run(
            ["bash", "-c", 'source "$1"; printf "%s\\n" "$WORKSPACE_ROOT" '
             '"$WORKSPACE_ID" "$RUNTIME_DIR"', "_",
             str(self.runner / "lib" / "bash" / "common.sh")],
            env=self.environment(workspace, namespace), cwd=str(self.checkout),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = completed.stdout.splitlines()
        self.assertEqual(len(lines), 3, completed.stdout)
        return lines[0], lines[1], Path(lines[2])

    def runtime(self, workspace, namespace=None):
        runtime = self.identity(workspace, namespace)[2]
        self.assertEqual(runtime.parent, self.runner / "runtime" / "workspaces")
        return runtime

    def start(self, workspace, namespace=None, timeout=60, clean=False, copy=False):
        self.sequence += 1
        log = self.root / ("runner-{}.log".format(self.sequence))
        stream = log.open("wb")
        command = [
            "bash", str(self.checkout / "run.sh"), str(workspace / "case1" / "run.tcl"),
            "--flow-config", str(self.flow), "--bg", "1", "--timeout", str(timeout),
            "--galaxcore", str(self.fake_tool),
        ]
        if clean:
            command.append("--clean-runtime")
        if copy:
            command.extend(["--copy", "--report-dst", str(self.root / "exports")])
        proc = subprocess.Popen(
            command, cwd=str(self.checkout), env=self.environment(workspace, namespace),
            stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
        )
        item = {"process": proc, "log": log, "stream": stream, "workspace": workspace}
        self.processes.append(item)
        return item

    def log_text(self, item):
        return item["log"].read_text(errors="replace")

    def wait_started(self, item):
        marker = item["workspace"] / "case1" / ".fake_started"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if marker.is_file():
                self.assertIsNone(item["process"].poll(), self.log_text(item))
                return
            if item["process"].poll() is not None:
                self.fail("Runner exited before starting fake tool:\n" + self.log_text(item))
            time.sleep(0.05)
        self.fail("Fake tool did not start:\n" + self.log_text(item))

    def finish(self, item, expected_returncode=0, release=True):
        if release:
            (item["workspace"] / "case1" / ".fake_release").write_text("finish\n")
        try:
            returncode = item["process"].wait(timeout=25)
        except subprocess.TimeoutExpired:
            self.fail("Runner did not finish:\n" + self.log_text(item))
        self.assertEqual(returncode, expected_returncode, self.log_text(item))

    def fake_is_alive(self, workspace):
        pid_file = workspace / "case1" / ".fake_pid"
        if not pid_file.is_file():
            return False
        stat = Path("/proc") / pid_file.read_text() / "stat"
        try:
            return stat.read_text().rpartition(")")[2].split()[0] != "Z"
        except FileNotFoundError:
            return False

    def assert_still_running(self, item):
        self.assertIsNone(item["process"].poll(), self.log_text(item))
        self.assertTrue(self.fake_is_alive(item["workspace"]), self.log_text(item))
        self.assertFalse((item["workspace"] / "case1" / ".fake_signal").exists())
        self.assertFalse((item["workspace"] / "case1" / ".fake_completed").exists())

    def result(self, runtime):
        path = runtime / "status" / "case1" / "result.env"
        self.assertTrue(path.is_file(), str(path))
        fields = {}
        for line in path.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator:
                decoded = shlex.split(value)
                fields[key] = decoded[0] if decoded else ""
        return fields

    def assert_artifacts(self, workspace, expected_status="PASS", namespace=None):
        runtime = self.runtime(workspace, namespace)
        fields = self.result(runtime)
        self.assertEqual(fields["STATUS"], expected_status)
        self.assertEqual(Path(fields["RUN_TCL"]), workspace.resolve() / "case1" / "run.tcl")
        for name in ("logs", "status", "tmp", "cache", "reports/latest", "reports/archive"):
            self.assertTrue((runtime / name).is_dir(), str(runtime / name))
        for name in REPORT_NAMES:
            self.assertTrue((runtime / "reports" / "latest" / name).is_file(), name)
        self.assertTrue(list((runtime / "reports" / "archive").glob("*/execution_report.json")))
        payload = json.loads((runtime / "reports" / "latest" / "execution_report.json").read_text())
        self.assertEqual(payload["meta"]["workspace_root"], str(workspace.resolve()))
        self.assertEqual(payload["meta"]["workspace_id"], self.identity(workspace, namespace)[1])
        self.assertEqual(payload["meta"]["runtime_namespace"], runtime.name)
        self.assertEqual(payload["meta"]["runtime_dir"], str(runtime))
        self.assertEqual(len(payload["records"]), 1)
        self.assertEqual(payload["records"][0]["STATUS"], expected_status)
        self.assertEqual((runtime / "tmp" / "pid_map.txt").read_text(), "")
        self.assertIn(str(workspace.resolve()), (runtime / "tmp" / "case_list.txt").read_text())
        if expected_status == "PASS":
            self.assertIn("Runtime: 1", (runtime / "status" / "case1" / "run.log").read_text())
        return runtime

    def test_different_workspaces_run_same_relative_case_concurrently(self):
        workspace_a = self.workspace("branch_A")
        workspace_b = self.workspace("branch_B")
        item_a = self.start(workspace_a, copy=True)
        self.wait_started(item_a)
        item_b = self.start(workspace_b, copy=True)
        self.wait_started(item_b)
        self.assert_still_running(item_a)
        self.assert_still_running(item_b)
        runtime_a = self.runtime(workspace_a)
        runtime_b = self.runtime(workspace_b)
        self.assertNotEqual(runtime_a, runtime_b)
        for runtime, own, other in ((runtime_a, workspace_a, workspace_b),
                                    (runtime_b, workspace_b, workspace_a)):
            for name in ("case_list.txt", "pid_map.txt"):
                content = (runtime / "tmp" / name).read_text()
                self.assertIn(str(own), content)
                self.assertNotIn(str(other), content)
            self.assertTrue(list((runtime / "tmp").glob("*.out")))
        map_b = (runtime_b / "tmp" / "pid_map.txt").read_text()
        self.finish(item_a)
        self.assert_still_running(item_b)
        self.assertEqual((runtime_b / "tmp" / "pid_map.txt").read_text(), map_b)
        self.finish(item_b)
        for workspace, other in ((workspace_a, workspace_b), (workspace_b, workspace_a)):
            runtime = self.assert_artifacts(workspace)
            self.assertNotIn(str(other), (runtime / "tmp" / "case_results.raw").read_text())
            self.assertNotIn(str(other), (runtime / "logs" / "run.log").read_text())
            legacy = self.runner / "runtime" / "status" / runtime.name / "case1" / "result.env"
            self.assertEqual(legacy.read_bytes(), (runtime / "status" / "case1" / "result.env").read_bytes())
        exports = list((self.root / "exports").glob("*/*/execution_report.json"))
        self.assertEqual(len(exports), 2)
        self.assertEqual(
            {json.loads(path.read_text())["meta"]["workspace_root"] for path in exports},
            {str(workspace_a), str(workspace_b)},
        )

    def test_same_workspace_cannot_bypass_lock_with_another_namespace(self):
        workspace = self.workspace("branch_A")
        first = self.start(workspace)
        self.wait_started(first)
        pid_before = (workspace / "case1" / ".fake_pid").read_text()
        for namespace in (None, "another_namespace"):
            with self.subTest(namespace=namespace):
                second = self.start(workspace, namespace=namespace)
                self.finish(second, expected_returncode=75, release=False)
                self.assertIn("lock", self.log_text(second).lower())
                self.assert_still_running(first)
                self.assertEqual((workspace / "case1" / ".fake_pid").read_text(), pid_before)
        self.finish(first)
        self.assert_artifacts(workspace)

    def test_realpath_identity_and_explicit_namespace(self):
        workspace = self.workspace("branch with spaces")
        alias = self.root / "workspace_alias"
        alias.symlink_to(workspace, target_is_directory=True)
        canonical = self.identity(workspace)
        self.assertEqual(self.identity(alias), canonical)
        self.assertEqual(canonical[0], str(workspace.resolve()))
        digest = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:16]
        self.assertTrue(canonical[1].endswith("_" + digest), canonical[1])
        self.assertRegex(canonical[1], r"^[A-Za-z0-9_.-]+$")
        self.assertEqual(self.identity(workspace, ""), canonical)
        overridden = self.identity(alias, "qa_A.1")
        self.assertEqual(overridden[:2], canonical[:2])
        self.assertEqual(overridden[2].name, "qa_A.1")
        first = self.start(workspace)
        self.wait_started(first)
        second = self.start(alias, namespace="alias_namespace")
        self.finish(second, expected_returncode=75, release=False)
        self.assert_still_running(first)
        self.finish(first)
        self.assert_artifacts(workspace)

    def test_invalid_namespace_is_rejected_before_case_execution(self):
        workspace = self.workspace("branch_A")
        for namespace in ("../escape", "with/slash", "with space", ".", "..", "bad\nname"):
            with self.subTest(namespace=namespace):
                item = self.start(workspace, namespace=namespace)
                code = item["process"].wait(timeout=10)
                self.assertNotEqual(code, 0, self.log_text(item))
                self.assertFalse((workspace / "case1" / ".fake_started").exists())

    def test_different_workspaces_cannot_claim_same_explicit_namespace(self):
        workspace_a = self.workspace("branch_A")
        workspace_b = self.workspace("branch_B")
        first = self.start(workspace_a, namespace="fixed_namespace")
        self.wait_started(first)
        runtime = self.runtime(workspace_a, "fixed_namespace")
        case_list = (runtime / "tmp" / "case_list.txt").read_bytes()
        second = self.start(workspace_b, namespace="fixed_namespace")
        code = second["process"].wait(timeout=10)
        self.assertNotEqual(code, 0, self.log_text(second))
        self.assertFalse((workspace_b / "case1" / ".fake_started").exists())
        self.assertEqual((runtime / "tmp" / "case_list.txt").read_bytes(), case_list)
        self.assert_still_running(first)
        self.finish(first)
        self.assert_artifacts(workspace_a, namespace="fixed_namespace")

    def test_clean_runtime_preserves_another_running_workspace(self):
        workspace_a = self.workspace("branch_A")
        workspace_b = self.workspace("branch_B")
        runtime_a = self.runtime(workspace_a)
        stale = []
        for name in ("tmp", "cache", "status", "reports/archive"):
            old = runtime_a / name / "old_sentinel"
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_text("old A data")
            stale.append(old)
        item_b = self.start(workspace_b)
        self.wait_started(item_b)
        runtime_b = self.runtime(workspace_b)
        sentinel_b = runtime_b / "cache" / "keep_sentinel"
        sentinel_b.write_text("live B data")
        map_b = (runtime_b / "tmp" / "pid_map.txt").read_bytes()
        item_a = self.start(workspace_a, clean=True)
        self.wait_started(item_a)
        for path in stale:
            self.assertFalse(path.exists(), str(path))
        self.finish(item_a)
        self.assert_still_running(item_b)
        self.assertEqual(sentinel_b.read_text(), "live B data")
        self.assertEqual((runtime_b / "tmp" / "pid_map.txt").read_bytes(), map_b)
        self.finish(item_b)
        self.assert_artifacts(workspace_a)
        self.assert_artifacts(workspace_b)

    def assert_signal_isolated(self, signum):
        workspace_a = self.workspace("branch_A")
        workspace_b = self.workspace("branch_B")
        item_a = self.start(workspace_a)
        self.wait_started(item_a)
        item_b = self.start(workspace_b)
        self.wait_started(item_b)
        item_a["process"].send_signal(signum)
        self.finish(item_a, expected_returncode=130, release=False)
        self.assertFalse(self.fake_is_alive(workspace_a), self.log_text(item_a))
        self.assert_still_running(item_b)
        self.assert_artifacts(workspace_a, "INTERRUPTED")
        self.finish(item_b)
        self.assert_artifacts(workspace_b)

    def test_sigint_does_not_interrupt_another_workspace(self):
        self.assert_signal_isolated(signal.SIGINT)

    def test_sigterm_does_not_interrupt_another_workspace(self):
        self.assert_signal_isolated(signal.SIGTERM)

    def test_timeout_does_not_interrupt_another_workspace(self):
        workspace_a = self.workspace("branch_A")
        workspace_b = self.workspace("branch_B")
        item_b = self.start(workspace_b)
        self.wait_started(item_b)
        item_a = self.start(workspace_a, timeout=2)
        self.wait_started(item_a)
        self.finish(item_a, release=False)
        self.assertFalse(self.fake_is_alive(workspace_a), self.log_text(item_a))
        self.assert_still_running(item_b)
        runtime_a = self.assert_artifacts(workspace_a, "TIMEOUT")
        self.assertEqual(self.result(runtime_a)["REASON"], "TIME_LIMIT_REACHED")
        self.finish(item_b)
        self.assert_artifacts(workspace_b)

    def test_legacy_status_path_remains_readable_for_one_call(self):
        workspace = self.workspace("branch_A")
        old_status = self.runner / "runtime" / "status"
        old_status.mkdir(parents=True)
        (old_status / "previous-result.txt").write_text("preserve old results")
        item = self.start(workspace)
        self.wait_started(item)
        self.finish(item)
        runtime = self.assert_artifacts(workspace)
        legacy = self.runner / "runtime" / "status" / runtime.name / "case1" / "result.env"
        self.assertTrue(legacy.is_file())
        self.assertFalse((self.runner / "runtime" / "status").is_symlink())
        self.assertEqual(legacy.read_bytes(), (runtime / "status" / "case1" / "result.env").read_bytes())
        backups = list((self.runner / "runtime" / "legacy").glob("status.*/previous-result.txt"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "preserve old results")

    def test_existing_discovery_and_judgment_suites(self):
        tests = self.runner / "tests"
        tests.mkdir()
        for name in ("test_discover.sh", "test_judge.sh"):
            with self.subTest(script=name):
                source = REPOSITORY / "vivado_runner" / "tests" / name
                target = tests / name
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                completed = subprocess.run(
                    ["bash", str(target)], cwd=str(self.checkout),
                    env=self.environment(self.checkout), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, timeout=30,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout)


if __name__ == "__main__":
    unittest.main()
