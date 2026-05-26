#!/usr/bin/env python3
import argparse
import os
from collections import Counter
from statistics import mean
from datetime import datetime
from utils import parse_env_file, write_json, write_text


def collect_status_files(root: str):
    result = []
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            if filename == 'result.env':
                result.append(os.path.join(dirpath, filename))
    return sorted(result)


def safe_int(value, default=0):
    try:
        return int(str(value).strip())
    except Exception:
        return default


def ts_to_str(ts):
    if ts <= 0:
        return ''
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')

def count_case_list(path):
    if not path or not os.path.isfile(path):
        return 0
    count = 0
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if line:
                count += 1
    return count

def build_reports(records, meta):
    status_counter = Counter()
    reason_counter = Counter()
    runtime_values = []
    failed_rows = []

    start_ts_values = []
    end_ts_values = []

    for rec in records:
        status = rec.get('STATUS', 'UNKNOWN')
        reason = rec.get('REASON', 'UNKNOWN')
        status_counter[status] += 1
        reason_counter[reason] += 1

        runtime_sec = safe_int(rec.get('RUNTIME_SEC', '0'))
        runtime_values.append(runtime_sec)

        start_ts = safe_int(rec.get('START_TS', '0'))
        end_ts = safe_int(rec.get('END_TS', '0'))
        if start_ts > 0:
            start_ts_values.append(start_ts)
        if end_ts > 0:
            end_ts_values.append(end_ts)

        if status != 'PASS':
            failed_rows.append(rec)

    runnable_cases = len(records)

    total_from_case_list = count_case_list(meta.get('case_list', ''))
    total = total_from_case_list if total_from_case_list > 0 else runnable_cases

    skipped_cases = max(0, total - runnable_cases)

    avg_runtime = round(mean(runtime_values), 2) if runtime_values else 0
    pass_count = status_counter.get('PASS', 0)
    timeout_count = status_counter.get('TIMEOUT', 0)

    failed_cases = runnable_cases - pass_count
    fail_count = failed_cases + skipped_cases

    run_start_ts = min(start_ts_values) if start_ts_values else 0
    run_end_ts = max(end_ts_values) if end_ts_values else 0
    elapsed_time_sec = max(0, run_end_ts - run_start_ts) if run_start_ts and run_end_ts else 0

    run_start_time = ts_to_str(run_start_ts)
    run_end_time = ts_to_str(run_end_ts)

    overall_status = 'PASS' if fail_count == 0 and timeout_count == 0 else 'FAIL'

    report_lines = [
        'Execution Report',
        '================',
        f"Host: {meta['host_name']}",
        f"SVN Version: {meta['svn_version']}",
        f"Flow Config: {meta['flow_config']}",
        f"Enabled Modules: {meta['enabled_modules']}",
        f"Parallel Max: {meta['bg_max']}",
        f"Time Limit(s): {meta['time_limit']}",
        f"Start Time: {run_start_time}",
        f"End Time: {run_end_time}",
        f"Elapsed Time(s): {elapsed_time_sec}",
        '',
        f'Total cases: {total}',
        f'Runnable cases: {runnable_cases}',
        f'Skipped cases: {skipped_cases}',
        f'PASS: {pass_count}',
        f'FAIL/TIMEOUT: {fail_count}',
        f'TIMEOUT: {timeout_count}',
        f'Average runtime (sec): {avg_runtime}',
        f'Status: {overall_status}',
        '',
        'Top failure reasons:'
    ]

    if reason_counter:
        for reason, count in reason_counter.most_common(10):
            report_lines.append(f'  {reason}: {count}')
    else:
        report_lines.append('  none')

    summary_lines = []
    for rec in records:
        if rec.get('STATUS', 'UNKNOWN') == 'PASS':
            summary_lines.append(
                f"{rec.get('CASE_DIR', '')}"
            )

    failed_lines = []
    for rec in records:
        if rec.get('STATUS', 'UNKNOWN') != 'PASS':
            failed_lines.append(
                f"[{rec.get('STATUS', 'UNKNOWN')}] {rec.get('CASE_DIR', '')} | {rec.get('REASON', 'UNKNOWN')}"
            )

    stat_lines = [
        '================ Statistics ================',
        f"Host                : {meta['host_name']}",
        f"Start Time          : {run_start_time}",
        f"End Time            : {run_end_time}",
        f"Version             : {meta['svn_version']}",
        f"Parallel Max        : {meta['bg_max']}",
        f"Time Limit(s)       : {meta['time_limit']}",
        f"Enabled Modules     : {meta['enabled_modules']}",
        f"Total Cases         : {total}",
        f"Runnable Cases      : {runnable_cases}",
        f"Skipped Cases       : {skipped_cases}",
        f"Pass Cases          : {pass_count}",
        f"Failed Cases        : {fail_count}",
        f"Timeout Cases       : {timeout_count}",
        f"Elapsed Time(s)     : {elapsed_time_sec}",
        f"Status              : {overall_status}",
        '============================================',
    ]

    payload = {
        'meta': {
            **meta,
            'start_time': run_start_time,
            'end_time': run_end_time,
            'elapsed_time_sec': elapsed_time_sec,
            'runnable_cases': runnable_cases,
            'skipped_cases': skipped_cases,
            'status': overall_status,
        },
        'total': total,
        'runnable_cases': runnable_cases,
        'skipped_cases': skipped_cases,
        'pass_cases': pass_count,
        'failed_cases': fail_count,
        'timeout_cases': timeout_count,
        'status_counter': dict(status_counter),
        'reason_counter': dict(reason_counter),
        'average_runtime_sec': avg_runtime,
        'start_time': run_start_time,
        'end_time': run_end_time,
        'elapsed_time_sec': elapsed_time_sec,
        'status': overall_status,
        'records': records,
    }

    return (
        '\n'.join(summary_lines) + ('\n' if summary_lines else ''),
        '\n'.join(failed_lines) + ('\n' if failed_lines else ''),
        '\n'.join(stat_lines) + '\n',
        '\n'.join(report_lines) + '\n',
        payload,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--status-root', required=True)
    parser.add_argument('--case-list', required=False, default='')
    parser.add_argument('--summary', required=True)
    parser.add_argument('--failed', required=True)
    parser.add_argument('--stat', required=True)
    parser.add_argument('--text-report', required=True)
    parser.add_argument('--json-report', required=True)
    parser.add_argument('--enabled-modules', required=True)
    parser.add_argument('--time-limit', required=True)
    parser.add_argument('--bg-max', required=True)
    parser.add_argument('--host-name', required=True)
    parser.add_argument('--svn-version', required=True)
    parser.add_argument('--flow-config', required=True)
    args = parser.parse_args()

    records = [parse_env_file(path) for path in collect_status_files(args.status_root)]
    meta = {
        'case_list': args.case_list,
        'enabled_modules': args.enabled_modules,
        'time_limit': args.time_limit,
        'bg_max': args.bg_max,
        'host_name': args.host_name,
        'svn_version': args.svn_version,
        'flow_config': args.flow_config,
    }

    summary_text, failed_text, stat_text, report_text, payload = build_reports(records, meta)
    write_text(args.summary, summary_text)
    write_text(args.failed, failed_text)
    write_text(args.stat, stat_text)
    write_text(args.text_report, report_text)
    write_json(args.json_report, payload)


if __name__ == '__main__':
    main()