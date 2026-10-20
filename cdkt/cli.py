"""Command-line entry point: ``cdkt convert <cdk-dir> -o <out-dir>``.

Flags default to "unset" so cdkt.toml and CDKT_* environment variables apply
unless a flag is given explicitly (see cdkt/config.py for precedence).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

import anthropic

from .agent import AgentError
from .config import ConfigError, Settings, load_dotenv, load_settings
from .deploy import DeployError, apply as tf_apply, current_aws_identity, discard as tf_discard, plan as tf_plan
from .pipeline import convert
from .validation import validate
from .workspace import WorkspaceError

DEFAULTS = Settings()


def _review_and_apply(out_dir: Path) -> int:
    """Run `terraform plan` for review and, with explicit approval, `terraform apply` it.

    Unlike the rest of cdkt, this talks to real AWS using whatever credentials are
    already active in the environment -- it can create, change, or (if the generated
    config says so) destroy real, billable resources. There is no auto-approve: a plan
    is always shown and a human must type `yes` before anything is applied.
    """
    identity = current_aws_identity()
    print("\n--- Reviewing a real `terraform plan` against your AWS credentials ---")
    if identity:
        print(f"AWS identity: {identity}")
    else:
        print("(could not determine AWS identity; make sure credentials are configured)")

    try:
        exit_code, plan_file = tf_plan(out_dir)
    except DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        tf_discard(out_dir / "tfplan")
        return 2

    if exit_code == 0:
        print("\nNo changes needed -- nothing to apply.")
        tf_discard(plan_file)
        return 0

    print("\n" + "=" * 70)
    print("Review the plan above carefully before approving. This will create/change")
    print("real, billable AWS resources.")
    answer = input("Apply this plan now? Type 'yes' to proceed, anything else to abort: ").strip().lower()
    approved = answer == "yes"


    if not approved:
        print("Aborted: no changes were applied. (Saved plan discarded.)")
        tf_discard(plan_file)
        return 0

    try:
        tf_apply(out_dir, plan_file)
    except DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        tf_discard(plan_file)

    print("✅ Apply complete.")
    return 0


def _add_setting_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", type=Path, help="path to cdkt.toml (default: ./cdkt.toml if present)")
    p.add_argument("--model", help=f"Claude model id (default: {DEFAULTS.model})")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], help=f"(default: {DEFAULTS.effort})")
    p.add_argument("--max-tokens", type=int, help=f"output token cap per model response (default: {DEFAULTS.max_tokens})")
    p.add_argument("--parallel", type=int, help=f"stacks converted concurrently (default: {DEFAULTS.parallel})")
    p.add_argument("--max-turns", type=int, help=f"turn limit per agent (default: {DEFAULTS.max_turns})")
    p.add_argument("--aws-provider-version", help=f"hashicorp/aws version constraint (default: {DEFAULTS.aws_provider_version!r})")
    p.add_argument("--knowledge", action="append", dest="extra_knowledge",
                   help="extra reference .md for the agents, e.g. your org's module conventions (repeatable)")
    p.add_argument("--api-key-env", help=f"environment variable holding the API key (default: {DEFAULTS.api_key_env})")
    p.add_argument("--base-url", help="API base URL (e.g. a gateway); default is the SDK's")
    p.add_argument("--no-terraform", dest="run_terraform", action="store_const", const=False,
                   help="skip terraform CLI checks; static checks only")
    p.add_argument("--no-imports", dest="emit_imports", action="store_const", const=False, help="don't generate imports.tf")
    p.add_argument("--no-fallbacks", dest="use_fallbacks", action="store_const", const=False,
                   help="don't send the server-side fallback beta (for platforms/accounts that reject it)")
    p.add_argument("--no-compaction", dest="use_compaction", action="store_const", const=False,
                   help="don't send the compaction beta")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cdkt", description="Convert an AWS CDK app to Terraform with Claude agents.")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("convert", help="convert a CDK project directory")
    c.add_argument("cdk_dir", type=str, help="root of the project directory or an s3:// URI")
    c.add_argument("-o", "--out", type=Path, default=Path("terraform"), help="output directory (default: ./terraform)")
    c.add_argument("--plan", type=Path, help="reuse a plan.json from a previous run and skip the planner")
    c.add_argument("--stack", action="append", dest="stacks", help="only convert this stack (repeatable)")
    c.add_argument("--apply", action="store_true",
                   help="after a successful conversion, run `terraform plan` against your real AWS "
                        "credentials, show it for review, and apply it only after you type 'yes' to confirm")
    _add_setting_flags(c)

    s = sub.add_parser("config", help="show the resolved settings (never prints secrets)")
    _add_setting_flags(s)

    v = sub.add_parser("validate", help="run cdkt's checks on an existing Terraform directory")
    v.add_argument("tf_dir", type=Path)
    v.add_argument("--no-terraform", action="store_true")
    return p


SETTING_ARGS = ["model", "effort", "max_tokens", "parallel", "max_turns", "aws_provider_version", "extra_knowledge",
                "api_key_env", "base_url", "run_terraform", "emit_imports", "use_fallbacks", "use_compaction"]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "validate":
        ok, report = validate(args.tf_dir, run_terraform=not args.no_terraform)
        print(report)
        return 0 if ok else 1

    load_dotenv(Path.cwd() / ".env")
    try:
        settings = load_settings(args.config, {k: getattr(args, k) for k in SETTING_ARGS})
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "config":
        import os

        for key, value in asdict(settings).items():
            print(f"{key} = {value!r}")
        present = "set" if os.environ.get(settings.api_key_env) else "NOT set"
        print(f"# ${settings.api_key_env} is {present}")
        return 0

    try:
        import tempfile
        import subprocess
        import shutil
        from contextlib import ExitStack

        with ExitStack() as stack:
            # 1. Handle S3 Source fetching
            source_uri = args.cdk_dir
            if source_uri.startswith("s3://"):
                print(f"Downloading source from {source_uri}...")
                s3_temp_dir = stack.enter_context(tempfile.TemporaryDirectory())
                subprocess.run(["aws", "s3", "sync", source_uri, s3_temp_dir], check=True)
                local_source_dir = Path(s3_temp_dir)
            else:
                local_source_dir = Path(source_uri)
                if not local_source_dir.exists():
                    print(f"Error: Source {local_source_dir} does not exist.", file=sys.stderr)
                    return 1

            # 2. Scalable Intent Registry (Future-proof for all AWS sources)
            PIPELINES = {
                "CDK": {
                    "detect": lambda d: (d / "cdk.json").exists(),
                    "module": "cdkt.pipeline"
                },
                "CFN": {
                    "detect": lambda d: (d / "template.yaml").exists() or (d / "template.json").exists(),
                    "module": "cdkt.cfn_pipeline"
                },
                "SAM": {
                    "detect": lambda d: (d / "samconfig.toml").exists(),
                    "module": "cdkt.sam_pipeline"  # Stub for future
                }
                # Future pipelines (Amplify, Serverless, Pulumi) go here.
            }

            intent = None
            for name, config in PIPELINES.items():
                if config["detect"](local_source_dir):
                    intent = name
                    break

            if intent:
                print(f"Detected intent: {intent} (Deterministic match).")
            else:
                # Fallback to LLM if ambiguous (e.g. custom scripts, raw boto3)
                print("Source ambiguous. Using LLM for fallback intent detection...")
                import os
                api_key = os.environ.get(settings.api_key_env)
                client = anthropic.Anthropic(api_key=api_key, base_url=settings.base_url)

                from .prompts import intent_detector_task
                files = [p.name for p in local_source_dir.iterdir() if p.name != ".git"][:50]
                prompt = intent_detector_task(files, list(PIPELINES.keys()))

                response = client.messages.create(
                    model=settings.model,
                    max_tokens=10,
                    messages=[{"role": "user", "content": prompt}]
                )
                intent = response.content[0].text.strip().upper()
                if intent not in PIPELINES:
                    print(f"LLM returned unknown intent: {intent}. Assuming CDK pipeline as default...")
                    intent = "CDK"

            # Dynamically load the correct pipeline module
            print(f"Routing to {intent} pipeline...")
            import importlib
            try:
                pipeline_module = importlib.import_module(PIPELINES[intent]["module"])
                convert_fn = pipeline_module.convert
            except ImportError:
                print(f"Error: The {intent} pipeline is planned but not yet implemented.")
                return 1

            # 3. Safe Sandboxing: Run conversion in a temporary directory
            temp_out_str = stack.enter_context(tempfile.TemporaryDirectory())
            temp_out = Path(temp_out_str)
            print("Starting conversion in isolated sandbox...")
            # Call the appropriate pipeline (CDK or CFN)
            report = convert_fn(local_source_dir, temp_out, settings, plan_file=args.plan, only_stacks=args.stacks)

            u = report.usage
            print(
                f"\nTokens: {u.get('input_tokens', 0):,} input, {u.get('cache_read_input_tokens', 0):,} cache read, "
                f"{u.get('cache_creation_input_tokens', 0):,} cache write, {u.get('output_tokens', 0):,} output"
            )
            if report.failed_stacks:
                print(f"Failed stacks: {', '.join(report.failed_stacks)}", file=sys.stderr)

            # 3. Commit Phase: Only overwrite user's disk if validation passed
            if report.validation_ok and not report.failed_stacks:
                if args.out.exists():
                    shutil.rmtree(args.out)
                shutil.copytree(temp_out, args.out)
                print(f"✅ Success! Output safely committed to: {args.out.resolve()}  (read MIGRATION_NOTES.md first)")

                if args.apply:
                    return _review_and_apply(args.out)
                return 0
            else:
                print(f"❌ Validation failed. Aborting commit to {args.out.resolve()} to prevent corruption.")
                print(f"Partial broken output remains in {temp_out} if you wish to inspect it before it is destroyed.")
                return 1
    except subprocess.CalledProcessError as exc:
        print(f"error: Failed to download from S3: {exc}", file=sys.stderr)
        return 2
    except (WorkspaceError, AgentError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except anthropic.AuthenticationError:
        print(f"error: the API rejected the credentials. Set {settings.api_key_env} "
              "(or run `ant auth login`); create a key at https://console.anthropic.com/settings/keys", file=sys.stderr)
        return 2
    except anthropic.APIError as exc:
        print(f"error: Claude API request failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
