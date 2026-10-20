"""Tools the agents can call, and the dispatcher that runs them.

Each tool is a JSON-schema definition plus a handler. Agents get role-specific
subsets (see ``PLANNER_TOOLS`` etc.) so e.g. the planner cannot write files.
Handlers return plain strings; errors are raised as ``ToolError`` and turned
into ``is_error`` tool results by the agent loop so the model can recover.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .validation import validate
from .workspace import Workspace, WorkspaceError, _resolve_inside


class ToolError(Exception):
    pass


# Terminal tools end an agent's run; their input is the agent's result.
TERMINAL_TOOLS = {"submit_plan", "finish"}

NOTE_KINDS = ["manual_step", "assumption", "unsupported", "behavior_change", "import"]


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOL_DEFS: dict[str, dict] = {
    "list_source_files": {
        "description": (
            "List files in the CDK project (read-only). node_modules, cdk.out, build output and "
            "dotfiles are excluded. Optionally filter with a glob such as 'lib/**/*.ts' or '*.json'."
        ),
        "input_schema": _schema({"glob": {"type": "string", "description": "fnmatch-style glob on the relative path"}}, []),
    },
    "read_source_file": {
        "description": (
            "Read a file from the CDK project, returned with line numbers. Use start_line/end_line "
            "to read slices of large files."
        ),
        "input_schema": _schema(
            {
                "path": {"type": "string", "description": "path relative to the CDK project root"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            ["path"],
        ),
    },
    "search_source": {
        "description": (
            "Regex search (Python syntax) across the CDK project's source files. Returns "
            "'path:line: text' matches. Useful for finding where a construct or prop is used."
        ),
        "input_schema": _schema(
            {"pattern": {"type": "string"}, "glob": {"type": "string"}},
            ["pattern"],
        ),
    },
    "list_terraform_files": {
        "description": "List every file written so far in the Terraform output directory.",
        "input_schema": _schema({}, []),
    },
    "read_terraform_file": {
        "description": "Read a file from the Terraform output directory, with line numbers.",
        "input_schema": _schema({"path": {"type": "string"}}, ["path"]),
    },
    "write_terraform_file": {
        "description": (
            "Create or overwrite a file in the Terraform output directory with the complete file "
            "contents. Paths are relative to the output root, e.g. 'modules/api_stack/main.tf'. "
            "Always send the full file; there is no partial edit. Keep each file under ~400 lines "
            "by splitting resources across topic files (lambda.tf, iam.tf, ...)."
        ),
        "input_schema": _schema(
            {"path": {"type": "string"}, "content": {"type": "string", "description": "full file contents"}},
            ["path", "content"],
        ),
        "eager_input_streaming": True,
    },
    "delete_terraform_file": {
        "description": "Delete a file you previously wrote in the Terraform output directory.",
        "input_schema": _schema({"path": {"type": "string"}}, ["path"]),
    },
    "validate_terraform": {
        "description": (
            "Validate the generated Terraform. Runs HCL syntax and cross-reference checks, and "
            "terraform fmt/init/validate when the CLI is available. Pass 'directory' to validate "
            "one module (e.g. 'modules/api_stack'); omit it to validate the whole tree from the root."
        ),
        "input_schema": _schema({"directory": {"type": "string"}}, []),
    },
    "record_note": {
        "description": (
            "Record something a human must know about the conversion: a manual step, an "
            "assumption you made, a construct with no Terraform equivalent, a behaviour "
            "difference, or how to import an existing resource. These become MIGRATION_NOTES.md."
        ),
        "input_schema": _schema(
            {
                "kind": {"type": "string", "enum": NOTE_KINDS},
                "construct": {"type": "string", "description": "CDK construct path or id, e.g. 'ApiStack/Handler'"},
                "detail": {"type": "string"},
            },
            ["kind", "construct", "detail"],
        ),
    },
    "submit_plan": {
        "description": (
            "Submit the conversion plan once you have read enough of the CDK app. This ends "
            "your task. Every stack instantiated by the app must appear exactly once."
        ),
        "input_schema": _schema(
            {
                "project_summary": {"type": "string"},
                "cdk_language": {"type": "string", "description": "typescript, python, java, csharp, go or javascript"},
                "stacks": {
                    "type": "array",
                    "items": _schema(
                        {
                            "stack_id": {"type": "string", "description": "the CDK stack id/name as instantiated in the app"},
                            "module_name": {"type": "string", "description": "snake_case Terraform module name"},
                            "class_name": {"type": "string"},
                            "source_files": {"type": "array", "items": {"type": "string"}},
                            "description": {"type": "string"},
                            "constructs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "every construct in the stack, e.g. 'AssetsBucket: s3.Bucket (versioned, KMS)'",
                            },
                            "inputs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "values the stack receives: props, context, and cross-stack references (say from which stack)",
                            },
                            "outputs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "CfnOutputs and values other stacks consume",
                            },
                            "depends_on_stacks": {"type": "array", "items": {"type": "string"}},
                        },
                        ["stack_id", "module_name", "source_files", "description", "constructs", "inputs", "outputs", "depends_on_stacks"],
                    ),
                },
                "app_settings": {
                    "type": "string",
                    "description": "env/account/region, context values from cdk.json, app-wide tags and aspects",
                },
                "risks": {"type": "array", "items": {"type": "string"}, "description": "things likely to need manual attention"},
            },
            ["project_summary", "cdk_language", "stacks", "app_settings", "risks"],
        ),
        "eager_input_streaming": True,
    },
    "finish": {
        "description": (
            "Call when your assigned work is complete and validate_terraform is clean (or the "
            "remaining problems are recorded as notes). This ends your task."
        ),
        "input_schema": _schema(
            {
                "summary": {"type": "string", "description": "what you produced and anything left unresolved"},
                "unresolved": {"type": "array", "items": {"type": "string"}},
            },
            ["summary"],
        ),
    },
}

SOURCE_TOOLS = ["list_source_files", "read_source_file", "search_source"]
OUTPUT_TOOLS = ["list_terraform_files", "read_terraform_file", "write_terraform_file", "delete_terraform_file", "validate_terraform"]
PLANNER_TOOLS = SOURCE_TOOLS + ["submit_plan"]
CONVERTER_TOOLS = SOURCE_TOOLS + OUTPUT_TOOLS + ["record_note", "finish"]
INTEGRATOR_TOOLS = SOURCE_TOOLS + OUTPUT_TOOLS + ["record_note", "finish"]

_JSON_TYPES = {"string": str, "integer": int, "array": list, "object": dict, "boolean": bool, "number": (int, float)}


def validate_input(schema: dict, value: Any, where: str = "input") -> None:
    """Minimal JSON-schema check for the subset we use.

    Eager input streaming means the API no longer validates tool inputs, and a
    truncated stream can yield a silently partial object, so every tool input is
    checked here before it runs.
    """
    expected = schema.get("type")
    py_type = _JSON_TYPES.get(expected)
    if py_type and (not isinstance(value, py_type) or (expected == "integer" and isinstance(value, bool))):
        raise ToolError(f"{where} must be of type {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError(f"{where} must be one of {schema['enum']}")
    if expected == "object":
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                raise ToolError(f"{where} is missing required field '{req}'")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise ToolError(f"{where} has unknown field(s): {sorted(extra)}")
        for key, sub in props.items():
            if key in value:
                validate_input(sub, value[key], f"{where}.{key}")
    elif expected == "array" and "items" in schema:
        for i, item in enumerate(value):
            validate_input(schema["items"], item, f"{where}[{i}]")


@dataclass
class ToolBox:
    """The tools one agent may use, bound to a workspace."""

    workspace: Workspace
    names: list[str]
    run_terraform: bool = True
    # restrict writes to this prefix (a stack converter only owns its module dir)
    write_prefix: str | None = None
    log: Callable[[str], None] = print
    written: set[str] = field(default_factory=set)

    def definitions(self, streaming: bool = True) -> list[dict]:
        defs = []
        for name in self.names:
            spec = TOOL_DEFS[name]
            d = {"name": name, "description": spec["description"], "input_schema": spec["input_schema"]}
            if streaming and spec.get("eager_input_streaming"):
                d["eager_input_streaming"] = True
            defs.append(d)
        return defs

    def call(self, name: str, tool_input: Any) -> str:
        if name not in self.names:
            raise ToolError(f"tool '{name}' is not available to this agent")
        validate_input(TOOL_DEFS[name]["input_schema"], tool_input)
        handler = getattr(self, f"_t_{name}")
        try:
            return handler(**tool_input)
        except WorkspaceError as exc:
            raise ToolError(str(exc)) from exc

    # ---- handlers ----------------------------------------------------------

    def _t_list_source_files(self, glob: str | None = None) -> str:
        files = self.workspace.list_source_files(glob)
        return "\n".join(files) if files else "(no matching files)"

    def _t_read_source_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        return self.workspace.read_source_file(path, start_line, end_line)

    def _t_search_source(self, pattern: str, glob: str | None = None) -> str:
        hits = self.workspace.search_source(pattern, glob)
        return "\n".join(hits) if hits else "(no matches)"

    def _t_list_terraform_files(self) -> str:
        files = self.workspace.list_output_files()
        return "\n".join(files) if files else "(output directory is empty)"

    def _t_read_terraform_file(self, path: str) -> str:
        return self.workspace.read_output_file(path)

    def _check_write_scope(self, path: str) -> None:
        if self.write_prefix is None:
            return
        root = self.workspace.output_root
        allowed = (root / self.write_prefix).resolve()
        if allowed not in _resolve_inside(root, path).parents:
            raise ToolError(
                f"you may only write inside {self.write_prefix}/ - root-level wiring is done by the "
                "integration agent; record a note if the root needs something specific"
            )

    def _t_write_terraform_file(self, path: str, content: str) -> str:
        self._check_write_scope(path)
        result = self.workspace.write_output_file(path, content)
        self.written.add(path)
        self.log(f"    wrote {path}")
        return result

    def _t_delete_terraform_file(self, path: str) -> str:
        self._check_write_scope(path)
        return self.workspace.delete_output_file(path)

    def _t_validate_terraform(self, directory: str | None = None) -> str:
        root = self.workspace.output_root
        target = root
        if directory:
            target = _resolve_inside(root, directory)
            if not target.is_dir():
                raise ToolError(f"no such directory in the output: {directory}")
        ok, report = validate(target, run_terraform=self.run_terraform)
        if target != root:
            # init in a child module leaves provider caches behind; keep the tree clean
            shutil.rmtree(target / ".terraform", ignore_errors=True)
            (target / ".terraform.lock.hcl").unlink(missing_ok=True)
        self.log(f"    validate {directory or '.'}: {'ok' if ok else 'problems found'}")
        return ("PASSED\n" if ok else "FAILED\n") + report

    def _t_record_note(self, kind: str, construct: str, detail: str) -> str:
        self.workspace.notes.append({"kind": kind, "construct": construct, "detail": detail})
        return "noted"

    def _t_submit_plan(self, **plan) -> str:
        return json.dumps(plan)

    def _t_finish(self, summary: str, unresolved: list[str] | None = None) -> str:
        return summary
