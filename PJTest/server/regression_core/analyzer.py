#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read PJTest terminal results and derive a current regression summary."""

import re
from collections import defaultdict
from dataclasses import dataclass

from .identity import build_test_identity


NUMERIC_REVISION_RE = re.compile(r"^\d+$")
TERMINAL_TASK_STATUSES = ("success", "failed")
TERMINAL_EXAMPLE_STATUSES = ("success", "failed", "timeout")
DEFAULT_REGRESSION_SUITE = "daily_regression"

STATE_SEVERITY = {
    "OPEN": 80,
    "NO_BASELINE": 50,
    "FLAKY": 30,
    "INCONCLUSIVE": 10,
    "FIXED": 0,
    "STABLE": 0,
    "UNKNOWN": 0,
}

STATE_ORDER = {
    "OPEN": 0,
    "NO_BASELINE": 1,
    "FLAKY": 2,
    "INCONCLUSIVE": 3,
    "FIXED": 4,
    "STABLE": 5,
    "UNKNOWN": 6,
}


@dataclass(frozen=True)
class Observation:
    test_key: str
    case_path: str
    template_name: str
    config_hash: str
    revision: int
    status: str
    run_date: str
    updated_at: str


@dataclass(frozen=True)
class RevisionResult:
    revision: int
    status: str
    updated_at: str


@dataclass(frozen=True)
class RegressionCase:
    test_key: str
    case_path: str
    template_name: str
    config_hash: str
    state: str
    last_good: object
    first_bad: object
    last_bad: object
    first_fixed: object
    latest_revision: int
    latest_status: str
    severity: int
    updated_at: str


@dataclass(frozen=True)
class NightlyRegression:
    test_key: str
    case_path: str
    template_name: str
    success_revision: int
    fail_revision: int


def _parse_revision(value):
    text = str(value or "").strip()
    if not NUMERIC_REVISION_RE.match(text):
        return None
    return int(text)


def _observation_status(example_status, infra_reason):
    if str(infra_reason or "").strip():
        return "INFRA_ERROR"
    mapping = {
        "success": "PASS",
        "failed": "FAIL",
        "timeout": "TIMEOUT",
    }
    return mapping.get(str(example_status or "").strip().lower(), "UNKNOWN")


def load_observations(conn, suite=DEFAULT_REGRESSION_SUITE):
    """Load comparable terminal results from completed regression-suite tasks."""
    task_placeholders = ",".join("?" for _ in TERMINAL_TASK_STATUSES)
    example_placeholders = ",".join("?" for _ in TERMINAL_EXAMPLE_STATUSES)
    query = """
        SELECT
            e.example_id,
            e.run_tcl_path,
            e.revision AS example_revision,
            e.status AS example_status,
            e.infra_reason,
            COALESCE(e.finished_at, e.updated_at, t.finished_at, t.updated_at,
                     t.created_at, '') AS observation_time,
            t.template_name,
            t.revision AS task_revision,
            t.created_at AS task_created_at,
            t.work_root,
            t.flow_config_json
        FROM task_examples AS e
        JOIN tasks AS t ON t.task_id = e.task_id
        WHERE t.status IN (%s)
          AND e.status IN (%s)
          AND t.suite = ?
        ORDER BY t.id, e.id
    """ % (task_placeholders, example_placeholders)
    params = TERMINAL_TASK_STATUSES + TERMINAL_EXAMPLE_STATUSES + (suite,)
    rows = conn.execute(query, params).fetchall()

    attempt_query = """
        SELECT
            a.example_id,
            a.revision AS attempt_revision,
            a.status AS attempt_status,
            a.infra_reason AS attempt_infra_reason,
            COALESCE(a.finished_at, a.started_at, '') AS attempt_time
        FROM task_attempts AS a
        JOIN task_examples AS e ON e.example_id = a.example_id
        JOIN tasks AS t ON t.task_id = e.task_id
        WHERE t.status IN (%s)
          AND e.status IN (%s)
          AND a.status IN (%s)
          AND t.suite = ?
        ORDER BY a.id
    """ % (
        task_placeholders,
        example_placeholders,
        example_placeholders,
    )
    attempt_params = (
        TERMINAL_TASK_STATUSES
        + TERMINAL_EXAMPLE_STATUSES
        + TERMINAL_EXAMPLE_STATUSES
        + (suite,)
    )
    attempts_by_example = defaultdict(list)
    for attempt in conn.execute(attempt_query, attempt_params).fetchall():
        attempts_by_example[attempt["example_id"]].append(attempt)

    observations = []
    skipped_non_numeric_revision = 0
    for row in rows:
        test_key, config_hash, case_path, _ = build_test_identity(
            row["work_root"],
            row["run_tcl_path"],
            row["template_name"],
            row["flow_config_json"],
        )

        attempt_rows = attempts_by_example.get(row["example_id"], [])
        source_rows = attempt_rows or [row]
        for source in source_rows:
            is_attempt = bool(attempt_rows)
            revision = _parse_revision(
                source["attempt_revision"] if is_attempt else row["example_revision"]
            )
            if revision is None:
                revision = _parse_revision(row["example_revision"])
            if revision is None:
                revision = _parse_revision(row["task_revision"])
            if revision is None:
                skipped_non_numeric_revision += 1
                continue

            status = (
                _observation_status(
                    source["attempt_status"],
                    source["attempt_infra_reason"],
                )
                if is_attempt
                else _observation_status(row["example_status"], row["infra_reason"])
            )
            updated_at = (
                source["attempt_time"] if is_attempt else row["observation_time"]
            )
            observations.append(Observation(
                test_key=test_key,
                case_path=case_path,
                template_name=str(row["template_name"] or ""),
                config_hash=config_hash,
                revision=revision,
                status=status,
                run_date=str(row["task_created_at"] or "")[:10],
                updated_at=str(updated_at or ""),
            ))

    return observations, {
        "terminal_rows": len(rows),
        "terminal_attempts": sum(len(value) for value in attempts_by_example.values()),
        "loaded_observations": len(observations),
        "skipped_non_numeric_revision": skipped_non_numeric_revision,
    }


