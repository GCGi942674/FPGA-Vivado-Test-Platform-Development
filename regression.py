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


def cached_versions(output):
    output = re.sub(r'\x1b\[[0-9;]*m', '', output)
    match = re.search(r'All available success versions:\s*\[([^]]*)\]', output)
    if not match:
        raise RuntimeError('Cannot parse qkmk -v success versions')
    return sorted(set(int(v) for v in re.findall(r'\d+', match.group(1))))


def locate(versions, step, test):
    """Search cached indices, assuming a single PASS to FAIL transition."""
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
    while high - low > 1:
        mid = (low + high) // 2
        if test(versions[mid], 'BISECT') == 'PASS':
            low = mid
        else:
            high = mid
    return {'outcome': 'BOUNDARY', 'last_pass': versions[low],
            'first_fail': versions[high], 'uncached_gap': versions[high] - versions[low] > 1}


class Regression:
    def __init__(self, args):
        self.args = args
        self.test2, self.root = resolve_layout(__file__)
        self.case = resolve_testcase(args.testcase, self.test2)
        stamp = time.strftime('%Y%m%d_%H%M%S') + '_{}'.format(os.getpid())
        self.output = self.test2 / 'vivado_runner' / 'regression_results' / stamp
        self.output.mkdir(parents=True)
        self.rows = []
        self.current = None
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
        output = self.command(['csh', '-ic', 'qkmk -v'], self.root, self.output / 'versions.log')
        versions = [v for v in cached_versions(output) if v < self.current] + [self.current]
        print('Case: {}\nBase: r{} | cached candidates: {} | step: {}\nLogs: {}'.format(
            self.case, self.current, len(versions), self.args.step, self.output), flush=True)
        try:
            result = locate(versions, self.args.step, self.test)
            self.save(result)
            print('\nRESULT: {}'.format(json.dumps(result, sort_keys=True)))
            print('Current checkout: r{}\nRecords: {}'.format(self.current, self.output / 'results.tsv'))
            return 0 if result['outcome'] == 'BOUNDARY' else 2
        except BaseException as exc:
            self.save({'outcome': 'STOPPED', 'reason': str(exc) or type(exc).__name__})
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('testcase', help='One testcase directory or run.tcl')
    parser.add_argument('step', nargs='?', type=int, default=4, help='Cached-version stride (default: 4)')
    parser.add_argument('--timeout', type=int, default=10800)
    args = parser.parse_args()
    if args.step < 1 or args.timeout < 1:
        parser.error('step and timeout must be positive')
    if os.name != 'posix':
        parser.error('Run this command on the Linux GalaxCore workstation')
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
