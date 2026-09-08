#!/usr/bin/env python3

from hashlib import sha256
from pathlib import Path
from typing import Tuple
import argparse
import sys
import os


SYNC_WORD = b"\xAA\x99\x55\x66"


def load_payload(path: Path) -> Tuple[bytes, int, bytes]:
    data = path.read_bytes()
    # Standard .bit container: magic, a/b/c/d metadata, e + 32-bit length.
    magic = b"\x00\x09\x0f\xf0\x0f\xf0\x0f\xf0\x0f\xf0\x00\x00\x01"
    if not data.startswith(magic):
        raise ValueError("{}: invalid standard .bit header".format(path))
    cursor = len(magic)
    for tag in b"abcd":
        if cursor + 3 > len(data) or data[cursor] != tag:
            raise ValueError("{}: missing/truncated metadata field {}".format(path, chr(tag)))
        size = int.from_bytes(data[cursor + 1:cursor + 3], "big")
        cursor += 3
        if not size or cursor + size > len(data):
            raise ValueError("{}: invalid metadata length".format(path))
        cursor += size
    if cursor + 5 > len(data) or data[cursor] != ord("e"):
        raise ValueError("{}: missing/truncated payload length".format(path))
    size = int.from_bytes(data[cursor + 1:cursor + 5], "big")
    cursor += 5
    if size != len(data) - cursor:
        raise ValueError("{}: declared payload length {} != actual {}".format(
            path, size, len(data) - cursor))
    sync_offset = data.find(SYNC_WORD, cursor)
    if sync_offset < 0 or sync_offset + 4 >= len(data):
        raise ValueError("{}: missing sync word or empty post-sync data".format(path))
    # Container checks do not validate configuration packets or device CRC.
    return data, sync_offset, data[sync_offset:]


class Output:
    def __init__(self, mode="auto", stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.enabled = mode == "always" or (
            mode == "auto" and self.stream.isatty() and "NO_COLOR" not in os.environ)
        if self.enabled and os.name == "nt" and self.stream.isatty():
            try:
                import ctypes
                import msvcrt
                handle = msvcrt.get_osfhandle(self.stream.fileno())
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                current = ctypes.c_ulong()
                handle = ctypes.c_void_p(handle)
                ok = kernel.GetConsoleMode(handle, ctypes.byref(current))
                ok = ok and kernel.SetConsoleMode(handle, current.value | 4)
                if not ok and mode == "auto":
                    self.enabled = False
            except (OSError, ValueError, AttributeError):
                self.enabled = mode == "always"

    def paint(self, text, color):
        return "\033[{}m{}\033[0m".format(color, text) if self.enabled else text

    def line(self, text, color="31"):
        print(self.paint(text, color), file=self.stream)


def show_differences(left, right, sync1, sync2, out, limit):
    shown = 0
    for offset in range(max(len(left), len(right))):
        a = left[offset] if offset < len(left) else None
        b = right[offset] if offset < len(right) else None
        if a == b:
            continue
        if limit and shown >= limit:
            out.line("Further differences omitted; use --max-diffs 0 to show all.", "33")
            break
        shown += 1
        out.line("Difference {}: sync+0x{:X}, word {}".format(shown, offset, offset // 4))
        for name, payload, base in (("File 1", left, sync1), ("File 2", right, sync2)):
            value = "{:02X}".format(payload[offset]) if offset < len(payload) else "<EOF>"
            out.line("  {}: file offset 0x{:X}, byte {}".format(name, base + offset, value))
            start = max(0, offset - 4)
            end = min(len(payload), offset + 5)
            context = " ".join("[{:02X}]".format(payload[i]) if i == offset
                               else "{:02X}".format(payload[i]) for i in range(start, end))
            out.line("    context (sync+0x{:X}): {}".format(start, context or "<EOF>"))


def sha256_hex(data: bytes) -> str:
    return sha256(data).hexdigest()


def first_difference(left: bytes, right: bytes):
    common_length = min(len(left), len(right))

    for offset in range(common_length):
        if left[offset] != right[offset]:
            return offset

    if len(left) != len(right):
        return common_length

    return None



def count_different_bytes(left: bytes, right: bytes) -> int:
    count = sum(a != b for a, b in zip(left, right))
    count += abs(len(left) - len(right))
    return count


def format_word(payload: bytes, byte_offset: int) -> str:
    word_offset = (byte_offset // 4) * 4
    word = payload[word_offset:word_offset + 4]

    if len(word) != 4:
        return "Less than 4 bytes available"

    return "0x{:08X}".format(
        int.from_bytes(word, byteorder="big")
    )

def compare_bitstreams(file1: Path, file2: Path, color="auto", max_diffs=16) -> int:
    out = Output(color)
    raw1, sync1, payload1 = load_payload(file1)
    raw2, sync2, payload2 = load_payload(file2)

    print("File 1: {}".format(file1))
    print("File 2: {}".format(file2))
    print()

    print("File 1 size: {:,} bytes".format(len(raw1)))
    print("File 2 size: {:,} bytes".format(len(raw2)))
    print("File 1 sync offset: 0x{:X}".format(sync1))
    print("File 2 sync offset: 0x{:X}".format(sync2))
    print("File 1 payload size: {:,} bytes".format(len(payload1)))
    print("File 2 payload size: {:,} bytes".format(len(payload2)))
    print()

    print("File 1 payload SHA256: {}".format(sha256_hex(payload1)))
    print("File 2 payload SHA256: {}".format(sha256_hex(payload2)))
    print()

    if payload1 == payload2:
        if raw1 == raw2:
            out.line("Result: The two bitstream files are identical.", "32")
        else:
            out.line("Result: The compared bytes from sync word onward are identical.", "32")
            out.line("Differences before the sync word are ignored (metadata/preamble).", "33")

        return 0

    offset = first_difference(payload1, payload2)
    different_count = count_different_bytes(payload1, payload2)

    out.line("Result: The compared payload bytes are different.")
    out.line("Different byte count: {:,}".format(different_count))

    if offset is not None:
        print(
            "First difference offset from sync word: 0x{:X}".format(offset)
        )
        print("First different 32-bit word index: {}".format(offset // 4))

        if offset < len(payload1):
            print(
                "File 1 corresponding 32-bit word: {}".format(
                    format_word(payload1, offset)
                )
            )
        else:
            print("File 1 has reached the end of the payload.")

        if offset < len(payload2):
            print(
                "File 2 corresponding 32-bit word: {}".format(
                    format_word(payload2, offset)
                )
            )
        else:
            print("File 2 has reached the end of the payload.")

    show_differences(payload1, payload2, sync1, sync2, out, max_diffs)
    return 1



def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare FPGA programming payloads while ignoring "
            "Vivado bitstream headers."
        )
    )

    parser.add_argument(
        "file1",
        type=Path,
        help="Path to the first bitstream file"
    )

    parser.add_argument(
        "file2",
        type=Path,
        help="Path to the second bitstream file"
    )

    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                        help="Color output (default: auto; redirected output is plain)")
    parser.add_argument("--max-diffs", type=int, default=16,
                        help="Maximum differing bytes to display (default: 16; 0: all)")
    args = parser.parse_args()
    if args.max_diffs < 0:
        parser.error("--max-diffs must be >= 0")

    try:
        return compare_bitstreams(args.file1, args.file2, args.color, args.max_diffs)

    except (OSError, ValueError) as error:
        Output(args.color, sys.stderr).line("Error: {}".format(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())