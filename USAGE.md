# Using cdkt: a step-by-step guide

This guide walks you from installation to a working Terraform configuration for your CDK app.
For how cdkt works inside, see [README.md](README.md).

**Contents**

1. [Before you start](#1-before-you-start)
2. [Install](#2-install)
3. [Add your API key](#3-add-your-api-key)
4. [Try it on the sample app](#4-try-it-on-the-sample-app)
5. [Convert your own CDK app](#5-convert-your-own-cdk-app)
6. [Read the output](#6-read-the-output)
7. [Review and fix](#7-review-and-fix)
8. [Move from CloudFormation to Terraform](#8-move-from-cloudformation-to-terraform)
9. [Configure cdkt for your team](#9-configure-cdkt-for-your-team)
10. [Command reference](#10-command-reference)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Before you start

You need:

| Requirement | Why | Check |
|---|---|---|
| Python 3.11 or newer | cdkt is a Python CLI | `python3 --version` |
| An Anthropic API key | the agents run on Claude | see [step 3](#3-add-your-api-key) |
| Your CDK project's source code | cdkt reads it; it does **not** need to build | a folder with `cdk.json` |
| Terraform CLI (optional, recommended) | enables `terraform validate` during conversion, and is required for [`--apply`](#7a-deploy-with-review-and-approval-optional) | `terraform version` |

You do **not** need AWS credentials, Node.js, or a working `cdk synth` just to convert. cdkt
never touches your AWS account during conversion; it only reads files and writes Terraform.
(AWS credentials *are* needed later, only if you choose to use `--apply` to actually deploy, or
to import already-deployed resources -- see [§7a](#7a-deploy-with-review-and-approval-optional)
and [§8](#8-move-from-cloudformation-to-terraform).)

## 2. Install

**Just want to run the CLI** -- install it like any other Python package, no repo clone needed:

```bash
python3 -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install "git+https://github.com/SushmaReddy12/cdkt-to-terraform-agent.git"
cdkt --help
```

Swap `` for another released tag, or drop it to install latest `main`. A prebuilt wheel or
internal index (e.g. AWS CodeArtifact) works the same way: `pip install cdkt-0.1.0-py3-none-any.whl`.

**Contributing to cdkt, or want the examples/tests locally** -- clone and install editable instead:

```bash
git clone https://github.com/SushmaReddy12/cdkt-to-terraform-agent.git
cd cdkt-to-terraform-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cdkt --help
```

**Also install, only if you need them:** Terraform CLI (`brew install terraform`) for `--apply`
or validation; AWS CLI + credentials (`aws configure`) for `--apply` or importing deployed
resources; Node.js + the `cdk` CLI, only to `cdk deploy`/`synth` a CDK app yourself -- cdkt itself
never needs Node.js, it only reads CDK source as text.

## 3. Add your API key

1. Create a key at <https://console.anthropic.com/settings/keys>.
2. Put it in a `.env` file in the directory you'll run `cdkt` from. `.env` is git-ignored:

   ```bash
   echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
   ```

   (Cloned the repo instead? `cp .env.example .env` and edit it.) Or export it in your shell:
   `export ANTHROPIC_API_KEY=sk-ant-...`

3. Check it's picked up:

   ```bash
   cdkt config
   ```

   The last line should read `# $ANTHROPIC_API_KEY is set`. cdkt never prints the key itself.

> **Keys never go in `cdkt.toml`.** That file is meant to be committed, and cdkt refuses to start
> if it finds a key there. If your key lives in a differently named variable, for example one
> injected by CI, point cdkt at it with `--api-key-env MY_VAR` or `api_key_env = "MY_VAR"` in
> `cdkt.toml`. `ANTHROPIC_AUTH_TOKEN` and `ant auth login` profiles also work.

## 4. Try it on the sample app

The repo includes a small single-stack app (an S3 bucket with an explicit bucket policy, plus a
DynamoDB table). Run it first to see what a conversion looks like:

```bash
cdkt convert examples/sample-app -o out/sample
```

You'll see progress from each agent:

```
Planning: surveying the CDK app...
Plan: 1 stack(s): StorageStack
Converting stack StorageStack -> modules/storage_stack
    wrote modules/storage_stack/main.tf
    validate modules/storage_stack: ok
    ...
Integrating: writing the root module and wiring stacks together...
Done. Validation passed; see MIGRATION_NOTES.md

Tokens: ... input, ... cache read, ... cache write, ... output
Output: /…/out/sample  (read MIGRATION_NOTES.md first)
```

A run takes minutes rather than seconds: each agent reads code, writes files, validates them, and
fixes what the checks report.

## 5. Convert your own CDK app

Point cdkt at the folder that contains `cdk.json`:

```bash
cdkt convert ~/code/my-cdk-app -o ~/code/my-terraform
```

**Recommended first run on a large app:** plan once, then convert one stack to check the quality
before paying for the whole thing:

```bash
# Plan + convert just one stack
cdkt convert ~/code/my-cdk-app -o tf --stack NetworkStack

# Happy with it? Convert the remaining stacks, reusing the plan from the first run
cdkt convert ~/code/my-cdk-app -o tf --plan tf/.cdkt/plan.json --stack DataStack --stack ApiStack
```

`--stack` takes the CDK stack id (as in `new MyStack(app, 'NetworkStack')`) or the module name,
and can be repeated. `--plan` skips the planning step and reuses `tf/.cdkt/plan.json`. The stack
names are listed in the plan and printed as `Plan: N stack(s): ...`. Leaving out `--stack`
converts every stack again, including ones already done.

After a partial run, validation reports problems for the stacks that aren't converted yet. That's
expected; they go away once every stack is done, because the root module is rebuilt on each run.

**Useful options**

| You want to... | Use |
|---|---|
| Convert several stacks at once (faster) or fewer (gentler on rate limits) | `--parallel 8` / `--parallel 1` |
| Spend less on a simple app | `--effort medium` |
| Get the most careful result on a complex app | `--effort xhigh` |
| Target an older AWS provider | `--aws-provider-version "~> 5.80"` |
| Skip `imports.tf` (new deployment, nothing to adopt) | `--no-imports` |
| Run without calling the Terraform CLI | `--no-terraform` |
| Review a real `terraform plan` and apply it on approval | `--apply` (see [§7a](#7a-deploy-with-review-and-approval-optional)) |

Exit codes: `0` success and output passes validation · `1` finished, but validation problems
remain or a stack failed · `2` configuration, credential, or API error.

## 6. Read the output

```
tf/
├── MIGRATION_NOTES.md      ← read this first
├── versions.tf             terraform + provider version constraints
├── providers.tf            provider "aws" (region, default_tags)
├── variables.tf            root inputs (region, cdk.json context values, cdk_project_dir, ...)
├── main.tf                 one module block per CDK stack, with cross-stack wiring
├── outputs.tf              your CfnOutputs, re-exported
├── imports.tf              import blocks for adopting deployed resources
├── modules/
│   └── <stack_name>/       one folder per CDK stack (one for each stack in your app)
│       ├── main.tf (split into topic files such as s3.tf, iam.tf, lambda.tf once large)
│       ├── variables.tf    what the stack receives (props, cross-stack values)
│       ├── outputs.tf      what the stack exposes
│       └── versions.tf
└── .cdkt/plan.json         the plan, reusable with --plan
```

How CDK ideas map to what you'll see:

| In your CDK app | In the Terraform |
|---|---|
| A stack | a module in `modules/<stack>/` |
| Passing a construct from one stack to another | an `output` in one module, fed to a `variable` of the other in root `main.tf` |
| `cdk.json` context, `tryGetContext` | root variables with the same defaults |
| `lambda.Function` | the function **plus** its IAM role, policy attachments, and log group |
| `bucket.grantRead(fn)` etc. | an `aws_iam_role_policy` with CDK's exact action list |
| `RemovalPolicy.RETAIN` | `lifecycle { prevent_destroy = true }` |
| `Code.fromAsset('lambda')` | `data "archive_file"` reading `${var.cdk_project_dir}/lambda` |

`MIGRATION_NOTES.md` sections:

- **Stacks**: which converted, failed, or were skipped.
- **Manual steps**: things you must do by hand, such as building a bundle or recreating a custom resource.
- **Unsupported / not converted**: constructs with no Terraform equivalent.
- **Behaviour differences**: places where the Terraform will behave differently from CDK.
- **Assumptions**: decisions an agent made where the source was ambiguous.
- **Importing existing resources**: import IDs, or how to find them.
- **Validation**: the final check results.

## 7. Review and fix

cdkt works from source code, so **treat the output as a strong first draft, not a final answer.**

1. **Work through `MIGRATION_NOTES.md`.** Every manual step and assumption is listed there.
2. **Re-run the checks** after any hand edits:
   ```bash
   cdkt validate tf
   ```
3. **Check against Terraform itself** (needs network access to the provider registry):
   ```bash
   cd tf && terraform init && terraform validate && terraform plan
   ```
4. **If one stack came out badly**, regenerate just that stack. Delete its module first so the
   agent starts clean, then run the integrator again to re-wire:
   ```bash
   rm -rf tf/modules/api_stack
   cdkt convert ~/code/my-cdk-app -o tf --plan tf/.cdkt/plan.json --stack ApiStack
   ```
5. **If the same mistake repeats across stacks**, teach cdkt the rule instead of fixing it by hand
   each time. See [custom knowledge](#custom-knowledge-your-conventions).

**Assets that need building.** For `NodejsFunction`, `PythonFunction`, or Docker image assets,
cdkt points Terraform at the code but does not build it. Run your usual build (e.g. `npm run
build`) before `terraform apply`; the notes say which paths are expected.

## 7a. Deploy with review and approval (optional)

By default `cdkt convert` only ever writes files -- it never calls `terraform plan`/`apply` with
real credentials, and never touches AWS. If you also want the agent itself to deploy, once it's
done converting, pass `--apply`:

```bash
cdkt convert ~/code/my-cdk-app -o tf --apply
```

This runs, after a successful conversion and commit to `tf/`:

1. `terraform init` and `terraform plan` in `tf/`, using whatever AWS credentials are already
   active in your shell (the same ones `aws`/`terraform` would use on their own -- cdkt doesn't
   read, store, or need its own copy of them).
2. The full plan is printed for you to review, along with the AWS account/identity it would run
   against (via `aws sts get-caller-identity`, if the `aws` CLI is installed).
3. If the plan has no changes, it stops there -- nothing to approve.
4. Otherwise, you're asked to type `yes` to confirm. Only on `yes` does it run
   `terraform apply` on *exactly* the plan you just reviewed (not a fresh one, so there's no
   drift between what you approved and what gets applied).

There is **no auto-approve flag** -- every run of `--apply` stops and waits for an interactive
`yes`, so it can't be scripted to apply unattended (e.g. in CI) without a human confirming at the
prompt.

Notes:

- This step needs the `terraform` CLI (same as `--no-terraform`'s validation step) and AWS
  credentials with permission to create/modify the resources being converted.
- If `imports.tf` is present (the default), `plan`/`apply` will try to **adopt** existing AWS
  resources rather than create new ones; delete or rename it first for a greenfield deploy (see
  [What is `imports.tf` doing?](#6-read-the-output)).
- Resources with `RemovalPolicy.RETAIN` in the original CDK app come out as
  `lifecycle { prevent_destroy = true }`; `terraform destroy` will refuse to remove them until
  that block is removed by hand, mirroring CDK's own behaviour.
- The saved plan file (`tf/tfplan`) is deleted automatically after you approve or decline --
  it can contain sensitive values in plaintext, so it's never left on disk or committed.

## 8. Move from CloudFormation to Terraform

If the CDK app is already deployed, adopt the existing resources instead of recreating them.
**Test this in a non-production account first.**

1. **Protect resources in CloudFormation.** In the CDK app, set `removalPolicy: RETAIN` on
   everything you'll import (or apply an Aspect that does it), then `cdk deploy`.
2. **Fill in import IDs.** Open `tf/imports.tf`. Blocks for resources with explicit names are
   ready to use. The commented `TODO` blocks need the physical ID, which you can list with:
   ```bash
   aws cloudformation describe-stack-resources --stack-name ApiStack \
     --query 'StackResources[].[LogicalResourceId,ResourceType,PhysicalResourceId]' --output table
   ```
3. **Plan the import.**
   ```bash
   cd tf && terraform init && terraform plan
   ```
   Aim for "N to import, 0 to add, 0 to destroy". Anything else means the Terraform doesn't yet
   match what's deployed; fix it before continuing.
4. **Apply**, then delete the CloudFormation stacks. Retained resources stay in place and are
   now managed by Terraform.
   ```bash
   terraform apply
   aws cloudformation delete-stack --stack-name ApiStack
   ```

## 9. Configure cdkt for your team

### Settings

Copy the example and commit it with your project:

```bash
cp cdkt.example.toml cdkt.toml
```

Each setting can be given in four places. Later ones win:

```
built-in default  →  cdkt.toml  →  CDKT_<NAME> environment variable  →  command-line flag
```

For example, `effort = "high"` in `cdkt.toml`, overridden for one CI job with
`CDKT_EFFORT=medium`, overridden again for one run with `--effort xhigh`. Run `cdkt config` any time
to see the values that will be used.

| Setting | Default | What it does |
|---|---|---|
| `model` | `claude-opus-5` | Claude model the agents use |
| `effort` | `high` | how hard the model thinks: `low`, `medium`, `high`, `xhigh`, `max` |
| `max_tokens` | `64000` | cap on a single model response |
| `parallel` | `4` | stacks converted at the same time |
| `max_turns` | `80` | step limit per agent before it gives up |
| `aws_provider_version` | `~> 6.0` | provider constraint written into the Terraform |
| `extra_knowledge` | none | your own reference files (see below) |
| `emit_imports` | `true` | write `imports.tf` |
| `run_terraform` | `true` | use the Terraform CLI for validation when available |
| `use_fallbacks` | `true` | retry on a fallback model if Claude declines a request |
| `use_compaction` | `true` | summarise very long agent conversations |
| `api_key_env` | `ANTHROPIC_API_KEY` | **name** of the variable holding the key |
| `base_url` | SDK default | send API calls through a gateway or proxy |
| `timeout_seconds`, `max_retries` | `900`, `4` | API client behaviour |

If your account or platform rejects the beta features, turn them off with `use_fallbacks = false`
and `use_compaction = false`.

### Custom knowledge: your conventions

cdkt ships with a reference guide for standard CDK constructs
([`cdkt/knowledge/cdk_to_terraform.md`](cdkt/knowledge/cdk_to_terraform.md)). Your organisation
probably has its own rules. Write them as Markdown and every agent will follow them; they take
precedence over the built-in guide.

```markdown
<!-- docs/terraform-conventions.md -->
# Our conventions

- Our CDK construct `SecureBucket` (from @acme/cdk-constructs) = S3 bucket + KMS key + access
  logging to the central log bucket. Convert it to our module:
  `module "x" { source = "app.terraform.io/acme/secure-bucket/aws" version = "~> 3.0" }`
- Never create VPCs; look them up with `data "aws_vpc"` by the tag `Name = "shared-${var.env}"`.
- Every resource that supports tags gets `tags = local.common_tags`.
```

```toml
# cdkt.toml  (paths are relative to this file)
[cdkt]
extra_knowledge = ["docs/terraform-conventions.md"]
```

or for one run: `cdkt convert app -o tf --knowledge docs/terraform-conventions.md`.

### In CI

```bash
export ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY_FROM_SECRET_STORE"
pip install -e .
cdkt convert ./infra -o ./terraform --parallel 2
cdkt validate ./terraform
```

Keep the key in your CI system's secret store, not in the repository.

## 10. Command reference

```
cdkt convert CDK_DIR [-o OUT] [--plan PLAN] [--stack ID ...] [--apply] [settings flags]
cdkt config  [settings flags]          show resolved settings (never shows the key)
cdkt validate TF_DIR [--no-terraform]  run cdkt's checks on any Terraform directory
```

`--apply` (see [§7a](#7a-deploy-with-review-and-approval-optional)) is only accepted by
`convert`, not by `config` or `validate`, and always requires an interactive `yes` before
applying -- there is no flag to bypass that prompt. Settings flags (both `convert` and `config`
accept them): `--config`, `--model`, `--effort`,
`--max-tokens`, `--parallel`, `--max-turns`, `--aws-provider-version`, `--knowledge`,
`--api-key-env`, `--base-url`, `--no-terraform`, `--no-imports`, `--no-fallbacks`,
`--no-compaction`. Run `cdkt convert --help` for details.

## 11. Troubleshooting

| Message or symptom | Cause | Fix |
|---|---|---|
| `no Anthropic credentials found` | no key in the environment | [step 3](#3-add-your-api-key); check `cdkt config` |
| `api_key_env is X but that environment variable is not set` | you named a variable that isn't exported | export it, or remove `api_key_env` |
| `the API rejected the credentials` | wrong, revoked, or mistyped key | create a new key in the console |
| `credentials don't belong in config files` | a key is in `cdkt.toml` | delete it there; use `.env` or the environment |
| `unknown setting 'modle'` | typo in `cdkt.toml` | the error lists the valid names |
| API error mentioning `fallbacks`, a beta, or `context_management` | account/platform doesn't support these betas | `--no-fallbacks --no-compaction` |
| Rate-limit (429) errors | too many stacks at once | `--parallel 1` or `2` |
| `terraform init failed, so terraform validate could not run` | no network access to the provider registry | fine: cdkt's own checks still run. Validate later on a machine with access |
| `stack X FAILED: gave up after 80 turns` | a very large or complex stack | re-run that stack with `--max-turns 150`, or split the stack |
| `planner found no stacks` | wrong directory | point cdkt at the folder with `cdk.json` |
| A resource is missing or wrong in the output | a construct the agents misunderstood | check `MIGRATION_NOTES.md`, then add a rule via `--knowledge` and re-run that stack |

Still stuck? Re-run with `ANTHROPIC_LOG=info` to see API request logs.


