"""Private loopback discovery; registry tokens are never returned to the UI."""
import json
import os
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def registry_path():
    uid = str(os.getuid()) if hasattr(os, 'getuid') else os.environ.get('USERNAME', 'user')
    return Path(os.environ.get('IDA_REVIEW_REGISTRY', str(Path(tempfile.gettempdir()) / ('ida-review-' + uid))))


def rpc(record, operation, timeout=20, **fields):
    port = int(record['port'])
    if not 0 < port < 65536:
        raise ValueError('Invalid bridge port')
    body = dict(fields, op=operation, expected={key: record[key] for key in ('instance', 'idb', 'input', 'imagebase')})
    request = Request('http://127.0.0.1:{}/review'.format(port), data=json.dumps(body).encode('utf-8'),
                      headers={'Content-Type': 'application/json', 'X-Review-Token': record['token']})
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as exc:
        try:
            message = json.load(exc).get('error', str(exc))
        except (ValueError, AttributeError):
            message = str(exc)
        raise RuntimeError(message)
    if not result.get('ok'):
        raise RuntimeError(result.get('error', 'IDA request failed'))
    return result['data']


def discover():
    records = []
    root = registry_path()
    if not root.exists() or root.is_symlink():
        return records
    if hasattr(os, 'getuid') and (root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077):
        return records
    for path in sorted(root.glob('*.json')):
        try:
            if path.is_symlink() or path.stat().st_size > 16384:
                continue
            if hasattr(os, 'getuid') and (path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
                continue
            record = json.loads(path.read_text())
            current = rpc(record, 'health', timeout=0.8)
            if any(current.get(key) != record.get(key) for key in ('instance', 'idb', 'input', 'imagebase')):
                continue
            records.append(record)
        except (OSError, ValueError, KeyError, RuntimeError):
            continue
    return records