def aggregate_revision_status(statuses):
    """Merge repeated results for one test and revision."""
    values = set(statuses)
    decisive = values.intersection(["PASS", "FAIL", "TIMEOUT"])

    if "PASS" in decisive and ("FAIL" in decisive or "TIMEOUT" in decisive):
        return "FLAKY"
    if decisive == set(["FAIL", "TIMEOUT"]):
        return "INCONCLUSIVE"
    if len(decisive) == 1:
        return next(iter(decisive))
    if "INFRA_ERROR" in values:
        return "INFRA_ERROR"
    return "UNKNOWN"


def group_revision_results(observations):
    """Return test_key -> ordered RevisionResult list."""
    grouped = defaultdict(lambda: defaultdict(list))
    identities = {}
    for observation in observations:
        identities[observation.test_key] = observation
        grouped[observation.test_key][observation.revision].append(observation)

    output = {}
    for test_key, by_revision in grouped.items():
        results = []
        for revision in sorted(by_revision):
            rows = by_revision[revision]
            results.append(RevisionResult(
                revision=revision,
                status=aggregate_revision_status(row.status for row in rows),
                updated_at=max((row.updated_at for row in rows), default=""),
            ))
        output[test_key] = (identities[test_key], results)
    return output


def analyze_case(identity, revisions):
    """Fold ordered revision outcomes into the latest regression episode."""
    last_pass = None
    active_event = None
    last_closed_event = None

    for result in revisions:
        if result.status == "PASS":
            if active_event is not None:
                closed = dict(active_event)
                closed["first_fixed"] = result.revision
                last_closed_event = closed
                active_event = None
            last_pass = result.revision
        elif result.status == "FAIL":
            if active_event is None:
                active_event = {
                    "last_good": last_pass,
                    "first_bad": result.revision,
                    "last_bad": result.revision,
                    "first_fixed": None,
                }
            else:
                active_event["last_bad"] = result.revision

    latest = revisions[-1]
    event = active_event or last_closed_event

    if latest.status == "FLAKY":
        state = "FLAKY"
    elif latest.status in ("TIMEOUT", "INCONCLUSIVE", "INFRA_ERROR", "UNKNOWN"):
        state = "INCONCLUSIVE"
    elif active_event is not None:
        state = "OPEN" if active_event["last_good"] is not None else "NO_BASELINE"
    elif last_closed_event is not None:
        state = "FIXED"
    elif last_pass is not None:
        state = "STABLE"
    else:
        state = "UNKNOWN"

    if event is None:
        last_good = last_pass
        first_bad = None
        last_bad = None
        first_fixed = None
    else:
        last_good = event["last_good"]
        first_bad = event["first_bad"]
        last_bad = event["last_bad"]
        first_fixed = event["first_fixed"]

    return RegressionCase(
        test_key=identity.test_key,
        case_path=identity.case_path,
        template_name=identity.template_name,
        config_hash=identity.config_hash,
        state=state,
        last_good=last_good,
        first_bad=first_bad,
        last_bad=last_bad,
        first_fixed=first_fixed,
        latest_revision=latest.revision,
        latest_status=latest.status,
        severity=STATE_SEVERITY[state],
        updated_at=latest.updated_at,
    )


def analyze_observations(observations):
    """Return one sorted current summary row per stable test identity."""
    cases = []
    for identity, revisions in group_revision_results(observations).values():
        cases.append(analyze_case(identity, revisions))

    cases.sort(key=lambda item: (
        STATE_ORDER[item.state],
        -item.severity,
        item.case_path,
        item.template_name,
        item.test_key,
    ))
    return cases


def analyze_latest_nightly_regressions(observations):
    """Return PASS-to-FAIL transitions between the latest two nightly dates."""
    run_dates = sorted(set(
        item.run_date
        for item in observations
        if re.match(r"^\d{4}-\d{2}-\d{2}$", item.run_date or "")
    ))
    if len(run_dates) < 2:
        return [], None, run_dates[-1] if run_dates else None

    previous_run_date = run_dates[-2]
    current_run_date = run_dates[-1]
    regressions = []
    grouped = defaultdict(lambda: defaultdict(list))
    identities = {}

    for observation in observations:
        identities[observation.test_key] = observation
        grouped[observation.test_key][observation.run_date].append(observation)

    for test_key, by_date in grouped.items():
        previous_rows = by_date.get(previous_run_date, [])
        current_rows = by_date.get(current_run_date, [])
        if not previous_rows or not current_rows:
            continue
        if aggregate_revision_status(item.status for item in previous_rows) != "PASS":
            continue
        if aggregate_revision_status(item.status for item in current_rows) != "FAIL":
            continue

        previous_revisions = set(item.revision for item in previous_rows)
        current_revisions = set(item.revision for item in current_rows)
        if len(previous_revisions) != 1 or len(current_revisions) != 1:
            continue

        identity = identities[test_key]
        regressions.append(NightlyRegression(
            test_key=identity.test_key,
            case_path=identity.case_path,
            template_name=identity.template_name,
            success_revision=next(iter(previous_revisions)),
            fail_revision=next(iter(current_revisions)),
        ))

    regressions.sort(key=lambda item: (
        item.template_name,
        item.case_path,
        item.test_key,
    ))
    return regressions, previous_run_date, current_run_date
