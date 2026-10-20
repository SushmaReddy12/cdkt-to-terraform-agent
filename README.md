# cdkt: AWS CDK → Terraform, with Claude agents

`cdkt` reads an AWS CDK app's **source code** (TypeScript, Python, Java, C#, Go) and writes an
equivalent Terraform configuration: one module per CDK stack, a root module that wires them
together, `import` blocks for adopting already-deployed resources, and a `MIGRATION_NOTES.md`
listing everything a human needs to check.

No `cdk synth` needed: the CDK app doesn't have to build, and no AWS credentials are required.

> **New here? Start with the [step-by-step usage guide](USAGE.md)**: install, API key, first
> conversion, reviewing the output, cutting over from CloudFormation, and troubleshooting. This
> README covers how cdkt works inside.

## How it works

```
  S3 URI or ──▶ ┌──────────────┐    ┌──▶ CDK Pipeline (Agent Planners & Converters)
  Local Dir     │ Intent Router│────┤
                └──────────────┘    └──▶ CloudFormation Pipeline (not yet implemented, stub only)
                        │
                (writes to isolated tempdir)
                        ▼
                ┌──────────────┐  root main.tf / variables / outputs / imports.tf,
                │  Integrator  │  wires module outputs → inputs, validates the tree
                └──────────────┘
                        ▼
                ┌──────────────┐  if terraform validate passes, commits 
                │  Sandboxing  │  the safe output to ./terraform on disk
                └──────────────┘
```

### 1. Intent Router & S3 Fetching
The CLI entrypoint (`cdkt/cli.py`) handles fetching (if an `s3://` URI is passed, it uses `aws s3 sync` into a temp folder). It then scans the directory (e.g. `cdk.json` vs `template.yaml`) to deterministically route the project to the right conversion pipeline, falling back to an LLM if ambiguous.

