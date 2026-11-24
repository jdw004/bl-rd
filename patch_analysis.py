"""Reconstruct independent patches, then compare source ASTs (never patch ASTs)."""

from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import sys
import tokenize
import types
from types import MappingProxyType

from artifact_bundle import PINNED_COMMIT, read_bundle
from unified_diff import PatchError, lines, parse_patch, reconstruct

DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
POSITIONS = {"lineno", "end_lineno", "col_offset", "end_col_offset"}


def structural(value, omit=()):
    """Exclude source positions even when they are fields, e.g. TypeIgnore."""
    if isinstance(value, ast.AST):
        return {"type": type(value).__name__, **{
            name: structural(child, omit) for name, child in ast.iter_fields(value)
            if name not in POSITIONS and name not in omit}}
    if isinstance(value, list):
        return [structural(child, omit) for child in value]
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, complex):
        return {"complex": repr(value)}
    if value is Ellipsis:
        return {"ellipsis": True}
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def projection(value):
    """An enclosing scope sees a nested definition's header, not its body."""
    if isinstance(value, DEFINITIONS):
        return structural(value, {"body"})
    if isinstance(value, ast.AST):
        return {"type": type(value).__name__, **{name: projection(child)
                for name, child in ast.iter_fields(value) if name not in POSITIONS}}
    if isinstance(value, list):
        return [projection(child) for child in value]
    return structural(value)


def own_nodes(body):
    for node in body:
        if isinstance(node, DEFINITIONS):
            continue
        yield node
        for child in ast.iter_child_nodes(node):
            yield from own_nodes([child])


def scope_components(node):
    body = node.body
    local = list(own_nodes(body))
    components = {
        "body": projection(body),
        "docstring": ast.get_docstring(node, clean=False),
        "imports": [structural(child) for child in local if isinstance(child, (ast.Import, ast.ImportFrom))],
        "assignments": [structural(child) for child in local if isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign))],
    }
    if isinstance(node, ast.Module):
        components["type_ignores"] = structural(node.type_ignores)
    if isinstance(node, DEFINITIONS):
        components["decorators"] = structural(node.decorator_list)
        components["type_parameters"] = structural(getattr(node, "type_params", []))
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        components["kind"] = type(node).__name__
        components["signature"] = structural(node.args, {"defaults", "kw_defaults", "annotation", "type_comment"})
        components["defaults"] = structural([*node.args.defaults, *node.args.kw_defaults])
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        arguments += [arg for arg in (node.args.vararg, node.args.kwarg) if arg]
        components["annotations"] = {"args": structural([arg.annotation for arg in arguments]),
            "returns": structural(node.returns), "type_comment": node.type_comment}
    if isinstance(node, ast.ClassDef):
        components["bases"] = structural(node.bases)
        components["class_keywords"] = structural(node.keywords)
        components["class_attributes"] = components.pop("assignments")
    return {key: fingerprint(value) for key, value in components.items()}


def bindings(node):
    result = {}
    for child in own_nodes(node.body):
        if isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            for target in targets:
                result.setdefault(ast.unparse(target), []).append({
                    "fingerprint": fingerprint(structural(child)), "range": [child.lineno, child.end_lineno]})
    return result


def changed_bindings(old, new):
    result = []
    for name in sorted(old.keys() | new.keys()):
        before, after = old.get(name, []), new.get(name, [])
        if [value["fingerprint"] for value in before] != [value["fingerprint"] for value in after]:
            result.append({"target": name, "baseline": before, "modified": after})
    return result


def index_tree(tree):
    symbols, duplicates = {}, []

    def visit(node, names=(), ancestors=()):
        if isinstance(node, DEFINITIONS):
            names += (node.name,)
            identity = ".".join(names)
            start = min([node.lineno] + [decorator.lineno for decorator in node.decorator_list])
            record = {"name": identity, "parent": ".".join(names[:-1]) or "<module>",
                "kind": type(node).__name__, "range": [start, node.end_lineno],
                "fingerprint": fingerprint(structural(node)), "components": scope_components(node),
                "bindings": bindings(node),
                "inside_function": any(isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)) for parent in ancestors)}
            if identity in symbols:
                duplicates.append(identity)
            symbols[identity] = record
            ancestors += (node,)
        for child in ast.iter_child_nodes(node):
            visit(child, names, ancestors)

    visit(tree)
    return symbols, sorted(set(duplicates))


def definition_occurrences(tree):
    result = {}

    def visit(node, parents=()):
        if isinstance(node, DEFINITIONS):
            parents += (node.name,)
            result.setdefault(".".join(parents), []).append(fingerprint(structural(node)))
        for child in ast.iter_child_nodes(node):
            visit(child, parents)

    visit(tree)
    return result


