"""Optional post-conversion deployment: review a real ``terraform plan`` and,
only with explicit approval, ``terraform apply`` it.

Nothing in this module runs unless the user opts in with ``cdkt convert --apply``.
Every other part of cdkt (planning, codegen, sandboxed validation) never touches
a real cloud account -- this is the one path that does, using whatever AWS
credentials are already active in the environment (the same ones the
``terraform``/``aws`` CLIs would pick up on their own).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

PLAN_FILE_NAME = "tfplan"


class DeployError(Exception):
    """Raised when terraform (or the environment) isn't in a state we can deploy from."""


def _terraform_bin() -> str:
    tf = shutil.which("terraform")
    if not tf:
        raise DeployError(
            "terraform CLI not found on PATH. Install it: https://developer.hashicorp.com/terraform/install"
        )
    return tf


def current_aws_identity() -> str | None:
    """Best-effort, informational-only: who/what account `terraform apply` would act as.

    Returns None (never raises) if the `aws` CLI isn't installed or isn't configured --
    the plan/apply flow below doesn't depend on this, it's purely shown to the user
    so they know which account they're about to change before they confirm.
    """
    aws = shutil.which("aws")
    if not aws:
        return None
    try:
        result = subprocess.run(
            [aws, "sts", "get-caller-identity", "--query", "[Account,Arn]", "--output", "text"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip().replace("\t", "  ")


def plan(out_dir: Path) -> tuple[int, Path]:
    """Run `terraform init` then `terraform plan -detailed-exitcode`, streaming output live.

    Returns (exit_code, plan_file):
      - exit_code 0: no changes needed (nothing to apply).
      - exit_code 2: changes are planned (the normal "go ask the user" case).
      - anything else: `init` or `plan` failed; raises DeployError instead.
    """
    tf = _terraform_bin()

    init = subprocess.run([tf, "init", "-input=false", "-no-color"], cwd=out_dir)
    if init.returncode != 0:
        raise DeployError("terraform init failed; see output above")

    plan_file = out_dir / PLAN_FILE_NAME
    result = subprocess.run(
        [tf, "plan", "-input=false", "-no-color", "-detailed-exitcode", f"-out={PLAN_FILE_NAME}"],
        cwd=out_dir,
    )
    if result.returncode not in (0, 2):
        raise DeployError("terraform plan failed; see output above")
    return result.returncode, plan_file


def apply(out_dir: Path, plan_file: Path) -> None:
    """Apply exactly the plan that was reviewed (no re-planning, so no drift between what
    the user approved and what gets applied)."""
    tf = _terraform_bin()
    result = subprocess.run([tf, "apply", "-input=false", "-no-color", plan_file.name], cwd=out_dir)
    if result.returncode != 0:
        raise DeployError("terraform apply failed; see output above")


def discard(plan_file: Path) -> None:
    """Remove the saved plan file once it's been applied or rejected (it can contain
    sensitive values in plaintext, so it shouldn't linger on disk or get committed)."""
    if plan_file.exists():
        plan_file.unlink()
