"""Orchestrates the conversion: plan → convert each stack → integrate → report.

    planner (1 agent, read-only)          -> plan.json
    stack converters (1 agent per stack)  -> modules/<stack>/*.tf   (run in parallel)
    integrator (1 agent)                  -> root *.tf, cross-module fixes
    final validation + MIGRATION_NOTES.md
"""

from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import anthropic

from . import prompts
from .agent import Agent, AgentError, AgentResult
from .config import Settings, make_client
from .tools import CONVERTER_TOOLS, INTEGRATOR_TOOLS, PLANNER_TOOLS, ToolBox
from .validation import validate
from .workspace import Workspace

PLAN_PATH = ".cdkt/plan.json"



@dataclass
class ConversionReport:
    plan: dict
    stack_results: dict[str, str] = field(default_factory=dict)
    failed_stacks: dict[str, str] = field(default_factory=dict)
    integration_summary: str = ""
    validation_ok: bool = False
    validation_report: str = ""
    usage: dict = field(default_factory=dict)


def snake(name: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s if s and not s[0].isdigit() else f"stack_{s}"


def normalize_plan(plan: dict) -> dict:
    """Make module names valid and unique so each converter owns one directory."""
    seen: set[str] = set()
    for stack in plan.get("stacks", []):
        base = snake(stack.get("module_name") or stack["stack_id"])
        name, i = base, 2
        while name in seen:
            name, i = f"{base}_{i}", i + 1
        seen.add(name)
        stack["module_name"] = name
    if not plan.get("stacks"):
        raise AgentError("planner found no stacks in the CDK app")
    return plan


class _SerializedLog:
    """Serialises log lines from parallel agents."""

    def __init__(self, log: Callable[[str], None]):
        self._log, self._lock = log, threading.Lock()

    def __call__(self, msg: str) -> None:
        with self._lock:
            self._log(msg)


def _add_usage(total: dict, result: AgentResult) -> None:
    for k, v in result.usage.items():
        total[k] = total.get(k, 0) + v


def convert(
    cdk_dir: Path,
    out_dir: Path,
    settings: Settings | None = None,
    client: anthropic.Anthropic | None = None,
    log: Callable[[str], None] = print,
    plan_file: Path | None = None,
    only_stacks: list[str] | None = None,
) -> ConversionReport:
    """Convert a CDK project. ``plan_file`` reuses an earlier plan; ``only_stacks``
    limits conversion to stacks named by stack_id or module_name."""
    settings = settings or Settings()
    client = client or make_client(settings)
    log = _SerializedLog(log)
    ws = Workspace(Path(cdk_dir), Path(out_dir))
    cdk_project_dir = os.path.relpath(ws.source_root, ws.output_root)
    usage: dict = {}

    def make_agent(name: str, system: str, tools: list[str], write_prefix: str | None = None) -> Agent:
        box = ToolBox(ws, tools, run_terraform=settings.run_terraform, write_prefix=write_prefix, log=log)
        return Agent(
            client, name, system, box,
            model=settings.model, effort=settings.effort, max_turns=settings.max_turns,
            max_tokens=settings.max_tokens, use_fallbacks=settings.use_fallbacks,
            use_compaction=settings.use_compaction, log=log,
        )

    # 1. Plan ---------------------------------------------------------------
    if plan_file:
        plan = json.loads(Path(plan_file).read_text())
        log(f"Using existing plan {plan_file}")
    else:
        log("Planning: surveying the CDK app...")
        result = make_agent("planner", prompts.system_prompt("planner", settings), PLANNER_TOOLS).run(prompts.planner_task(ws.source_root.name))
        _add_usage(usage, result)
        if result.terminal_tool != "submit_plan":
            raise AgentError("planner stopped without submitting a plan")
        plan = result.terminal_input
    plan = normalize_plan(plan)
    plan_path = ws.output_root / PLAN_PATH
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2))
    log(f"Plan: {len(plan['stacks'])} stack(s): " + ", ".join(s["stack_id"] for s in plan["stacks"]))

    report = ConversionReport(plan=plan)
    stacks = plan["stacks"]
    if only_stacks:
        wanted = set(only_stacks)
        stacks = [s for s in stacks if s["stack_id"] in wanted or s["module_name"] in wanted]

    # 2. Convert each stack (in parallel) ------------------------------------
    def convert_stack(stack: dict) -> tuple[str, AgentResult]:
        name = stack["module_name"]
        log(f"Converting stack {stack['stack_id']} -> modules/{name}")
        agent = make_agent(f"stack:{name}", prompts.system_prompt("converter", settings), CONVERTER_TOOLS, write_prefix=f"modules/{name}")
        return name, agent.run(prompts.converter_task(stack, plan, cdk_project_dir))

    with ThreadPoolExecutor(max_workers=settings.parallel) as pool:
        futures = {pool.submit(convert_stack, s): s["module_name"] for s in stacks}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                _, result = fut.result()
            except (AgentError, anthropic.APIError) as exc:
                report.failed_stacks[name] = str(exc)
                ws.notes.append({"kind": "manual_step", "construct": name, "detail": f"Automatic conversion failed: {exc}"})
                log(f"  stack {name} FAILED: {exc}")
                continue
            _add_usage(usage, result)
            summary = (result.terminal_input or {}).get("summary") or result.final_text or "(no summary)"
            report.stack_results[name] = summary

    # 3. Integrate ----------------------------------------------------------
    log("Integrating: writing the root module and wiring stacks together...")
    integrator = make_agent("integrator", prompts.system_prompt("integrator", settings), INTEGRATOR_TOOLS)
    try:
        result = integrator.run(prompts.integrator_task(plan, report.stack_results, cdk_project_dir, settings.emit_imports))
        _add_usage(usage, result)
        report.integration_summary = (result.terminal_input or {}).get("summary") or result.final_text
    except (AgentError, anthropic.APIError) as exc:
        report.integration_summary = f"Integration failed: {exc}"
        log(f"  integrator FAILED: {exc}")

    # 4. Final validation + notes ------------------------------------------
    report.validation_ok, report.validation_report = validate(ws.output_root, run_terraform=settings.run_terraform)
    report.usage = usage
    write_notes(ws, report)
    log(f"Done. Validation {'passed' if report.validation_ok else 'reported problems'}; see MIGRATION_NOTES.md")
    return report


