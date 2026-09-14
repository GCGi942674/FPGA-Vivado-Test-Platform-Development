"""Shared offline review model, extracted from the native-panel prototype."""

import os, re, shutil, stat, tempfile, time, uuid

from pathlib import Path

from collections import namedtuple

ENTRY_RE = re.compile(r"^(\s*)(0x[0-9a-fA-F]+):([A-Za-z_][\w.-]*)(?:\s+(\d+))?(\s*)$")

ADDRESS_RE = re.compile(r"0x([0-9a-fA-F]+):([A-Za-z_][\w.-]*)")

SOURCE_EXTENSIONS = {'.cpp', '.cc', '.cxx', '.c', '.h', '.hpp', '.hxx', '.inl', '.ipp'}

SKIP_DIRS = {'.git', '.svn', '.hg', '__pycache__', 'node_modules'}

Entry = namedtuple('Entry', 'line section address module marker')

SourceHit = namedtuple('SourceHit', 'path line addresses')

class ReviewError(Exception):
    pass

def parse_entries(data):
    """Recognize review rows without discarding original bytes or sections."""
    result, section = [], 'Ungrouped'
    for number, raw in enumerate(data.decode('utf-8-sig', errors='replace').splitlines()):
        text = raw.strip()
        if text.startswith('='):
            section = text.lstrip('=').strip() or 'Ungrouped'
            continue
        match = ENTRY_RE.fullmatch(raw)
        if match:
            result.append(Entry(number, section, int(match.group(2), 16),
                                match.group(3), match.group(4) or ''))
    return result

def source_hits(text, path):
    """Each annotation is an independent mapping; never derive address offsets."""
    result = []
    for number, line in enumerate(text.splitlines()):
        if not re.match(r'^\s*//\s*0x[0-9a-fA-F]+:', line):
            continue
        pairs = [(int(ea, 16), module) for ea, module in ADDRESS_RE.findall(line)]
        if pairs:
            result.append(SourceHit(str(path), number, tuple(pairs)))
    return result

def build_index(root, cancelled=lambda: False):
    """Single background scan. Duplicates remain explicit candidates."""
    index, warnings, count = {}, [], 0
    for directory, dirs, files in os.walk(str(root), followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if cancelled():
                return index, warnings, count
            path = Path(directory) / name
            if path.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            try:
                text = path.read_text(encoding='utf-8-sig', errors='replace')
            except OSError as exc:
                warnings.append('{}: {}'.format(path, exc))
                continue
            count += 1
            for hit in source_hits(text, path):
                for key in set(hit.addresses):
                    index.setdefault(key, []).append(hit)
    return index, warnings, count

def target_address(hit, module):
    matches = {ea for ea, candidate in hit.addresses if candidate == module}
    if len(matches) != 1:
        raise ReviewError('Annotation must contain exactly one address for module "{}".'.format(module))
    return next(iter(matches))

class ReviewDocument:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.undo_state = None
        self.reload()

    def reload(self):
        self.data = self.path.read_bytes()
        self.entries = parse_entries(self.data)

    def replace(self, expected, replacement):
        """Cooperating writers lock; external changes are refused, not merged."""
        lock = str(self.path) + '.review.lock'
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ReviewError('Review file is locked: {}. Another panel may be saving.'.format(lock))
        temp = None
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(str(os.getpid()))
            if self.path.read_bytes() != expected:
                raise ReviewError('review_list changed on disk. Reload before marking or undoing.')
            backup_dir = self.path.parent / (self.path.name + '.review-backups')
            backup_dir.mkdir(exist_ok=True)
            backup = backup_dir / ('{}-{}.bak'.format(time.strftime('%Y%m%d-%H%M%S'), uuid.uuid4().hex[:8]))
            shutil.copy2(str(self.path), str(backup))
            handle, temp = tempfile.mkstemp(prefix='.' + self.path.name + '.', dir=str(self.path.parent))
            with os.fdopen(handle, 'wb') as stream:
                stream.write(replacement)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, stat.S_IMODE(self.path.stat().st_mode))
            if self.path.read_bytes() != expected:
                raise ReviewError('review_list changed while saving. Reload and retry.')
            os.replace(temp, str(self.path))
            temp = None
        finally:
            if temp is not None:
                os.unlink(temp)
            os.unlink(lock)
        self.reload()

    def mark_done(self, entry):
        if entry.marker not in ('', '6'):
            raise ReviewError('This row has an unknown marker; edit it manually if needed.')
        if entry.marker == '6':
            return
        if entry not in self.entries:
            raise ReviewError('Review selection is stale. Reload the list.')
        lines = self.data.splitlines(keepends=True)
        raw = lines[entry.line]
        body = raw.rstrip(b'\r\n')
        newline = raw[len(body):]
        trailing = body[len(body.rstrip(b' \t')):]
        lines[entry.line] = body.rstrip(b' \t') + b' 6' + trailing + newline
        before, after = self.data, b''.join(lines)
        self.replace(before, after)
        self.undo_state = (before, after)

    def undo(self):
        if self.undo_state is None:
            raise ReviewError('There is no completed row to undo in this session.')
        before, after = self.undo_state
        self.replace(after, before)
        self.undo_state = None
