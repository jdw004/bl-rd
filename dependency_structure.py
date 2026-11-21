"""Conservative AST relationships, augmented by observed runtime class MROs."""

import ast
import importlib.util


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def analyze(root, modules):
    trees = {module: ast.parse((root / path).read_bytes()) for module, path in modules.items()}
    bindings, reexports, explicit_exports, classes, unresolved = {}, {}, {}, {}, []

    def issue(kind, module, node, reason):
        unresolved.append({"kind": kind, "file": modules[module], "line": node.lineno,
                           "expression": ast.unparse(node), "reason": reason})

    for module, tree in trees.items():
        aliases = bindings[module] = {}
        exports = reexports[module] = {}
        package = module if modules[module].endswith("/__init__.py") else module.rpartition(".")[0]
        # Only bindings in module scope are usable for module-level resolution.
        # Conditional imports are a conservative union; conflicting names stay unresolved.
        def statements(nodes):
            for node in nodes:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        local = alias.asname or alias.name.split(".")[0]
                        target = alias.name if alias.asname else local
                        bind(local, target, node)
                elif isinstance(node, ast.ImportFrom):
                    try:
                        base = importlib.util.resolve_name("." * node.level + (node.module or ""), package) if node.level else node.module or ""
                    except (ImportError, ValueError):
                        issue("import", module, node, "unresolved relative import")
                        continue
                    for alias in node.names:
                        if alias.name == "*":
                            issue("reexport", module, node, "wildcard export names require dynamic resolution")
                        else:
                            bind(alias.asname or alias.name, base + "." + alias.name, node)
                elif isinstance(node, (ast.If, ast.Try, ast.With)):
                    statements(node.body)
                    statements(getattr(node, "orelse", []))
                    statements(getattr(node, "finalbody", []))
                    for handler in getattr(node, "handlers", []):
                        statements(handler.body)
                elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    if node.targets[0].id == "__all__":
                        try:
                            names = ast.literal_eval(node.value)
                            if not isinstance(names, (tuple, list)) or not all(isinstance(name, str) for name in names):
                                raise ValueError("nonliteral exports")
                            explicit_exports[module] = sorted(names)
                        except (ValueError, TypeError):
                            issue("reexport", module, node, "dynamic __all__")
                    target = dotted(node.value)
                    if target:
                        first, _, rest = target.partition(".")
                        bind(node.targets[0].id, aliases.get(first, module + "." + first) + ("." + rest if rest else ""), node)

        def bind(local, target, node):
            if local in aliases and aliases[local] != target:
                issue("import", module, node, "ambiguous/conditional binding for " + local)
            aliases[local] = target
            exports[local] = target

        statements(tree.body)

        def visit(node, parents=()):
            if isinstance(node, ast.ClassDef):
                qualified = ".".join((module,) + parents + (node.name,))
                classes[qualified] = {
                    "file": modules[module], "line": node.lineno, "end_line": node.end_lineno,
                    "base_expressions": [ast.unparse(base) for base in node.bases],
                    "bases": [], "methods": {method.name: [method.lineno, method.end_lineno]
                    for method in node.body if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))},
                    "module": module,
                }
                for base in node.bases:
                    expression = dotted(base.value if isinstance(base, ast.Subscript) else base)
                    if expression:
                        first, _, rest = expression.partition(".")
                        target = aliases.get(first, "builtins.object" if first == "object" else module + "." + first)
                        classes[qualified]["bases"].append(target + ("." + rest if rest else ""))
                    else:
                        issue("inheritance", module, base, "computed class base")
                if node.keywords:
                    issue("inheritance", module, node, "metaclass/class keyword may alter relationships")
            if isinstance(node, ast.Call):
                name = dotted(node.func) or ""
                first, _, rest = name.partition(".")
                name = aliases.get(first, first) + ("." + rest if rest else "")
                if name.endswith("import_module") or name == "__import__":
                    if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                        issue("dynamic_import", module, node, "nonliteral target; observed imports do not cover all values")
                if name in {"exec", "eval"} or name.endswith("spec_from_file_location"):
                    issue("dynamic_import", module, node, "dynamic code/module loading is not statically resolved")
                if name.endswith("getfixturevalue") and (not node.args or not isinstance(node.args[0], ast.Constant)):
                    issue("fixture", module, node, "nonliteral fixture lookup")
                if (name == "type" and len(node.args) >= 3) or name.endswith("new_class"):
                    issue("inheritance", module, node, "dynamic class construction")
            next_parents = parents + (node.name,) if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) else parents
            for child in ast.iter_child_nodes(node):
                visit(child, next_parents)

        visit(tree)

    def resolve(symbol):
        seen = set()
        while symbol not in seen:
            seen.add(symbol)
            changed = False
            for module in sorted(bindings, key=lambda name: (-len(name), name)):
                if symbol.startswith(module + "."):
                    suffix = symbol[len(module) + 1:]
                    first, _, rest = suffix.partition(".")
                    target = bindings[module].get(first)
                    if target:
                        symbol = target + ("." + rest if rest else "")
                        changed = True
                    break
            if not changed:
                return symbol
        return symbol

    for qualified, entry in classes.items():
        entry["bases"] = [resolve(base) for base in entry["bases"]]
        for base in entry["bases"]:
            if base not in classes and base != "builtins.object":
                unresolved.append({"kind": "inheritance", "file": entry["file"], "line": entry["line"],
                                   "expression": base, "reason": "base is external or unresolved statically"})
        entry["overrides"] = {}
        pending, seen = list(entry["bases"]), {qualified}
        while pending:
            base = pending.pop()
            if base in seen or base not in classes:
                continue
            seen.add(base)
            parent = classes[base]
            for method in sorted(set(entry["methods"]) & set(parent["methods"])):
                entry["overrides"].setdefault(method, []).append(base + "." + method)
            pending.extend(parent["bases"])
        entry["overrides"] = {method: sorted(owners) for method, owners in entry["overrides"].items()}
    for module, exports in reexports.items():
        reexports[module] = {name: resolve(target) for name, target in sorted(exports.items())}
    return {"reexports": reexports, "explicit_exports": explicit_exports, "classes": classes,
            "unresolved": sorted(unresolved, key=lambda row: (row["file"], row["line"], row["kind"], row["expression"]))}
