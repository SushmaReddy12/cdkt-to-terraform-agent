"""Validation feedback for generated Terraform.

Two layers, so the agents always get a signal even without network access:

1. Static checks (always run, pure Python): HCL syntax via python-hcl2, plus
   cross-reference checks that catch the most common conversion mistakes -
   undeclared ``var.``/``local.`` references, references to resources that
   were never declared, module calls that pass unknown inputs or miss
   required ones, and ``module.x.y`` references to outputs that don't exist.
2. Terraform CLI (when ``terraform`` is on PATH): ``fmt`` (applied in place),
   ``init -backend=false`` and ``validate -json``. ``init`` needs to download
   providers; if it can't, that is reported and the static layer stands in.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import hcl2

REF_VAR = re.compile(r"(?<![\w.])var\.([A-Za-z_][\w-]*)")
REF_LOCAL = re.compile(r"(?<![\w.])local\.([A-Za-z_][\w-]*)")
REF_MODULE = re.compile(r"(?<![\w.])module\.([A-Za-z_][\w-]*)(?:\.([A-Za-z_][\w-]*))?")
REF_DATA = re.compile(r"(?<![\w.])data\.([a-z][a-z0-9_]*)\.([A-Za-z_][\w-]*)")
# Resource references are matched for provider prefixes found in the tree itself
# (required_providers + declared resource types), never a fixed provider list.
REF_RESOURCE_TMPL = r"(?<![\w.\"/:-])((?:{prefixes})_[a-z0-9_]+)\.([A-Za-z_][\w-]*)"
# A string literal is matched first so "//" or "#" inside strings is kept.
STRING_OR_COMMENT = re.compile(r'("(?:\\.|[^"\\\n])*")|(?:#|//)[^\n]*|/\*.*?\*/', re.S)
# Resource addresses in import/moved/removed blocks: `to = module.a.aws_x.y`
ADDRESS_LINE = re.compile(r"^\s*(?:to|from)\s*=\s*(\S+)\s*$", re.M)
BUILTIN_PREFIXES = {"terraform"}
# Meta-arguments a module block accepts that are not input variables.
MODULE_META_ARGS = {"source", "version", "providers", "depends_on", "count", "for_each"}


def _unquote(value):
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _blocks(parsed: dict, kind: str):
    """Yield (labels..., body) for each block of ``kind`` in a python-hcl2 parse."""
    for entry in parsed.get(kind, []) or []:
        for label, body in entry.items():
            yield _unquote(label), body


@dataclass
class ModuleIndex:
    path: Path
    variables: dict[str, bool] = field(default_factory=dict)  # name -> required
    locals: set[str] = field(default_factory=set)
    outputs: set[str] = field(default_factory=set)
    resources: set[tuple[str, str]] = field(default_factory=set)
    data: set[tuple[str, str]] = field(default_factory=set)
    modules: dict[str, dict] = field(default_factory=dict)
    providers: set[str] = field(default_factory=set)  # required_providers local names
    text: dict[str, str] = field(default_factory=dict)  # file -> comment-stripped text
    syntax_errors: list[str] = field(default_factory=list)


def index_module(module_dir: Path, root: Path) -> ModuleIndex:
    idx = ModuleIndex(path=module_dir)
    for tf in sorted(module_dir.glob("*.tf")):
        rel = tf.relative_to(root).as_posix()
        raw = tf.read_text(encoding="utf-8", errors="replace")
        idx.text[rel] = strip_comments(raw)
        try:
            parsed = hcl2.loads(raw)
        except Exception as exc:  # python-hcl2 raises lark errors of several types
            first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            idx.syntax_errors.append(f"{rel}: HCL syntax error: {first_line}")
            continue
        for name, body in _blocks(parsed, "variable"):
            idx.variables[name] = "default" not in (body or {})
        for entry in parsed.get("locals", []) or []:
            idx.locals.update(k for k in entry if k != "__is_block__")
        for name, _ in _blocks(parsed, "output"):
            idx.outputs.add(name)
        for rtype, named in _blocks(parsed, "resource"):
            for rname, _ in _blocks({"x": [named]}, "x"):
                idx.resources.add((rtype, rname))
        for dtype, named in _blocks(parsed, "data"):
            for dname, _ in _blocks({"x": [named]}, "x"):
                idx.data.add((dtype, dname))
        for name, body in _blocks(parsed, "module"):
            idx.modules[name] = body or {}
        for tblock in parsed.get("terraform", []) or []:
            for rp in tblock.get("required_providers", []) or []:
                idx.providers.update(_unquote(k) for k in rp if k != "__is_block__")
    return idx


def strip_comments(text: str) -> str:
    """Drop HCL comments but keep string literals (which may contain // or #)."""
    return STRING_OR_COMMENT.sub(lambda m: m.group(1) or "", text)


def _provider_prefixes(indexes: dict[Path, ModuleIndex]) -> set[str]:
    prefixes = set(BUILTIN_PREFIXES)
    for idx in indexes.values():
        prefixes |= idx.providers
        prefixes |= {t.split("_", 1)[0] for t, _ in idx.resources | idx.data}
    return {p for p in prefixes if p}


def _check_address(address: str, mdir: Path, indexes: dict[Path, ModuleIndex]) -> str | None:
    """Check an import/moved address like module.a.aws_x.y["k"]; return a problem or None."""
    parts = re.sub(r"\[[^\]]*\]", "", address).split(".")
    current = indexes.get(mdir)
    while len(parts) >= 2 and parts[0] == "module" and current is not None:
        if parts[1] not in current.modules:
            return f"address {address}: no module \"{parts[1]}\""
        current = _child_index(current.modules[parts[1]], current.path, indexes)
        parts = parts[2:]
    if current is None or len(parts) != 2:
        return None  # non-local module or unusual form; can't check offline
    if tuple(parts) not in current.resources:
        return f"address {address}: resource {parts[0]}.{parts[1]} is not declared in the target module"
    return None


def static_check(output_root: Path) -> list[str]:
    """Return a list of human-readable problems (empty list means clean)."""
    output_root = Path(output_root)
    module_dirs = sorted({p.parent for p in output_root.rglob("*.tf") if ".terraform" not in p.parts})
    indexes = {d: index_module(d, output_root) for d in module_dirs}
    problems: list[str] = []
    ref_resource = re.compile(REF_RESOURCE_TMPL.format(prefixes="|".join(sorted(map(re.escape, _provider_prefixes(indexes))))))

    for mdir, idx in indexes.items():
        problems.extend(idx.syntax_errors)
        for rel, text in idx.text.items():
            # import/moved/removed addresses are checked as addresses, not as references
            for address in ADDRESS_LINE.findall(text):
                problem = _check_address(address, mdir, indexes)
                if problem:
                    problems.append(f"{rel}: {problem}")
            text = ADDRESS_LINE.sub("", text)
            for name in sorted(set(REF_VAR.findall(text)) - set(idx.variables)):
                problems.append(f"{rel}: var.{name} is referenced but no variable \"{name}\" is declared in this module")
            for name in sorted(set(REF_LOCAL.findall(text)) - idx.locals):
                problems.append(f"{rel}: local.{name} is referenced but never defined in a locals block")
            for dtype, dname in sorted(set(REF_DATA.findall(text)) - idx.data):
                problems.append(f"{rel}: data.{dtype}.{dname} is referenced but not declared in this module")
            # the lookbehind excludes "." so data.aws_x.y tails are not matched here
            for rtype, rname in sorted(set(ref_resource.findall(text)) - idx.resources):
                problems.append(f"{rel}: {rtype}.{rname} is referenced but no resource \"{rtype}\" \"{rname}\" is declared in this module")
            for mname, attr in sorted(set(REF_MODULE.findall(text))):
                if mname not in idx.modules:
                    problems.append(f"{rel}: module.{mname} is referenced but no module \"{mname}\" block exists")
                    continue
                child = _child_index(idx.modules[mname], mdir, indexes)
                if child is not None and attr and attr not in child.outputs:
                    problems.append(f"{rel}: module.{mname}.{attr} is referenced but module \"{mname}\" has no output \"{attr}\"")

        for mname, body in idx.modules.items():
            source = _unquote(body.get("source", ""))
            if not isinstance(source, str) or not source.startswith("."):
                continue  # registry/git modules can't be checked offline
            child_dir = (mdir / source).resolve()
            if child_dir not in indexes:
                problems.append(f"{_rel(mdir, output_root)}: module \"{mname}\" source {source} does not exist or has no .tf files")
                continue
            child = indexes[child_dir]
            passed = {k for k in body if k not in MODULE_META_ARGS and k != "__is_block__"}
            for extra in sorted(passed - set(child.variables)):
                problems.append(f"{_rel(mdir, output_root)}: module \"{mname}\" passes \"{extra}\" but {source} declares no such variable")
            for missing in sorted(n for n, req in child.variables.items() if req and n not in passed):
                problems.append(f"{_rel(mdir, output_root)}: module \"{mname}\" does not set required variable \"{missing}\" of {source}")
    return problems


def _child_index(body: dict, parent_dir: Path, indexes: dict[Path, ModuleIndex]) -> ModuleIndex | None:
    source = _unquote(body.get("source", ""))
    if isinstance(source, str) and source.startswith("."):
        return indexes.get((parent_dir / source).resolve())
    return None


def _rel(path: Path, root: Path) -> str:
    rel = path.relative_to(root).as_posix()
    return "(root module)" if rel == "." else rel


def _run(cmd: list[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def terraform_check(output_root: Path) -> list[str]:
    """Run terraform fmt/init/validate on the root module; returns report lines."""
    tf = shutil.which("terraform")
    if tf is None:
        return ["terraform CLI not found on PATH; only static checks were run"]
    report: list[str] = []
    fmt = _run([tf, "fmt", "-recursive", "-list=true", "."], output_root)
    if fmt.returncode != 0:
        report.append("terraform fmt failed (syntax error):\n" + (fmt.stderr or fmt.stdout).strip()[:4000])
        return report
    if fmt.stdout.strip():
        report.append("terraform fmt reformatted: " + ", ".join(fmt.stdout.split()))
    init = _run([tf, "init", "-backend=false", "-input=false", "-no-color"], output_root)
    if init.returncode != 0:
        report.append(
            "terraform init failed, so `terraform validate` could not run "
            "(often no network access to the provider registry). Rely on the static checks. Details:\n"
            + init.stderr.strip()[-2000:]
        )
        return report
    val = _run([tf, "validate", "-json", "-no-color"], output_root)
    try:
        result = json.loads(val.stdout)
    except json.JSONDecodeError:
        report.append("terraform validate produced unparseable output:\n" + (val.stdout + val.stderr)[:4000])
        return report
    for diag in result.get("diagnostics", []):
        rng = diag.get("range") or {}
        where = f"{rng.get('filename', '?')}:{(rng.get('start') or {}).get('line', '?')}"
        report.append(f"terraform validate {diag.get('severity')}: {where}: {diag.get('summary')} - {diag.get('detail', '')}".strip())
    if result.get("valid") and not result.get("diagnostics"):
        report.append("terraform validate: configuration is valid")
    return report


def validate(output_root: Path, run_terraform: bool = True) -> tuple[bool, str]:
    """Run all checks. Returns (ok, report_text)."""
    output_root = Path(output_root)
    if not any(output_root.rglob("*.tf")):
        return False, "no .tf files have been written yet"
    lines: list[str] = []
    ok = True
    if run_terraform:
        tf_lines = terraform_check(output_root)
        lines.extend(tf_lines)
        if any(("validate error" in l) or l.startswith("terraform fmt failed") for l in tf_lines):
            ok = False
    problems = static_check(output_root)
    if problems:
        ok = False
        lines.append(f"static checks found {len(problems)} problem(s):")
        lines.extend(f"- {p}" for p in problems[:150])
        if len(problems) > 150:
            lines.append(f"- ... and {len(problems) - 150} more")
    else:
        lines.append("static checks: no problems found")
    return ok, "\n".join(lines)
