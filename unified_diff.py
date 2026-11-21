"""Strict byte-preserving unified diff parsing and in-memory reconstruction."""

from __future__ import annotations

from dataclasses import dataclass, field
import posixpath
import re


class PatchError(ValueError):
    def __init__(self, code, message, line=None):
        super().__init__(message)
        self.code, self.line = code, line

    def as_dict(self):
        return {"code": self.code, "message": str(self), "patch_line": self.line}


def lines(data):
    """Split only on LF: CR and other control bytes belong to file contents."""
    pieces = data.split(b"\n")
    return [piece + b"\n" for piece in pieces[:-1]] + ([pieces[-1]] if pieces[-1] else [])


def unquote(value):
    if not value.startswith(b'"'):
        return value
    if not value.endswith(b'"'):
        raise PatchError("malformed_path", "unterminated quoted path")
    result, index = bytearray(), 1
    escapes = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8,
               ord("f"): 12, ord("v"): 11, ord("a"): 7, ord('"'): 34, ord("\\"): 92}
    while index < len(value) - 1:
        byte = value[index]
        index += 1
        if byte == 34:
            raise PatchError("malformed_path", "unescaped quote inside path")
        if byte == 92:
            if index >= len(value) - 1:
                raise PatchError("malformed_path", "truncated path escape")
            byte = value[index]
            index += 1
            if 48 <= byte <= 55:
                octal = bytes([byte])
                while len(octal) < 3 and index < len(value) - 1 and 48 <= value[index] <= 55:
                    octal += value[index:index + 1]
                    index += 1
                byte = int(octal, 8)
                if byte > 255:
                    raise PatchError("malformed_path", "invalid octal path escape")
            elif byte in escapes:
                byte = escapes[byte]
            else:
                raise PatchError("unsupported_path_escape", "unknown Git path escape")
        result.append(byte)
    return bytes(result)


def normalize_path(value, side=None):
    value = unquote(value).decode("utf-8", "surrogateescape")
    if value == "/dev/null":
        return None
    if not value or "\x00" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise PatchError("unsafe_path", "empty, absolute, or NUL-containing path")
    if ".." in value.split("/"):
        raise PatchError("unsafe_path", "parent traversal is not supported")
    value = posixpath.normpath(value)
    if side and value.startswith(side + "/"):
        value = value[2:]
    if value in {"", "."}:
        raise PatchError("unsafe_path", "path does not name a file")
    return value


def path_header(value, side):
    # Unified diff timestamps are separated by TAB. Quoted Git paths encode TAB.
    if value.startswith(b'"'):
        match = re.match(br'"(?:[^"\\]|\\.)*"', value)
        if not match or value[match.end():].strip() and not value[match.end():].startswith(b"\t"):
            raise PatchError("malformed_path", "invalid quoted file header")
        value = match.group()
    else:
        value = value.split(b"\t", 1)[0]
    return normalize_path(value, side)


def git_paths(header):
    value = header[len(b"diff --git "):]
    # Handle ordinary unquoted names with spaces as well as Git C-quoted names.
    if value.startswith(b'"'):
        match = re.match(br'("(?:[^"\\]|\\.)*") (.+)$', value)
        if not match:
            raise PatchError("malformed_path", "invalid diff --git paths")
        first, second = match.groups()
    elif b' "' in value:
        first, second = value.split(b' "', 1)
        second = b'"' + second
    elif b" b/" in value:
        if value.count(b" b/") != 1:
            raise PatchError("ambiguous_path", "ambiguous unquoted diff --git paths")
        first, second = value.split(b" b/", 1)
        second = b"b/" + second
    else:
        parts = value.split(b" ")
        if len(parts) != 2:
            raise PatchError("ambiguous_path", "cannot separate diff --git paths")
        first, second = parts
    return normalize_path(first, "a"), normalize_path(second, "b")


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    entries: list = field(default_factory=list)


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    headers_seen: bool = False
    git: bool = False

    @property
    def action(self):
        return ("add" if self.old_path is None else "remove" if self.new_path is None
                else "rename" if self.old_path != self.new_path else "modify")


HUNK = re.compile(br"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")


