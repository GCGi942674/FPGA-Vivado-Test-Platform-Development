#!/usr/bin/env python3
"""Locate a cached GalaxCore regression boundary (Python 3.6+)."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


def resolve_layout(script_path):
    """Resolve installation paths from test2/regression.py, never from cwd."""
    test2 = Path(script_path).resolve().parent
    if not (test2 / 'run.sh').is_file() or not (test2 / 'vivado_runner').is_dir():
        raise RuntimeError('Place regression.py in test2 beside run.sh and vivado_runner/')
    return test2, test2.parent


def resolve_testcase(value, test2):
    """Prefer testcase paths relative to test2; also accept cwd-relative paths."""
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        relative_to_test2 = test2 / candidate
        if relative_to_test2.exists():
            candidate = relative_to_test2
    candidate = candidate.resolve()
    if candidate.is_dir():
        candidate = candidate / 'run.tcl'
    if not candidate.is_file() or candidate.name != 'run.tcl':
        raise RuntimeError('Specify one testcase directory or run.tcl file under {}'.format(test2))
    try:
        candidate.relative_to(test2)
    except ValueError:
        raise RuntimeError('Testcase must be inside {}'.format(test2))
    return candidate


DEFAULT_VERSION_FILE = '/home/xshare/zhouwei_runcache/projectObj/GalaxCore/opt/success_version'


def parse_version_manifest(content):
    """Parse the complete cache manifest, not the abbreviated qkmk display.

    E revisions reuse unchanged source and are not separate cache candidates.
    Day versions extend the history beyond the rolling success cache.
    """
    groups, equivalent = {}, set()
    for label in ('success version', 'day_version'):
        headers = re.findall(r'^[ \t]*' + re.escape(label) + r'\b', content, re.MULTILINE)
        matches = re.findall(r'^[ \t]*' + re.escape(label) + r'\s*\[([^\]]*)\][ \t]*\r?$',
                             content, re.MULTILINE)
        if (len(headers) != len(matches) or len(matches) > 1 or
                (label == 'success version' and not matches)):
            raise RuntimeError('Invalid success_version file: expected one ' + label + ' list')
        versions = groups[label] = set()
        if not matches or not matches[0].strip():
            continue
        for item in matches[0].split(','):
            token = item.strip()
            if not re.fullmatch(r'[0-9]+E?', token):
                raise RuntimeError('Invalid version token in {}: {!r}; a complete manifest is required'.format(label, token))
            if token.endswith('E'):
                equivalent.add(int(token[:-1]))
            else:
                versions.add(int(token))
    result = sorted((groups['success version'] | groups['day_version']) - equivalent)
    if not result:
        raise RuntimeError('No usable cached versions in success_version file')
    return {'versions': result, 'day_versions': sorted(groups['day_version'] - equivalent)}


def read_candidates(path, current, snapshot):
    path = Path(path).expanduser()
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RuntimeError('Cannot read version file {}: {}'.format(path, exc))
    Path(snapshot).write_bytes(data)
    manifest = parse_version_manifest(data.decode('utf-8-sig'))
    # The checkout can be newer than the build cache or an equivalent E
    # revision. Do not ask qkmk to build an unavailable candidate.
    versions = [v for v in manifest['versions'] if v <= current]
    if not versions:
        raise RuntimeError('No usable cached version at or before checkout r{}'.format(current))
    return {'versions': versions,
            'day_versions': [v for v in manifest['day_versions'] if v < versions[-1]]}


def cached_test(test):
    """Only cache conclusive testcase results, never infrastructure errors."""
    results = {}

    def checked(version, phase):
        if version not in results:
            status = test(version, phase)
            if status not in ('PASS', 'FAIL'):
                raise RuntimeError('Inconclusive testcase status at r{}: {}'.format(version, status))
            results[version] = status
        return results[version]

    return checked


def bisect_boundary(versions, low, high, test):
    """Refine a tested PASS/FAIL bracket using available cache entries only."""
    while high - low > 1:
        mid = (low + high) // 2
        if test(versions[mid], 'BISECT') == 'PASS':
            low = mid
        else:
            high = mid
    return {'outcome': 'BOUNDARY', 'last_pass': versions[low],
            'first_fail': versions[high], 'uncached_gap': versions[high] - versions[low] > 1}


def locate(versions, step, test):
    """Search cached indices, assuming a single PASS to FAIL transition."""
    test = cached_test(test)
    high = len(versions) - 1
    if test(versions[high], 'BASE') == 'PASS':
        return {'outcome': 'BASE_PASS', 'base': versions[high]}
    while high > 0:
        low = max(0, high - step)
        if test(versions[low], 'BACKWARD') == 'PASS':
            break
        high = low
    else:
        return {'outcome': 'NO_PASS_IN_CACHE', 'oldest': versions[0]}
    return bisect_boundary(versions, low, high, test)


def locate_by_day(versions, day_versions, test):
    """Probe daily caches backwards, then bisect one PASS-to-FAIL interval."""
    test = cached_test(test)
    high = len(versions) - 1
    if test(versions[high], 'BASE') == 'PASS':
        return {'outcome': 'BASE_PASS', 'base': versions[high]}
    indices = {version: index for index, version in enumerate(versions)}
    days = sorted({indices[v] for v in day_versions if v in indices and v < versions[-1]},
                  reverse=True)
    for low in days:
        if test(versions[low], 'DAY') == 'PASS':
            return bisect_boundary(versions, low, high, test)
        high = low
    # With a single PASS-to-FAIL transition, the oldest available cache is
    # enough to bracket any boundary older than all failed daily checkpoints.
    # This also handles a manifest without any day versions.
    if high > 0 and test(versions[0], 'OLDEST') == 'PASS':
        return bisect_boundary(versions, 0, high, test)
    return {'outcome': 'NO_PASS_IN_CACHE', 'oldest': versions[0]}


def format_summary(result, current, case, count, records):
    """Render a readable terminal summary while keeping JSON machine-readable."""
    lines = ['', '=' * 60, 'Regression Search Result', '-' * 60, 'Testcase: {}'.format(case)]
    original = result.get('original_revision')
    base = result.get('test_base_revision')
    if original is not None and base is not None and original != base:
        lines.extend(['Original checkout: r{} (not tested; not a usable cache entry)'.format(original),
                      'Tested base: r{}'.format(base)])
    outcome = result['outcome']
    if outcome == 'BOUNDARY':
        if result.get('uncached_gap'):
            lines.extend([
                'Status: PASS/FAIL interval found',
                'Passing version: r{}'.format(result['last_pass']),
                'Failing version: r{}'.format(result['first_fail']),
                'Note: Uncached versions in this interval prevent an exact first-failure result.',
            ])
        else:
            lines.extend([
                'Status: First failing version identified (single-transition assumption)',
                'First failure: r{}  [FAIL]'.format(result['first_fail']),
                'Previous pass: r{}  [PASS]'.format(result['last_pass']),
                'Suggested check: Review the code changes in r{}.'.format(result['first_fail']),
            ])
    elif outcome == 'BASE_PASS':
        lines.extend(['Status: Starting cached version passed; failure not reproduced',
                      'Base version: r{}  [PASS]'.format(result['base']),
                      'Historical versions were not searched.'])
    elif outcome == 'NO_PASS_IN_CACHE':
        lines.extend(['Status: No passing version found; failure boundary unknown',
                      'Oldest tested: r{}  [FAIL]'.format(result['oldest']),
                      'Note: Earlier available versions are needed to continue.'])
    lines.extend(['-' * 60, 'Versions tested: {}'.format(count),
                  'Current checkout: r{} (not automatically restored)'.format(current),
                  'Records: {}'.format(records), '=' * 60])
    return '\n'.join(lines)


def color_summary(summary):
    """Add terminal-only ANSI colors; keep saved reports free of escapes."""
    if not sys.stdout.isatty() or os.environ.get('TERM') == 'dumb' or 'NO_COLOR' in os.environ:
        return summary
    colored = []
    for line in summary.split('\n'):
        code = None
        if '[FAIL]' in line or line.startswith(('First failure:', 'Failing version:')):
            code = '1;31'
        elif '[PASS]' in line or line.startswith(('Previous pass:', 'Passing version:')):
            code = '1;32'
        elif line.startswith(('Status:', 'Note:', 'Suggested check:')):
            code = '1;33'
        elif line == 'Regression Search Result' or line.startswith(('Current checkout:', 'Records:')):
            code = '1;36'
        elif line and set(line) <= {'=', '-'}:
            code = '36'
        colored.append('\033[{}m{}\033[0m'.format(code, line) if code else line)
    return '\n'.join(colored)


class Regression:
    def __init__(self, args):
        self.args = args
        self.test2, self.root = resolve_layout(__file__)
        self.case = resolve_testcase(args.testcase, self.test2)
        stamp = time.strftime('%Y%m%d_%H%M%S') + '_{}'.format(os.getpid())
        self.output = self.test2 / 'vivado_runner' / 'regression_results' / stamp
        self.output.mkdir(parents=True)
        self.rows = []
        self.strategy = 'step' if args.step is not None else 'day'
        self.current = None
        self.original_revision = None
        self.test_base_revision = None
        self.lock = None

    def command(self, argv, cwd, log, env=None):
        started = time.time()
        with open(str(log), 'wb') as stream:
            proc = subprocess.Popen(argv, cwd=str(cwd), stdout=stream,
                                    stderr=subprocess.STDOUT, env=env,
                                    start_new_session=True)
            try:
                while proc.poll() is None:
                    if sys.stdout.isatty():
                        print('\r  running {:5d}s | {}'.format(int(time.time() - started), log.name),
                              end='', flush=True)
                    time.sleep(0.3)
            except BaseException:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                raise
            finally:
                if sys.stdout.isatty():
                    print()
        output = log.read_text(errors='replace')
        if proc.returncode:
            raise RuntimeError('Command exited {}: {} (log: {})'.format(
                proc.returncode, ' '.join(argv), log))
        return output

    def revision(self):
        output = self.command(['svn', 'info', '--xml', str(self.root)], self.root,
                              self.output / 'svn_info.log')
        return int(ET.fromstring(output).find('entry').get('revision'))

    def save(self, result=None):
        data = {'case': str(self.case), 'step': self.args.step,
                'strategy': self.strategy, 'versions_file': self.args.versions_file,
                'original_revision': self.original_revision, 'test_base_revision': self.test_base_revision,
                'current_revision': self.current, 'tests': self.rows,
                'result': result, 'assumption': 'Single PASS-to-FAIL transition in cached versions'}
        (self.output / 'results.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
        lines = ['VERSION\tSTATUS\tPHASE\tSECONDS\tREASON']
        for row in self.rows:
            lines.append('{version}\t{status}\t{phase}\t{seconds}\t{reason}'.format(**row))
        if result:
            lines.append(json.dumps(result, sort_keys=True))
        (self.output / 'results.tsv').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def test(self, version, phase):
        folder = self.output / str(version)
        folder.mkdir()
        started = time.time()
        runner_started = False
        row = dict(version=version, status='ERROR', phase=phase, seconds=0, reason='INCOMPLETE')
        self.rows.append(row)
        print('[{}] r{} | {}'.format(len(self.rows), version, phase), flush=True)
        try:
            if version != self.current:
                self.command(['svn', 'up', '-r', str(version), '--non-interactive'], self.root,
                             folder / 'svn_update.log')
                self.current = self.revision()
                if self.current != version:
                    raise RuntimeError('SVN revision mismatch')
                conflicts = self.command(['svn', 'status', '--xml'], self.root,
                                         folder / 'svn_status.log')
                for node in ET.fromstring(conflicts).iter('wc-status'):
                    if node.get('item') == 'conflicted' or node.get('props') == 'conflicted' or node.get('tree-conflicted') == 'true':
                        raise RuntimeError('SVN conflict; resolve manually before continuing')
            output = self.command(['csh', '-ic', 'qkmk'], self.root, folder / 'qkmk.log')
            marker = 'Quick make completed successfully for version {}.'.format(version)
            if marker not in output:
                raise RuntimeError('qkmk did not confirm requested version {}'.format(version))
            runtime = self.snapshot / 'vivado_runner' / 'runtime'
            # Remove only prior results in this newly created private snapshot.
            for old in (runtime / 'status').rglob('result.env'):
                old.unlink()
            env = os.environ.copy()
            env['RUN_SH_LOCK_HELD'] = '1'
            env['RUN_SH_LOCK_FILE'] = str(self.lock_path)
            env['GALAXCORE_WORKSPACE_ROOT'] = str(self.test2)
            runner_started = True
            self.command(['bash', str(self.snapshot / 'run.sh'), str(self.case),
                          '--flow-config', str(self.snapshot / 'flow_config'), '--bg', '1',
                          '--timeout', str(self.args.timeout), '--galaxcore',
                          str(self.root / 'bin' / 'Linux_64' / 'GalaxCore')],
                         self.test2, folder / 'runner.log', env)
            results = list((runtime / 'status').rglob('result.env'))
            if len(results) != 1:
                raise RuntimeError('Expected one fresh result.env, found {}'.format(len(results)))
            fields = {}
            for line in results[0].read_text().splitlines():
                key, sep, value = line.partition('=')
                if sep:
                    fields[key] = value
            status = fields.get('STATUS')
            if status not in ('PASS', 'FAIL'):
                raise RuntimeError('Inconclusive testcase status: {}'.format(status))
            if fields.get('REASON') in ('MISSING_LOG', 'RUN_LOG_CAPTURE_FAILED', 'LOG_LIMIT_REACHED'):
                raise RuntimeError('Test infrastructure error: {}'.format(fields.get('REASON')))
            row['status'] = status
            row['reason'] = fields.get('REASON', 'UNKNOWN')
            return status
        except Exception as exc:
            row['reason'] = str(exc)
            raise
        finally:
            runtime = self.snapshot / 'vivado_runner' / 'runtime'
            if runner_started and runtime.exists():
                shutil.copytree(str(runtime), str(folder / 'runtime'))
            row['seconds'] = round(time.time() - started, 1)
            self.save()
            print('  r{version} {status} | {reason} | {seconds}s'.format(**row), flush=True)

    def run(self):
        import fcntl
        self.lock_path = self.root / '.regression.lock'
        self.lock = open(str(self.lock_path), 'a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        tag = next((os.environ[name] for name in (
            'DTS_SLOT_WORKER', 'PJTEST_SLOT_WORKER', 'DTS_WORKER',
            'GALAXCORE_WORKER_NAME', 'USER') if os.environ.get(name)), 'unknown')
        tag = re.sub(r'[^A-Za-z0-9_.-]', '_', tag)
        lock_dir = Path(os.environ.get('RUN_SH_LOCK_DIR', str(Path.home() / 'PJTest' / 'tmp')))
        runner_lock_path = Path(os.environ.get('RUN_SH_LOCK_FILE',
                                              str(lock_dir / ('galaxcore_run_' + tag + '.lock'))))
        runner_lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.runner_lock = open(str(runner_lock_path), 'a')
        fcntl.flock(self.runner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Pin the runner and user's flow configuration across SVN updates.
        self.snapshot = self.output / 'snapshot' / 'test2'
        self.snapshot.mkdir(parents=True)
        shutil.copy2(str(self.test2 / 'run.sh'), str(self.snapshot / 'run.sh'))
        shutil.copy2(str(self.test2 / 'flow_config'), str(self.snapshot / 'flow_config'))
        for name in ('lib', 'config', 'templates'):
            shutil.copytree(str(self.test2 / 'vivado_runner' / name),
                            str(self.snapshot / 'vivado_runner' / name),
                            ignore=shutil.ignore_patterns('__pycache__'))
        self.current = self.revision()
        self.original_revision = self.current
        try:
            candidates = read_candidates(self.args.versions_file, self.current,
                                         self.output / 'success_version.snapshot')
            versions, days = candidates['versions'], candidates['day_versions']
            self.test_base_revision = versions[-1]
            (self.output / 'candidate_versions.json').write_text(json.dumps(candidates, indent=2))
            mode = 'day checkpoints, then bisect' if self.strategy == 'day' else 'step {}'.format(self.args.step)
            print('Version file: {}'.format(self.args.versions_file), flush=True)
            print('Case: {}\nOriginal checkout: r{}\nBase: r{} | candidates (including base): {} | day checkpoints: {}\nSearch: {}\nLogs: {}'.format(
                self.case, self.original_revision, self.test_base_revision,
                len(versions), len(days), mode, self.output), flush=True)
            if self.original_revision != self.test_base_revision:
                print('r{} has no separate usable cache entry; starting from r{}.'.format(
                    self.original_revision, self.test_base_revision), flush=True)
            if self.strategy == 'day':
                if not days:
                    print('No earlier day checkpoints; bracket with the oldest available cache.', flush=True)
                result = locate_by_day(versions, days, self.test)
            else:
                result = locate(versions, self.args.step, self.test)
            result.update(original_revision=self.original_revision, test_base_revision=self.test_base_revision)
            self.save(result)
            summary = format_summary(result, self.current, self.case,
                                     len(self.rows), self.output / 'results.tsv')
            (self.output / 'summary.txt').write_text(summary + '\n', encoding='utf-8')
            print(color_summary(summary))
            return 0 if result['outcome'] == 'BOUNDARY' else 2
        except BaseException as exc:
            self.save({'outcome': 'STOPPED', 'reason': str(exc) or type(exc).__name__})
            raise


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('testcase', help='One testcase directory or run.tcl')
    parser.add_argument('step', nargs='?', type=int,
                        help='Use legacy cached-version stride; omit for day checkpoints followed by bisection')
    parser.add_argument('--versions-file', default=DEFAULT_VERSION_FILE,
                        help='Complete success_version manifest (default: %(default)s)')
    parser.add_argument('--timeout', type=int, default=10800)
    args = parser.parse_args(argv)
    if (args.step is not None and args.step < 1) or args.timeout < 1:
        parser.error('step and timeout must be positive')
    return args


def main():
    args = parse_args()
    if os.name != 'posix':
        print('ERROR: Run this command on the Linux GalaxCore workstation', file=sys.stderr)
        return 2
    def interrupted(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    try:
        return Regression(args).run()
    except KeyboardInterrupt:
        print('\nInterrupted. Checkout remains at the last selected revision.', file=sys.stderr)
        return 130
    except Exception as exc:
        print('ERROR: {}'.format(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
