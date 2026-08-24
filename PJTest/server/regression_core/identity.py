#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build stable identities for comparable PJTest examples."""

import hashlib
import json
from pathlib import PurePosixPath


# These values describe one execution, not the functional test configuration.
# They must not make an otherwise identical test look like a new test case.
DYNAMIC_CONFIG_KEYS = frozenset([
    "attempt_id",
    "created_at",
    "example_id",
    "finished_at",
    "log_file",
    "log_path",
    "report_dir",
    "run_log_dir",
    "started_at",
    "task_id",
    "timestamp",
    "updated_at",
    "worker_name",
])


def normalize_path(value):
    """Return a stable slash-separated path without filesystem access."""
    text = str(value or "").strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    if not text:
        return ""
    normalized = str(PurePosixPath(text))
    return "" if normalized == "." else normalized.rstrip("/")


def _normalize_config_value(value):
    if isinstance(value, dict):
        return dict(
            (str(key), _normalize_config_value(item))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key).lower() not in DYNAMIC_CONFIG_KEYS
        )
    if isinstance(value, list):
        return [_normalize_config_value(item) for item in value]
    return value


def canonical_flow_config(raw_value):
    """Return canonical JSON and ignore execution-only dynamic keys."""
    if isinstance(raw_value, dict):
        config = raw_value
    else:
        raw_text = str(raw_value or "{}").strip() or "{}"
        try:
            config = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid flow_config_json: %s" % exc)

    if not isinstance(config, dict):
        raise ValueError("flow_config_json must contain a JSON object")

    normalized = _normalize_config_value(config)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _short_hash(text, length):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def build_test_identity(work_root, run_tcl_path, template_name, flow_config_json):
    """Return (test_key, config_hash, normalized_path, canonical_config)."""
    normalized_root = normalize_path(work_root)
    normalized_case = normalize_path(run_tcl_path)
    normalized_template = str(template_name or "").strip()
    canonical_config = canonical_flow_config(flow_config_json)
    config_hash = _short_hash(canonical_config, 16)
    identity_text = "\n".join([
        normalized_root,
        normalized_case,
        normalized_template,
        canonical_config,
    ])
    test_key = _short_hash(identity_text, 20)
    return test_key, config_hash, normalized_case, canonical_config