def parse_patch(raw: bytes) -> list[FilePatch]:
    if b"\x00" in raw:
        raise PatchError("unsupported_binary", "NUL-containing patch")
    records, source, index, current = [], lines(raw), 0, None
    while index < len(source):
        number = index + 1
        row = source[index].removesuffix(b"\n")
        if row.startswith((b"diff --cc ", b"diff --combined ", b"@@@")):
            raise PatchError("unsupported_combined_diff", "combined merge diffs are not unified diffs", number)
        if row.startswith((b"GIT binary patch", b"Binary files ")):
            raise PatchError("unsupported_binary", "binary patch payload", number)
        if row.startswith((b"copy from ", b"copy to ")):
            raise PatchError("unsupported_copy", "copy metadata is not implemented", number)
        if row.startswith(b"diff --git "):
            current = FilePatch(*git_paths(row), git=True)
            records.append(current)
            index += 1
            continue
        if row.startswith(b"--- "):
            if current is None or current.headers_seen:
                current = FilePatch(None, None)
                records.append(current)
            if index + 1 >= len(source) or not source[index + 1].startswith(b"+++ "):
                raise PatchError("malformed_file_header", "missing +++ file header", number)
            old = path_header(row[4:], "a")
            new = path_header(source[index + 1].removesuffix(b"\n")[4:], "b")
            if old is None and new is None:
                raise PatchError("malformed_file_header", "both sides are /dev/null", number)
            if current.git and ((old is not None and old != current.old_path)
                                or (new is not None and new != current.new_path)):
                raise PatchError("conflicting_paths", "Git and unified headers disagree", number)
            current.old_path, current.new_path, current.headers_seen = old, new, True
            index += 2
            continue
        if row.startswith(b"@@"):
            match = HUNK.fullmatch(row)
            if current is None or not current.headers_seen or not match:
                raise PatchError("malformed_hunk", "invalid hunk header or missing file headers", number)
            old, old_count, new, new_count = match.groups()
            hunk = Hunk(int(old), int(old_count) if old_count is not None else 1,
                        int(new), int(new_count) if new_count is not None else 1)
            if (hunk.old_count and not hunk.old_start) or (hunk.new_count and not hunk.new_start):
                raise PatchError("malformed_hunk", "nonempty hunk starts at line zero", number)
            if not hunk.old_count and not hunk.new_count:
                raise PatchError("malformed_hunk", "empty hunk", number)
            index += 1
            old_seen = new_seen = 0
            while old_seen < hunk.old_count or new_seen < hunk.new_count:
                if index >= len(source) or source[index][:1] not in (b" ", b"+", b"-"):
                    raise PatchError("hunk_count_mismatch", "truncated hunk or wrong line counts", index + 1)
                entry = source[index]
                if not entry.endswith(b"\n"):
                    raise PatchError("truncated_hunk_line", "hunk data must end in LF; use a no-newline marker", index + 1)
                tag, content = entry[:1], entry[1:]
                old_seen += tag != b"+"
                new_seen += tag != b"-"
                if old_seen > hunk.old_count or new_seen > hunk.new_count:
                    raise PatchError("hunk_count_mismatch", "hunk line counts exceeded", index + 1)
                index += 1
                if index < len(source) and source[index].removesuffix(b"\n") == b"\\ No newline at end of file":
                    content = content[:-1]
                    index += 1
                hunk.entries.append((tag, content))
            current.hunks.append(hunk)
            continue
        if current is not None and current.git and not current.hunks:
            if re.fullmatch(br"index [0-9a-f]+\.\.[0-9a-f]+(?: [0-9]{6})?", row):
                mode = row.rsplit(b" ", 1)[-1]
                if len(mode) == 6 and mode.isdigit() and mode not in {b"100644", b"100755"}:
                    raise PatchError("unsupported_file_type", "non-regular file patch", number)
                current.metadata["index"] = row.decode("ascii")
            elif row.startswith((b"old mode ", b"new mode ", b"new file mode ", b"deleted file mode ")):
                name, mode = row.rsplit(b" ", 1)
                if mode not in {b"100644", b"100755"}:
                    raise PatchError("unsupported_file_type", "only regular-file modes are supported", number)
                current.metadata[name.decode()] = mode.decode()
            elif row.startswith((b"rename from ", b"rename to ")):
                key = "rename from" if row.startswith(b"rename from ") else "rename to"
                current.metadata[key] = normalize_path(row[len(key) + 1:])
            elif re.fullmatch(br"(?:dis)?similarity index (?:100|[0-9]{1,2})%", row):
                current.metadata["similarity"] = row.decode()
            else:
                raise PatchError("unsupported_patch_record", "unknown Git metadata or trailing content", number)
            index += 1
            continue
        raise PatchError("unsupported_patch_record", "unexpected content outside a unified hunk", number)
    if not records:
        raise PatchError("empty_patch", "patch contains no file changes")
    old_paths, new_paths = set(), set()
    for record in records:
        metadata = record.metadata
        if {"new file mode", "deleted file mode"} <= metadata.keys() or (
                ("rename from" in metadata or "rename to" in metadata)
                and ("new file mode" in metadata or "deleted file mode" in metadata)):
            raise PatchError("conflicting_metadata", "incompatible file operations")
        if "rename from" in metadata or "rename to" in metadata:
            if metadata.get("rename from") != record.old_path or metadata.get("rename to") != record.new_path:
                raise PatchError("conflicting_paths", "incomplete or conflicting rename metadata")
        if "new file mode" in metadata:
            if record.headers_seen and record.old_path is not None:
                raise PatchError("conflicting_paths", "new-file mode with a nonempty old side")
            record.old_path = None
        if "deleted file mode" in metadata:
            if record.headers_seen and record.new_path is not None:
                raise PatchError("conflicting_paths", "deleted-file mode with a nonempty new side")
            record.new_path = None
        if not record.hunks and record.action == "modify" and not {"old mode", "new mode"} <= metadata.keys():
            raise PatchError("missing_hunks", "file has no hunks or supported metadata-only change")
        if ("old mode" in metadata) != ("new mode" in metadata):
            raise PatchError("malformed_mode_change", "both old and new modes are required")
        if record.old_path in old_paths or record.new_path in new_paths:
            raise PatchError("duplicate_file_change", "a file occurs twice on the same side of the patch")
        if record.old_path is not None:
            old_paths.add(record.old_path)
        if record.new_path is not None:
            new_paths.add(record.new_path)
    return records


