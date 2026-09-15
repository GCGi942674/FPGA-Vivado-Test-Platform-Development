#!/usr/bin/env python3
"""Identify working copies and maintain the legacy result-directory entrypoint."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import sys
import time
import uuid


def workspace_identity(workspace):
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Workspace must be a directory: {}".format(root))
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", root.parent.name)
    name = name.strip("._-")[:48] or "workspace"
    name = re.sub(r"\.{2,}", "_", name)
    if not name[0].isalnum():
        name = "workspace_" + name
    digest = hashlib.sha256(os.fsencode(str(root))).hexdigest()[:16]
    return "{}_{}".format(name, digest)


def prepare_runtime(base, workspace, namespace):
    # Serialize only namespace registration and the one-time legacy migration.
    # The long-running case work is protected by the workspace execution lock.
    import fcntl

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", namespace) or ".." in namespace:
        raise ValueError("Invalid runtime namespace: {}".format(namespace))
    root = Path(workspace).resolve(strict=True)
    base = Path(base).resolve()
    base.mkdir(parents=True, exist_ok=True)
    with (base / ".compat.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        workspaces = base / "workspaces"
        if workspaces.is_symlink():
            raise ValueError("Runtime workspaces directory must not be a symlink")
        runtime = workspaces / namespace
        if runtime.is_symlink():
            raise ValueError("Runtime namespace must not be a symlink")
        runtime.mkdir(parents=True, exist_ok=True)
        owner = runtime / ".workspace-root"
        if owner.exists():
            if owner.read_text(encoding="utf-8") != str(root):
                raise ValueError("Runtime namespace is already assigned to another workspace: {}".format(namespace))
        else:
            owner.write_text(str(root), encoding="utf-8")
        status = runtime / "status"
        if status.is_symlink():
            raise ValueError("Workspace status directory must not be a symlink")
        status.mkdir(exist_ok=True)

        legacy = base / "status"
        layout = base / ".workspace-status-layout"
        if not layout.exists() and (legacy.exists() or legacy.is_symlink()):
            # Keep pre-upgrade results instead of deleting or mixing them.
            backup = base / "legacy"
            backup.mkdir(exist_ok=True)
            legacy.rename(backup / "status.{}.{}".format(int(time.time() * 1000000000), os.getpid()))
        legacy.mkdir(exist_ok=True)
        layout.write_text("workspace-mirrors-v1\n", encoding="utf-8")


def publish_results(base, namespace):
    """Publish result.env snapshots under the stable legacy search root."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", namespace) or ".." in namespace:
        raise ValueError("Invalid runtime namespace")
    base = Path(base).resolve()
    source = base / "workspaces" / namespace / "status"
    destination = base / "status" / namespace
    for result in source.rglob("result.env"):
        target = destination / result.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / (".result.next." + uuid.uuid4().hex)
        try:
            # Preserve timestamps so existing readers can reject stale results.
            shutil.copy2(str(result), str(temporary))
            os.replace(str(temporary), str(target))
        finally:
            if temporary.exists():
                temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    identity = commands.add_parser("identity")
    identity.add_argument("workspace")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("base")
    prepare.add_argument("workspace")
    prepare.add_argument("namespace")
    publish = commands.add_parser("publish")
    publish.add_argument("base")
    publish.add_argument("namespace")
    args = parser.parse_args()
    if args.command is None:
        parser.error("a command is required")
    try:
        if args.command == "identity":
            print(workspace_identity(args.workspace))
        elif args.command == "prepare":
            prepare_runtime(args.base, args.workspace, args.namespace)
        else:
            publish_results(args.base, args.namespace)
    except (OSError, ValueError) as error:
        print("[ERROR] {}".format(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