NOTE_TITLES = {
    "manual_step": "Manual steps",
    "unsupported": "Unsupported / not converted",
    "behavior_change": "Behaviour differences",
    "assumption": "Assumptions",
    "import": "Importing existing resources",
}


def write_notes(ws: Workspace, report: ConversionReport) -> None:
    lines = ["# Migration notes", "", f"Generated by cdkt from `{ws.source_root.name}`.", ""]
    lines += ["## Stacks", "", "| CDK stack | Terraform module | Status |", "|---|---|---|"]
    for s in report.plan["stacks"]:
        name = s["module_name"]
        status = "failed" if name in report.failed_stacks else ("converted" if name in report.stack_results else "skipped")
        lines.append(f"| {s['stack_id']} | `modules/{name}` | {status} |")
    lines.append("")
    for kind, title in NOTE_TITLES.items():
        notes = [n for n in ws.notes if n["kind"] == kind]
        if notes:
            lines += [f"## {title}", ""]
            lines += [f"- **{n['construct']}**: {n['detail']}" for n in notes]
            lines.append("")
    if report.plan.get("risks"):
        lines += ["## Risks flagged during planning", ""] + [f"- {r}" for r in report.plan["risks"]] + [""]
    lines += ["## Validation", "", "```", report.validation_report, "```", ""]
    lines += [
        "## Cutting over from CloudFormation",
        "",
        "1. Add `RemovalPolicy.RETAIN` (DeletionPolicy: Retain) to every resource you will import, and deploy.",
        "2. `terraform init && terraform plan` - review the import blocks in `imports.tf`.",
        "3. `terraform apply` to adopt the resources, then delete the CloudFormation stacks (retained resources survive).",
        "",
    ]
    (ws.output_root / "MIGRATION_NOTES.md").write_text("\n".join(lines), encoding="utf-8")