def reconstruct(patch: FilePatch, original: bytes):
    """Apply exact preimages only; never offset-search or mutate the baseline.

    Edit coordinates are zero-based half-open intervals. An insertion [k,k]
    is between baseline lines k and k+1; new_lines maps unchanged lines only.
    """
    old_lines, result, correspondence, edits = lines(original), [], [], []
    cursor = 0
    for hunk in patch.hunks:
        start = hunk.old_start - 1 if hunk.old_count else hunk.old_start
        new_start = hunk.new_start - 1 if hunk.new_count else hunk.new_start
        if start < cursor or start > len(old_lines):
            raise PatchError("hunk_out_of_range", "overlapping, unordered, or out-of-range hunk")
        result.extend(old_lines[cursor:start])
        correspondence.extend(range(cursor + 1, start + 1))
        cursor = start
        if new_start != len(result):
            raise PatchError("inconsistent_new_position", "new hunk position disagrees with previous edits")
        edit = None
        for tag, content in hunk.entries:
            if tag == b" ":
                if edit is not None:
                    edits.append(edit)
                    edit = None
            elif edit is None:
                edit = {"old": [cursor, cursor], "new": [len(result), len(result)]}
            if tag != b"+":
                if cursor >= len(old_lines) or old_lines[cursor] != content:
                    raise PatchError("preimage_mismatch", f"baseline content differs at line {cursor + 1}")
                cursor += 1
            if tag != b"-":
                result.append(content)
                correspondence.append(cursor if tag == b" " else None)
            if edit is not None:
                edit["old"][1], edit["new"][1] = cursor, len(result)
        if edit is not None:
            edits.append(edit)
    result.extend(old_lines[cursor:])
    correspondence.extend(range(cursor + 1, len(old_lines) + 1))
    if any(not line.endswith(b"\n") for line in result[:-1]):
        raise PatchError("invalid_no_newline_marker", "no-newline marker occurs before the end of the new file")
    modified = b"".join(result)
    if patch.new_path is None and modified:
        raise PatchError("incomplete_file_removal", "removed file still has content")
    return modified, correspondence, edits