def executed_statement_ranges(original, modified, old_tree, changes, edits, path):
    """A body replacement can only affect tests that reached its old statement.

    Reject insertions/deletions and compiler scope changes: a new local, closure
    binding, or yield can change behavior before the edited statement executes.
    """
    leaves = [change for change in changes if change["categories"] != ["nested_definitions"]]
    if not leaves or any(change["change"] != "modified" or change["definition_time_change"]
                         or change["kind"] not in {"FunctionDef", "AsyncFunctionDef"} for change in leaves):
        return None
    if any(not edit["old"][1] - edit["old"][0]
           or edit["old"][1] - edit["old"][0] != edit["new"][1] - edit["new"][0] for edit in edits):
        return None

    def scope_signatures(source):
        signatures = {}

        def visit(code):
            name = code.co_qualname.replace(".<locals>.", ".")
            signatures.setdefault(name, []).append((code.co_varnames, code.co_cellvars, code.co_freevars, code.co_flags))
            for value in code.co_consts:
                if isinstance(value, types.CodeType):
                    visit(value)

        visit(compile(source, path, "exec"))
        return signatures

    old_signatures, new_signatures = scope_signatures(original), scope_signatures(modified)
    for change in leaves:
        name = change["identity"].split("::", 1)[1]
        if old_signatures.get(name) != new_signatures.get(name):
            return None
    statements = [node for node in ast.walk(old_tree)
                  if isinstance(node, ast.stmt) and not isinstance(node, DEFINITIONS)]
    ranges = []
    for edit in edits:
        first, last = edit["old"][0] + 1, edit["old"][1]
        candidates = [node for node in statements if node.lineno <= first and node.end_lineno >= last]
        if not candidates:
            return None
        node = min(candidates, key=lambda item: (item.end_lineno - item.lineno, -item.col_offset))
        ranges.append([node.lineno, node.end_lineno])
    return ranges


def parse_source(source, path):
    encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
    tree = ast.parse(source, filename=path, type_comments=True)
    # AST construction alone accepts some invalid programs, e.g. return at module scope.
    compile(tree, path, "exec")
    return tree, encoding


def smallest_scope(symbols, line):
    candidates = [symbol for symbol in symbols.values() if symbol["range"][0] <= line <= symbol["range"][1]]
    return min(candidates, key=lambda symbol: (symbol["range"][1] - symbol["range"][0], -symbol["name"].count(".")))["name"] if candidates else "<module>"


def line_ranges(correspondence):
    result = []
    for new, old in enumerate(correspondence, 1):
        if old is None:
            continue
        if result and result[-1][1] == new - 1 and old == result[-1][2] + new - result[-1][0]:
            result[-1][1] = new
        else:
            result.append([new, new, old])
    return result


