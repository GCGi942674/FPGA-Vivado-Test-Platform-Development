#!/usr/bin/env python3
import os, sys, re, pdb
import subprocess
import shutil
import tempfile
import zipfile
from contextlib import contextmanager

SDF_SORT_SCRIPT = "/home/zhenlei/projects/GalaxCoreRegressionPlatform/sort_sdf.py"
BIT_SYNC_WORD = b"\xAA\x99\x55\x66"

GREEN = "\033[92m"
RED = "\033[91m"
BLUE = "\033[94m"
RESET = "\033[0m"


def clean_log(path: str, report=False):
  cleaned = []
  with open(path, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
      line = line.rstrip("\n")
      if not line.strip():
        continue
      if report:
        cleaned.append(line)
        continue
      if line.strip().startswith("general.maxThreads"):
        continue
      if "Errors encountered." in line:   #ignore maxThreads info, golden  is single-thread
        line = re.sub(r"\b\d+ Infos\b", "0 Infos", line)

      if line.startswith(("#Command route_design execution time:", "#Command place_design execution time:")) :
        break
      if line.startswith(("#", "Command:", "source", "set argv", "INFO")):
        continue
      if "Infos" in line and "Warnings" in line and "Errors" in line : # 9 Infos, 1 Warnings, 0 Critical Warnings and 0 Errors encountered.
        continue
      if line.startswith("Time taken to check if laguna hold fix is required") : # xcvu13p-fhgb2104-2-e_fip_cordic_rca
        continue
      if ("Time (s)" in line) or ("CPU" in line) or ("cpu" in line) or ("time:" in line) or ("xiaonan" in line):
        continue
      line = re.sub(r"\b\d\d:\d\d:\d\d\b", "00:00:00", line)
      cleaned.append(line)
  return cleaned


def clean_report_section(path: str, title: str, end_marker: str):
  lines = clean_log(path, report=True)
  start = next((i for i, line in enumerate(lines) if title in line), None)
  if start is None:
    raise ValueError(f"{title} not found in {path}")

  cleaned = []
  for line in lines[start:]:
    if line.strip() == "q" or end_marker in line or line.lstrip().startswith("## if {"):
      break
    if re.match(r"^\s*\|?\s*(Tool Version|Date|Host|Command)\s*:", line, re.I):
      continue
    cleaned.append(line)
  if len(cleaned) <= 1:
    raise ValueError(f"No comparable report content in {path}")
  return cleaned


def clean_report_timing_summary_only(path: str):
  return clean_report_section(path, "Timing Summary Report", "report_timing_summary: Time")


def clean_report_power_only(path: str):
  return clean_report_section(path, "Power Report", "report_power: Time")


def clean_report_place_status(path: str):
  return clean_report_section(path, "Design Place Status", "report_place_status: Time")


def clean_report_route_status(path: str):
  return clean_report_section(path, "Design Route Status", "report_route_status: Time")


def clean_utilization_from_place_only(path: str):
  return clean_report_section(path, "Utilization Design Information", "report_utilization: Time")



def clean_verilog(path: str):
  cleaned = []
  original = []
  found_timescale = False
  with open(path, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
      line = line.rstrip("\n")
      if line.strip():
        original.append(line)
      if not found_timescale:
        if not line.lstrip().startswith("`timescale"):
          continue
        found_timescale = True
      if line.strip():
        cleaned.append(line)
  return cleaned if found_timescale else original


def clean_sdf(path: str):
  cleaned = []
  original = []
  found_delayfile = False
  with open(path, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
      line = line.rstrip("\n")
      if re.match(r"^\(DATE(?:\s|\))", line.strip()):
        continue
      if line.strip():
        original.append(line)
      if not found_delayfile:
        if not line.lstrip().startswith("(DELAYFILE"):
          continue
        found_delayfile = True
      if line.strip():
        cleaned.append(line)
  return cleaned if found_delayfile else original


def clean_bit(path: str):
  with open(path, "rb") as f:
    data = f.read()

  magic = bytes.fromhex("00090ff00ff00ff00ff0000001")
  if not data.startswith(magic):
    raise ValueError(f"{path}: invalid .bit header")

  cursor = len(magic)
  for tag in b"abcd":
    if cursor + 3 > len(data) or data[cursor] != tag:
      raise ValueError(f"{path}: missing metadata field {chr(tag)}")
    size = int.from_bytes(data[cursor + 1:cursor + 3], "big")
    cursor += 3
    if not size or cursor + size > len(data):
      raise ValueError(f"{path}: invalid metadata length")
    cursor += size

  if cursor + 5 > len(data) or data[cursor] != ord("e"):
    raise ValueError(f"{path}: missing .bit payload length")
  size = int.from_bytes(data[cursor + 1:cursor + 5], "big")
  cursor += 5
  if size != len(data) - cursor:
    raise ValueError(f"{path}: declared payload length does not match file")

  sync_offset = data.find(BIT_SYNC_WORD, cursor)
  if sync_offset < 0 or sync_offset + len(BIT_SYNC_WORD) >= len(data):
    raise ValueError(f"{path}: missing sync word or empty post-sync payload")
  return memoryview(data)[sync_offset + len(BIT_SYNC_WORD):]


def clean_file(path: str):
  ext = os.path.splitext(path)[1].lower()
  name = os.path.basename(path).lower()
  if ext == ".v":
    return clean_verilog(path)
  elif ext == ".sdf":
    return clean_sdf(path)
  elif ext == ".bit":
    return clean_bit(path)
  elif "timing_summary_only" in name:
    return clean_report_timing_summary_only(path)
  elif "power_only" in name:
    return clean_report_power_only(path)
  elif "place_status" in name:
    return clean_report_place_status(path)
  elif "route_status" in name:
    return clean_report_route_status(path)
  elif "utilization_from_place_only" in name:
    return clean_utilization_from_place_only(path)
  else:
    return clean_log(path)


@contextmanager
def prepare_file(path: str):
  if not path.lower().endswith(".zip"):
    yield path
    return

  with zipfile.ZipFile(path) as archive:
    files = [item for item in archive.infolist() if not item.is_dir()]
    expected_name = os.path.basename(path[:-4])
    matches = [item for item in files
               if os.path.basename(item.filename) == expected_name]
    if len(matches) == 1:
      member = matches[0]
    elif len(files) == 1 and expected_name != "golden.bit":
      member = files[0]
    else:
      raise ValueError(f"Expected one file in ZIP archive: {path}")

    with tempfile.TemporaryDirectory() as temp_dir:
      prepared = os.path.join(temp_dir, os.path.basename(member.filename))
      with archive.open(member) as source, open(prepared, "wb") as target:
        shutil.copyfileobj(source, target)
      yield prepared


def sort_sdf_file(path: str, sorter: str, output: str):
  result = subprocess.run(
    [sys.executable, sorter, path, "-o", output],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True
  )
  if result.returncode != 0:
    raise RuntimeError(f"SDF sort failed for {path}: {result.stderr.strip()}")
  return output


def print_differences(log1, log2, f1, f2, to_diff):
  print("\n" + BLUE + "Differences:" + RESET)
  max_len = max(len(log1), len(log2))
  cnt = 1
  for i in range(max_len):
    line1 = log1[i] if i < len(log1) else ""
    line2 = log2[i] if i < len(log2) else ""
    if line1 != line2:
      if to_diff != "" :
        f = open(to_diff, "w" if cnt == 1 else "a")
        f.write(f"{cnt}:\n");
        f.write(f"[{f1}]\n")
        f.write("line: " + f"{line1}\n");
        f.write(f"[{f2}]\n")
        f.write("line: " + f"{line2}\n");
        f.write("\n")
        f.close()

      print(f"{cnt}:");
      print(RED + f"[{f1}]")
      print(BLUE + "line: " + f"{line1}");
      print(RED + f"[{f2}]")
      print(BLUE + "line: " + f"{line2}" + RESET);
      print()
      cnt += 1


def print_bit_differences(payload1, payload2, f1, f2, to_diff=""):
  count = 0
  shown = []
  for offset in range(max(len(payload1), len(payload2))):
    byte1 = payload1[offset] if offset < len(payload1) else None
    byte2 = payload2[offset] if offset < len(payload2) else None
    if byte1 != byte2:
      count += 1
      if len(shown) < 16:
        shown.append((offset, byte1, byte2))

  lines = [f"Different payload bytes: {count}"]
  for offset, byte1, byte2 in shown:
    value1 = f"0x{byte1:02X}" if byte1 is not None else "<EOF>"
    value2 = f"0x{byte2:02X}" if byte2 is not None else "<EOF>"
    lines.append(f"post-sync byte 0x{offset:X} (word {offset // 4}): "
                 f"[{f1}] {value1}, [{f2}] {value2}")
  if count > len(shown):
    lines.append(f"Showing first {len(shown)} differences")
  print("\n" + BLUE + "Differences:" + RESET)
  print("\n".join(lines))
  if to_diff:
    with open(to_diff, "w", encoding="utf-8") as result:
      result.write("\n".join(lines) + "\n")


def VvdCmpFiles(f1, f2, to_diff=""):
  with prepare_file(f1) as prepared1, prepare_file(f2) as prepared2:
    bit1 = os.path.splitext(prepared1)[1].lower() == ".bit"
    bit2 = os.path.splitext(prepared2)[1].lower() == ".bit"
    if bit1 != bit2:
      raise ValueError("Cannot compare a .bit file with a different file type")
    with tempfile.TemporaryDirectory() as sort_dir:
      if os.path.splitext(prepared1)[1].lower() == ".sdf":
        prepared1 = sort_sdf_file(
          prepared1, SDF_SORT_SCRIPT, os.path.join(sort_dir, "first_sorted.sdf")
        )
      if os.path.splitext(prepared2)[1].lower() == ".sdf":
        prepared2 = sort_sdf_file(
          prepared2, SDF_SORT_SCRIPT, os.path.join(sort_dir, "second_sorted.sdf")
        )
      log1 = clean_file(prepared1)
      log2 = clean_file(prepared2)

  if log1 == log2:
    print(GREEN + "Files are same" + RESET)
    if bit1 and to_diff:
      with open(to_diff, "w", encoding="utf-8"):
        pass
    return 1
  else:
    print(RED + "Files are different" + RESET)
    if bit1:
      print_bit_differences(log1, log2, f1, f2, to_diff)
    else:
      print_differences(log1, log2, f1, f2, to_diff)
    return 0


if __name__ == "__main__":
  if len(sys.argv) not in (3, 5) or (len(sys.argv) == 5 and sys.argv[3] != "--bit-result"):
    print("Usage: vvdCmpFiles.py <file1> <file2> [--bit-result <mis_bit.txt>]", file=sys.stderr)
    sys.exit(2)
  try:
    if len(sys.argv) == 5 and not (sys.argv[1].lower().endswith((".bit", ".bit.zip")) and
                                   sys.argv[2].lower().endswith((".bit", ".bit.zip"))):
      raise ValueError("--bit-result requires two .bit inputs")
    same = VvdCmpFiles(sys.argv[1], sys.argv[2], sys.argv[4] if len(sys.argv) == 5 else "")
    sys.exit(0 if same else 1)
  except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
    print(f"Comparison error: {exc}", file=sys.stderr)
    sys.exit(2)
