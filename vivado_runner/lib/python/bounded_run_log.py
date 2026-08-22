#!/usr/bin/env python3
"""Drain a case's output without allowing its run log to grow without bound."""

import argparse
import collections
import os
import sys
from pathlib import Path


PIN_REDEFINITION = b"PinRedefinition is not implemented. HdnTError.cc"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit-bytes", required=True, type=int)
    parser.add_argument("--tail-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--limit-marker", required=True)
    parser.add_argument("--suppress-pin-redefinition", action="store_true")
    return parser.parse_args()


class TailBuffer:
    def __init__(self, limit):
        self.limit = max(0, limit)
        self.parts = collections.deque()
        self.size = 0

    def add(self, data):
        if not data or self.limit == 0:
            return
        if len(data) >= self.limit:
            self.parts.clear()
            self.parts.append(data[-self.limit :])
            self.size = self.limit
            return
        self.parts.append(data)
        self.size += len(data)
        while self.size > self.limit and self.parts:
            excess = self.size - self.limit
            first = self.parts[0]
            if len(first) <= excess:
                self.parts.popleft()
                self.size -= len(first)
            else:
                self.parts[0] = first[excess:]
                self.size -= excess

    def value(self):
        return b"".join(self.parts)


def main():
    args = parse_args()
    if args.limit_bytes < 0:
        raise SystemExit("--limit-bytes must not be negative")

    output = Path(args.output)
    limit_marker = Path(args.limit_marker)
    try:
        limit_marker.unlink()
    except FileNotFoundError:
        pass

    unlimited = args.limit_bytes == 0
    tail_limit = 0 if unlimited else min(max(0, args.tail_bytes), args.limit_bytes // 2)
    tail = TailBuffer(tail_limit)
    accepted_bytes = 0
    suppressed_lines = 0

    with output.open("w+b", buffering=0) as run_log:
        for line in sys.stdin.buffer:
            if args.suppress_pin_redefinition and PIN_REDEFINITION in line:
                suppressed_lines += 1
                continue

            accepted_bytes += len(line)
            tail.add(line)
            remaining = args.limit_bytes - run_log.tell()
            if unlimited:
                run_log.write(line)
            elif remaining > 0:
                run_log.write(line[:remaining])

        if suppressed_lines:
            summary = (
                f"\n[INFO] suppressed_pin_redefinition_lines={suppressed_lines}\n"
            ).encode("ascii")
            accepted_bytes += len(summary)
            tail.add(summary)
            remaining = args.limit_bytes - run_log.tell()
            if unlimited:
                run_log.write(summary)
            elif remaining > 0:
                run_log.write(summary[:remaining])

        if not unlimited and accepted_bytes > args.limit_bytes:
            notice = (
                "\n[ERROR] LOG_LIMIT_REACHED: filtered run log exceeded "
                f"{args.limit_bytes} bytes; GalaxCore output was drained to EOF "
                "and the process was allowed to finish.\n"
            ).encode("ascii")
            tail_data = tail.value()
            head_size = max(0, args.limit_bytes - len(notice) - len(tail_data))
            run_log.seek(head_size)
            run_log.truncate()
            run_log.write(notice[: args.limit_bytes - run_log.tell()])
            remaining = args.limit_bytes - run_log.tell()
            if remaining > 0:
                run_log.write(tail_data[-remaining:])

            limit_marker.write_text(
                "reason=LOG_LIMIT_REACHED\n"
                f"limit_bytes={args.limit_bytes}\n"
                f"filtered_bytes={accepted_bytes}\n"
                f"suppressed_pin_redefinition_lines={suppressed_lines}\n",
                encoding="ascii",
            )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # The runner may be interrupted while this process is flushing stdout.
        try:
            sys.stdout.close()
        finally:
            os._exit(130)