def compare_python(original, modified, old_path, new_path, edits):
    old_tree, old_encoding = parse_source(original, old_path or new_path)
    new_tree, new_encoding = parse_source(modified, new_path or old_path)
    old_symbols, old_duplicates = index_tree(old_tree)
    new_symbols, new_duplicates = index_tree(new_tree)
    changes, uncertainty = [], []
    path = old_path or new_path
    for name in sorted(old_symbols.keys() | new_symbols.keys()):
        old, new = old_symbols.get(name), new_symbols.get(name)
        if old and new and old["fingerprint"] == new["fingerprint"]:
            continue
        reference = new or old
        if old and new:
            categories = sorted(key for key in old["components"].keys() | new["components"].keys()
                                if old["components"].get(key) != new["components"].get(key))
            if not categories:
                categories = ["nested_definitions"]
        else:
            categories = ["definition_added" if new else "definition_removed"]
        header_change = bool(set(categories) & {"definition_added", "definition_removed", "kind", "decorators",
            "signature", "defaults", "annotations", "type_parameters", "bases", "class_keywords", "docstring"})
        class_initialization = reference["kind"] == "ClassDef" and bool(set(categories) & {"body", "class_attributes", "imports"})
        parent = reference["parent"]
        while parent not in old_symbols and parent != "<module>":
            parent = parent.rpartition(".")[0] or "<module>"
        changes.append({
            "identity": f"{path}::{name}", "old_identity": f"{old_path}::{name}" if old else None,
            "new_identity": f"{new_path}::{name}" if new else None,
            "kind": reference["kind"], "change": "modified" if old and new else "added" if new else "removed",
            "categories": categories, "baseline_range": old["range"] if old else None,
            "binding_changes": changed_bindings(old["bindings"] if old else {}, new["bindings"] if new else {}),
            "modified_range": new["range"] if new else None,
            "old_fingerprint": old["fingerprint"] if old else None,
            "new_fingerprint": new["fingerprint"] if new else None,
            "baseline_enclosing_scope": f"{path}::{parent}",
            "requires_scope_expansion": old is None or header_change or class_initialization,
            "definition_time_change": header_change or class_initialization,
            "requires_import_expansion": (header_change or class_initialization) and not reference["inside_function"],
        })
    old_module, new_module = scope_components(old_tree), scope_components(new_tree)
    module_changes = sorted(key for key in old_module.keys() | new_module.keys()
                            if old_module.get(key) != new_module.get(key))
    old_fingerprint, new_fingerprint = fingerprint(structural(old_tree)), fingerprint(structural(new_tree))
    identical_ast = old_fingerprint == new_fingerprint
    if identical_ast and original != modified:
        uncertainty.append({"code": "identical_ast_not_safety_proof",
            "message": "Source bytes or line positions changed; reflection, encoding, comments and fixture metadata may matter."})
    if old_encoding != new_encoding:
        uncertainty.append({"code": "encoding_changed", "message": f"{old_encoding} -> {new_encoding}"})
    duplicate_names = set(old_duplicates + new_duplicates)
    before_occurrences, after_occurrences = definition_occurrences(old_tree), definition_occurrences(new_tree)
    affected_duplicates = sorted(name for name in duplicate_names
                                 if before_occurrences.get(name) != after_occurrences.get(name))
    if affected_duplicates:
        uncertainty.append({"code": "ambiguous_symbol_identity", "message": "Repeated qualified definitions require fallback.",
                            "symbols": affected_duplicates})
    additions = []
    old_count = len(lines(original))
    for edit in edits:
        if edit["new"][0] == edit["new"][1]:
            continue
        scopes = sorted({smallest_scope(new_symbols, line) for line in range(edit["new"][0] + 1, edit["new"][1] + 1)})
        enclosing = set()
        for scope in scopes:
            while scope not in old_symbols and scope != "<module>":
                scope = scope.rpartition(".")[0] or "<module>"
            enclosing.add(f"{path}::{scope}")
        boundary = edit["old"][0]
        additions.append({"modified_lines": [edit["new"][0] + 1, edit["new"][1]],
            "baseline_coverage": None, "baseline_enclosing_scopes": sorted(enclosing),
            "baseline_anchor_lines": [line for line in (boundary, edit["old"][1] + 1) if 1 <= line <= old_count],
            "needs_dependency_expansion": True})
    shifted = [name for name in sorted(old_symbols.keys() & new_symbols.keys())
               if old_symbols[name]["fingerprint"] == new_symbols[name]["fingerprint"]
               and old_symbols[name]["range"] != new_symbols[name]["range"]]
    return {"identical_ast": identical_ast, "encoding": {"old": old_encoding, "new": new_encoding},
            "old_ast_fingerprint": old_fingerprint, "new_ast_fingerprint": new_fingerprint,
            "symbol_changes": changes, "position_only_symbols": shifted,
            "module_binding_changes": changed_bindings(bindings(old_tree), bindings(new_tree)),
            "module_changes": module_changes, "requires_import_expansion": bool(module_changes)
                or any(change["requires_import_expansion"] for change in changes),
            "new_code": additions, "uncertainty_reasons": uncertainty,
            "body_statement_ranges": executed_statement_ranges(original, modified, old_tree, changes, edits, path)}