Only the **CDK pipeline** (`cdkt/pipeline.py`) is implemented. The CloudFormation pipeline
(`cdkt/cfn_pipeline.py`) is a stub -- its planner/converter/integrator agents are not yet written,
and running `cdkt` against a `template.yaml`-only project returns a dummy, no-op report. (You can
still *import* resources that were deployed via CloudFormation/CDK into a CDK-sourced conversion;
see [USAGE.md §8](USAGE.md#8-move-from-cloudformation-to-terraform) -- that's a different thing
from converting a raw CloudFormation template as the source.)

### 2. The Conversion Pipeline
The CDK pipeline uses Claude tool-use loops (`cdkt/agent.py`) configured with role-specific tools (`cdkt/tools.py`):


| Tool | Planner | Converter | Integrator |
|---|:-:|:-:|:-:|
| `list_source_files`, `read_source_file`, `search_source` | ✓ | ✓ | ✓ |
| `write/read/list/delete_terraform_file` |  | ✓ (own module only) | ✓ |
| `validate_terraform` |  | ✓ | ✓ |
| `record_note` → MIGRATION_NOTES.md |  | ✓ | ✓ |
| `submit_plan` / `finish` (end the agent) | ✓ | ✓ | ✓ |

**Parallel Map-Reduce.** The Planner surveys the project and creates a `plan.json`. Then, multiple Converter agents run in parallel (one per stack/module) to translate AWS constructs into HCL. 

### 3. The Integrator & Safe Sandboxing
The stack boundary is the module boundary, so variables and outputs become the contract. The **Integrator Agent** wires these together into a root `main.tf`. All of this happens inside an ephemeral `tempfile.TemporaryDirectory()`. The CLI only commits the final code to your local disk if the final `terraform validate` strictly passes, ensuring your workspace is never corrupted by a half-finished hallucination.

**Validation feedback** (`cdkt/validation.py`) always runs, even offline:
- HCL syntax (python-hcl2), undeclared `var.`/`local.`/`data.`/resource references (for
  whichever providers the tree declares), module calls passing unknown or missing required
  variables, `module.x.y` references to outputs that don't exist, and `import`/`moved`
  addresses that point at resources which don't exist;
- plus `terraform fmt`, `init -backend=false` and `validate` when the `terraform` CLI is on
  `PATH` and can reach the provider registry.

**Safety.** The CDK project is read-only to the agents and every path is confined to its root;
writes go only to the output directory (and a converter only to its own module). CDK source is
treated as untrusted data in the prompts.

## Setup

```bash
pip install -e ".[dev]"      # Python 3.11+
cp .env.example .env               # then put your key in .env (git-ignored)
cp cdkt.example.toml cdkt.toml     # optional: project settings, safe to commit
```

Get an API key from https://console.anthropic.com/settings/keys. cdkt reads it **only from the
environment**: `ANTHROPIC_API_KEY`, or whichever variable `api_key_env` names, optionally loaded
from `.env`. `ANTHROPIC_AUTH_TOKEN` and `ant auth login` profiles also work. It refuses to start if
a key is put in `cdkt.toml`, and it fails fast with a clear message if no credentials are found.

## Configuration

Nothing is fixed in code. Every setting resolves as
**default < `cdkt.toml` < `CDKT_<NAME>` env var < CLI flag**. `cdkt config` prints the resolved values
(it reports whether the key is set, never the key).

| Setting | Default | Flag / env |
|---|---|---|
| `model` | `claude-opus-5` | `--model` / `CDKT_MODEL` |
| `effort` | `high` | `--effort` / `CDKT_EFFORT` |
| `max_tokens`, `max_turns`, `parallel` | 64000, 80, 4 | `--max-tokens` `--max-turns` `--parallel` |
| `aws_provider_version` | `~> 6.0` | `--aws-provider-version` |
| `extra_knowledge` | none | `--knowledge file.md` (repeatable) |
| `use_fallbacks`, `use_compaction` | on | `--no-fallbacks`, `--no-compaction` |
| `run_terraform`, `emit_imports` | on | `--no-terraform`, `--no-imports` |
| `api_key_env`, `base_url`, `timeout_seconds`, `max_retries` | `ANTHROPIC_API_KEY`, SDK default, 900, 4 | `--api-key-env`, `--base-url` |

**Your own conversion rules.** The built-in reference (`cdkt/knowledge/cdk_to_terraform.md`) covers
standard CDK constructs. Add your organisation's conventions, such as internal construct libraries,
approved Terraform modules, naming and tagging rules, as Markdown files via `extra_knowledge`.
They are given to every agent and take precedence over the built-in guidance.

## Usage

```bash
cdkt convert path/to/cdk-app -o out/terraform
cdkt convert path/to/cdk-app -o tf --stack ApiStack --plan tf/.cdkt/plan.json   # redo one stack
cdkt convert path/to/cdk-app -o tf --apply                                     # also plan+apply, with your approval
cdkt validate out/terraform         # run the checks on any Terraform tree
cdkt config                         # show resolved settings
```

`--apply` is the only command that touches real AWS: after a successful conversion it runs a real
`terraform plan` with whatever credentials are active in your shell, shows it to you, and applies
it only once you type `yes` at an interactive prompt -- there is no auto-approve flag. See
[USAGE.md](USAGE.md#7a-deploy-with-review-and-approval-optional) for details. Every other command,
and `convert` without `--apply`, never calls `terraform apply` and never reaches AWS.

The run saves its plan to `<out>/.cdkt/plan.json`; pass it back with `--plan` to skip re-planning
when iterating on individual stacks.

Output layout:

```
out/terraform/
  versions.tf providers.tf variables.tf main.tf outputs.tf imports.tf
  modules/<stack>/{main.tf, *.tf, variables.tf, outputs.tf, versions.tf}
  MIGRATION_NOTES.md
```

## Claude API usage

- Model `claude-opus-5` with adaptive thinking and configurable effort, streamed so large file
  writes don't time out; `eager_input_streaming` on the large-input tools with client-side
  input validation, and a truncated (`max_tokens`) tool call is never executed.
- `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`, toggle `use_fallbacks`) so a policy decline is retried
  server-side on a fallback model instead of failing the run.
- Server-side compaction (beta `compact-2026-01-12`, toggle `use_compaction`) for long conversions of big stacks.
- The system prompt (role instructions + knowledge base) is identical for every agent of a role and
  marked for caching, so parallel stack converters share it; the conversation history is
  append-only and auto-cached.

## Development

```bash
pytest          # uses a scripted fake client; no API calls
```

`tests/test_pipeline.py` runs the whole planner → converters → integrator flow against
`examples/sample-app` with scripted agents.

## Limitations

- Source-only means the agent infers what CDK would synthesize; unusual third-party constructs
  or heavy runtime logic may be misread. Check `MIGRATION_NOTES.md` and review `terraform plan`.
- Assets that need a build step (`NodejsFunction`, Docker images) are referenced, not built.
- Import IDs are only filled in when a physical name is explicit in the source.
