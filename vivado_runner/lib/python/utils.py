#!/usr/bin/env python3
import json
import os
from typing import Dict


def parse_env_file(path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    with open(path, 'r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or '=' not in line:
                continue
            key, value = line.split('=', 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            data[key] = value
    return data


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def write_text(path: str, content: str) -> None:
    ensure_parent_dir(path)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def write_json(path: str, payload: dict) -> None:
    ensure_parent_dir(path)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