class PatchAnalyzer:
    def __init__(self, baseline, metadata=None, verified=False):
        # Each analysis starts from this snapshot; no working tree or previous result is used.
        self.baseline = MappingProxyType({name: bytes(content) for name, content in baseline.items()})
        self.metadata = metadata
        self.verified = verified

    @classmethod
    def from_data(cls, data):
        if data["metadata"]["source"]["commit"] != PINNED_COMMIT:
            raise ValueError("wrong baseline revision")
        baseline = {name: base64.b64decode(content, validate=True) for name, content in data["baseline_files"].items()}
        if set(baseline) != set(data["source_sha256"]) or any(
                hashlib.sha256(content).hexdigest() != data["source_sha256"][name] for name, content in baseline.items()):
            raise ValueError("baseline snapshot does not match source hashes")
        return cls(baseline, data, verified=True)

    @classmethod
    def from_bundle(cls, path):
        data, _ = read_bundle(path)
        return cls.from_data(data)

    def metadata_references(self, path, python, edits):
        if self.metadata is None:
            return {}
        changed_lines = {line for edit in edits for line in range(edit["old"][0] + 1, edit["old"][1] + 1)}
        line_sets = {group for first, last, group in self.metadata["lines"].get(path, [])
                     if any(first <= line <= last for line in changed_lines)}
        names = {change["identity"].split("::", 1)[1] for change in python.get("symbol_changes", [])}
        function_sets = {group for name, first, last, group in self.metadata["functions"].get(path, []) if name in names}
        return {"baseline_line_consumer_sets": sorted(line_sets), "baseline_function_consumer_sets": sorted(function_sets),
                "file_read_consumer_set": self.metadata["reads"].get(path),
                "fixture_definitions": sorted(key for key, value in self.metadata["fixtures"].items() if value["file"] == path),
                "classes": sorted(key for key, value in self.metadata["structure"]["classes"].items() if value["file"] == path)}

    def analyze(self, raw):
        summary = {"format_supported": False, "reconstruction_complete": False, "analysis_complete": False,
                   "fallback_required": True, "uncertainty_reasons": [], "files": [],
                   "parser_python": ".".join(map(str, sys.version_info[:3])), "safe_to_skip": False}
        try:
            patches = parse_patch(raw)
        except PatchError as error:
            summary["uncertainty_reasons"].append(error.as_dict())
            return summary
        summary["format_supported"] = True
        if not self.verified:
            summary["uncertainty_reasons"].append({"code": "baseline_unverified", "message": "Pinned baseline snapshot is not verified."})
        if self.metadata is None:
            summary["uncertainty_reasons"].append({"code": "metadata_unavailable", "message": "Generated dependency metadata is unavailable."})
        elif not self.metadata["metadata"]["instrumentation_complete"]:
            summary["uncertainty_reasons"].append({"code": "dependency_metadata_incomplete", "message": "Dependency generation reports incomplete evidence."})
        if self.metadata is not None:
            expected = self.metadata["metadata"]["environment"]["python"].split(".")[:2]
            if expected != list(map(str, sys.version_info[:2])):
                summary["uncertainty_reasons"].append({"code": "parser_environment_mismatch", "message": "Python parser differs from generation environment."})
        moved_or_removed = {patch.old_path for patch in patches if patch.old_path != patch.new_path}
        for patch in patches:
            file = {"old_path": patch.old_path, "new_path": patch.new_path, "action": patch.action,
                    "hunks": len(patch.hunks),
                    "dependency_kind": "python_source" if any(path and path.endswith(".py")
                        for path in (patch.old_path, patch.new_path)) else "repository_data_file",
                    "file_level_change": True, "metadata_changes": patch.metadata,
                    "reconstructed": False, "analyzed": False, "uncertainty_reasons": []}
            summary["files"].append(file)
            try:
                if patch.old_path is not None and patch.old_path not in self.baseline:
                    raise PatchError("baseline_unavailable", "baseline file is absent: " + patch.old_path)
                if patch.new_path != patch.old_path and patch.new_path in self.baseline and patch.new_path not in moved_or_removed:
                    raise PatchError("destination_exists", "add/rename would overwrite a baseline file")
                original = self.baseline[patch.old_path] if patch.old_path is not None else b""
                modified, correspondence, edits = reconstruct(patch, original)
                file.update({"reconstructed": True, "edits": edits,
                    "unchanged_line_ranges": line_ranges(correspondence),
                    "baseline_line_count": len(lines(original)), "modified_line_count": len(lines(modified)),
                    "original_sha256": hashlib.sha256(original).hexdigest(),
                    "modified_sha256": hashlib.sha256(modified).hexdigest()})
                python = {}
                if file["dependency_kind"] == "python_source":
                    python = compare_python(original, modified, patch.old_path, patch.new_path, edits)
                    file["python"] = python
                    file["uncertainty_reasons"].extend(python["uncertainty_reasons"])
                file["dependency_references"] = self.metadata_references(patch.old_path or patch.new_path, python, edits)
                if patch.action in {"add", "rename", "remove"} or "old mode" in patch.metadata:
                    file["requires_file_dependency_expansion"] = True
                file["analyzed"] = True
            except PatchError as error:
                file["uncertainty_reasons"].append(error.as_dict())
            except (SyntaxError, UnicodeError, LookupError, ValueError, TypeError, RecursionError) as error:
                file["uncertainty_reasons"].append({"code": "python_parse_failed", "message": str(error),
                                                   "line": getattr(error, "lineno", None)})
        summary["reconstruction_complete"] = all(file["reconstructed"] for file in summary["files"])
        summary["analysis_complete"] = all(file["analyzed"] for file in summary["files"])
        summary["fallback_required"] = not summary["analysis_complete"] or bool(summary["uncertainty_reasons"]) or any(
            file["uncertainty_reasons"] for file in summary["files"])
        return summary
