"""Orchestrates the CloudFormation to Terraform conversion.

This pipeline leverages the determinism of CloudFormation state to 
generate accurate Terraform configurations without the hallucination risks 
of parsing imperative source code.

    cfn_planner (1 agent)                 -> plan.json (Maps CFN Resources to modules)
    cfn_converters (1 agent per module)   -> modules/<module>/*.tf (parallel)
    integrator (1 agent)                  -> root *.tf, wiring cross-references
    final validation + MIGRATION_NOTES.md
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable

import anthropic

from .config import Settings
from .pipeline import ConversionReport


def convert(
    cfn_dir: Path,
    out_dir: Path,
    settings: Settings | None = None,
    client: anthropic.Anthropic | None = None,
    log: Callable[[str], None] = print,
    plan_file: Path | None = None,
    only_stacks: list[str] | None = None,
) -> ConversionReport:
    """Entry point for CloudFormation conversion."""
    settings = settings or Settings()
    
    log("CFN Planner: Reading template.yaml to map declarative state...")
    
    # TODO: Implement CFN Planner Agent
    # Unlike CDK, this planner just parses YAML/JSON looking for `Resources`, `Parameters`, and `Outputs`.
    
    log("CFN Converter: Translating AWS:: types to hashicorp/aws resources...")
    
    # TODO: Implement CFN Converter Agents
    # They will map `AWS::S3::Bucket` -> `aws_s3_bucket` using a deterministic mapping table.
    
    log("Integrator: Wiring CloudFormation Outputs to Terraform Variables...")
    
    # TODO: Implement Integrator
    # Reuses the exact same Integrator from `pipeline.py` since the intermediate HCL is the same.
    
    # Returning a dummy report for now
    return ConversionReport(
        plan={"stacks": []},
        validation_ok=True,
        validation_report="CloudFormation pipeline is a stub."
    )
