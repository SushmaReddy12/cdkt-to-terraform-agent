# CDK → Terraform conversion guide

This reference is loaded into every agent. It records the parts of the CDK that
are *implicit* in source code - the resources, defaults and wiring the CDK
generates for you - because those are what a naive translation misses.

## Target layout

```
<output>/
  versions.tf        # terraform + required_providers (aws at the configured version, archive, random as needed)
  providers.tf       # provider "aws" { region = var.aws_region }, default_tags if the app tags everything
  variables.tf       # root inputs: aws_region, environment/context values, anything the stacks need
  main.tf            # one module block per CDK stack, wiring outputs -> inputs for cross-stack refs
  outputs.tf         # re-export stack CfnOutputs as module outputs
  imports.tf         # optional import blocks for adopting already-deployed resources
  modules/<stack_snake_name>/
    main.tf | <topic>.tf   # resources (split by concern once a file passes ~300 lines)
    variables.tf
    outputs.tf
    versions.tf      # required_providers only (no provider blocks inside child modules)
  MIGRATION_NOTES.md # written by the tool from recorded notes
```

Naming: Terraform identifiers are snake_case from the CDK construct id
(`new s3.Bucket(this, 'AssetsBucket')` → `aws_s3_bucket.assets_bucket`). Keep
the construct path in a comment above each resource: `# CDK: MyStack/AssetsBucket`.

## General translation rules

| CDK concept | Terraform |
|---|---|
| `Stack` | child module under `modules/` |
| Stack props / constructor params | module `variable`s |
| `CfnOutput` | `output` in module + re-export in root `outputs.tf` |
| Cross-stack reference (passing a construct from stack A to stack B, `Fn.importValue`, `exportName`) | `output` in module A, `variable` in module B, wired in root `main.tf` |
| `CfnParameter` | `variable` |
| `this.node.tryGetContext('x')` / `cdk.json` context | root `variable` with the cdk.json value as `default` |
| `Stack.of(this).region` / `.account`, `Aws.REGION`, `Aws.ACCOUNT_ID` | `data "aws_region" "current" {}` / `data "aws_caller_identity" "current" {}` (`.region` on aws provider >= 6, `.name` on older versions) |
| `Aws.PARTITION`, `Aws.URL_SUFFIX` | `data "aws_partition" "current" {}` (`.partition`, `.dns_suffix`) |
| `Tags.of(scope).add(k, v)` | `default_tags` on the provider when applied at app level, else a `tags` map/local |
| `RemovalPolicy.RETAIN` (and the default for stateful resources such as buckets, tables, log groups with retention, user pools, KMS keys) | `lifecycle { prevent_destroy = true }` - add it and record a note |
| `RemovalPolicy.DESTROY` + `autoDeleteObjects: true` | `force_destroy = true` on `aws_s3_bucket` (the CDK custom resource is not needed) |
| `Duration.seconds/minutes/days(n)` | the number in the unit the Terraform argument expects (check: lambda `timeout` is seconds, SQS `visibility_timeout_seconds`, log retention days) |
| `Size.mebibytes(n)` | number in MB |
| `if`/loops/helper functions in source | `count`/`for_each`/locals - preserve the logic, don't unroll unless trivial |
| `CfnResource` / L1 `Cfn*` constructs | the direct `aws_*` equivalent; if none exists use `awscc_*` (Cloud Control provider) and note it |
| Construct IDs that produce physical names (`bucketName`, `tableName`, `functionName`...) | set the same name; when unset, CDK generates one - leave unset in TF (provider generates a name or use `name_prefix`) and record a note |

## Hidden resources the CDK creates (easy to miss)

- **`lambda.Function` / `NodejsFunction` / `PythonFunction`**: an execution role
  (`aws_iam_role` with `lambda.amazonaws.com` trust), attachment of
  `service-role/AWSLambdaBasicExecutionRole` (plus `AWSLambdaVPCAccessExecutionRole`
  when in a VPC), a default policy for every grant, and - when
  `logRetention` is set - an `aws_cloudwatch_log_group` named
  `/aws/lambda/<function_name>`. Code: `Code.fromAsset(dir)` →
  `data "archive_file"` whose `source_dir` is `"${var.cdk_project_dir}/<dir>"`.
  `cdk_project_dir` is a root variable (its default is given to you in the task:
  the CDK project path relative to the Terraform root) passed into every module
  that packages assets, with `output_path = "${path.root}/.build/<name>.zip"`.
  `NodejsFunction` bundles with esbuild: emit a note that the bundle must be
  built (e.g. `npm run build`) and point `archive_file` at the build output.
  `Code.fromInline` → `archive_file` with `source { content = ... }`.
  `Runtime.NODEJS_20_X` → `"nodejs20.x"`, etc. Default memory 128, timeout 3.
- **`grantRead/grantWrite/grantReadWrite/grantInvoke/grant*`**: IAM statements
  appended to the grantee role's default policy. Emit one
  `aws_iam_role_policy` per role (name it `<role>_default_policy`) built from
  `data "aws_iam_policy_document"`. Use the exact CDK action lists, e.g.
  - S3 `grantRead`: `s3:GetObject*`, `s3:GetBucket*`, `s3:List*` on bucket and `bucket/*`
  - S3 `grantWrite`: `s3:DeleteObject*`, `s3:PutObject`, `s3:PutObjectLegalHold`, `s3:PutObjectRetention`, `s3:PutObjectTagging`, `s3:PutObjectVersionTagging`, `s3:Abort*`
  - S3 `grantReadWrite`: union of the two
  - DynamoDB `grantReadData`: `dynamodb:BatchGetItem`, `dynamodb:GetRecords`, `dynamodb:GetShardIterator`, `dynamodb:Query`, `dynamodb:GetItem`, `dynamodb:Scan`, `dynamodb:ConditionCheckItem`, `dynamodb:DescribeTable` on the table ARN (and `/index/*` when indexes exist)
  - DynamoDB `grantWriteData`: `dynamodb:BatchWriteItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem`, `dynamodb:DeleteItem`, `dynamodb:DescribeTable`
  - SQS `grantSendMessages`: `sqs:SendMessage`, `sqs:GetQueueAttributes`, `sqs:GetQueueUrl`; `grantConsumeMessages`: `sqs:ReceiveMessage`, `sqs:ChangeMessageVisibility`, `sqs:GetQueueUrl`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes`
  - SNS `grantPublish`: `sns:Publish`
  - Lambda `grantInvoke`: `lambda:InvokeFunction` on the function ARN and `${arn}:*`
  - KMS-encrypted targets also add `kms:Decrypt` / `kms:Encrypt`/`kms:ReEncrypt*`/`kms:GenerateDataKey*` on the key.
- **`s3.Bucket`**: `aws_s3_bucket` plus separate resources for each feature:
  `aws_s3_bucket_versioning`, `aws_s3_bucket_server_side_encryption_configuration`
  (`S3_MANAGED` → `AES256`, `KMS` → `aws:kms`), `aws_s3_bucket_public_access_block`
  (`BLOCK_ALL` → all four true; CDK ≥ 2.x defaults to blocking public access),
  `aws_s3_bucket_ownership_controls` (default `BucketOwnerEnforced`),
  `aws_s3_bucket_lifecycle_configuration`, `aws_s3_bucket_cors_configuration`,
  `aws_s3_bucket_website_configuration`, `aws_s3_bucket_policy` (also created by
  `enforceSSL: true` → deny `aws:SecureTransport = false`), and
  `aws_s3_bucket_notification` for `addEventNotification` (plus
  `aws_lambda_permission` for Lambda targets - CDK uses a custom resource; TF does not need it).
- **`dynamodb.Table` / `TableV2`**: `aws_dynamodb_table`; `billingMode` default is
  `PROVISIONED` with 5/5 capacity for `Table` and `PAY_PER_REQUEST` for `TableV2`.
  Only key attributes (partition/sort keys, GSI/LSI keys) go in `attribute` blocks.
  `pointInTimeRecovery` → `point_in_time_recovery { enabled = true }`; `stream` → `stream_enabled` + `stream_view_type`.
- **`sqs.Queue`**: `aws_sqs_queue`; `deadLetterQueue` → `redrive_policy = jsonencode({ deadLetterTargetArn, maxReceiveCount })`. Default `visibility_timeout_seconds` 30.
- **`sns.Topic` + `addSubscription`**: `aws_sns_topic_subscription`; SQS subscriptions also need an `aws_sqs_queue_policy` allowing `sns.amazonaws.com` with `aws:SourceArn`; Lambda subscriptions need `aws_lambda_permission`.
- **`apigateway.LambdaRestApi` / `RestApi`**: `aws_api_gateway_rest_api`, one
  `aws_api_gateway_resource` per path part, `aws_api_gateway_method` +
  `aws_api_gateway_integration` (`AWS_PROXY`, `integration_http_method = "POST"`),
  `aws_api_gateway_deployment` (with `triggers = { redeployment = sha1(jsonencode([...])) }`
  and `lifecycle { create_before_destroy = true }`), `aws_api_gateway_stage`
  (default stage name `prod`), and `aws_lambda_permission` for
  `apigateway.amazonaws.com` scoped to `${execution_arn}/*/*`. `LambdaRestApi` with
  `proxy: true` (the default) creates `{proxy+}` + `ANY` on root and the proxy
  resource. CDK also creates a CloudWatch role + `aws_api_gateway_account` when
  `cloudWatchRole` is true (default true in older versions) - include it only if the source enables it and record a note.
- **`apigatewayv2.HttpApi`**: `aws_apigatewayv2_api` (`protocol_type = "HTTP"`), `aws_apigatewayv2_stage` `$default` with `auto_deploy = true`, integrations/routes, `aws_lambda_permission`.
- **`ec2.Vpc`**: by default 2 AZs (or `maxAzs`), a public and a private-with-egress
  subnet per AZ (`/18` masks from `10.0.0.0/16`), an IGW, **one NAT gateway per
  AZ** with EIPs, route tables and associations. Prefer the community module
  `terraform-aws-modules/vpc/aws` with matching `azs`, `public_subnets`,
  `private_subnets`, `enable_nat_gateway`, `single_nat_gateway = natGateways == 1`,
  and compute CIDRs with `cidrsubnet()`. `Vpc.fromLookup` → `data "aws_vpc"` + `data "aws_subnets"`.
- **`ecs_patterns.ApplicationLoadBalancedFargateService`** (and other L3 patterns):
  expands to cluster, task definition, task + execution roles, log group,
  security groups for ALB and service, ALB, target group, listener, and
  service. Write all of them explicitly; don't guess at hidden defaults - note any assumptions.
- **`events.Rule` + targets**: `aws_cloudwatch_event_rule` + `aws_cloudwatch_event_target` (+ `aws_lambda_permission` for `events.amazonaws.com`). `Schedule.rate(Duration.minutes(5))` → `rate(5 minutes)`; `Schedule.cron({...})` → `cron(m h dom mon dow year)` with `?` for whichever of day-of-month/day-of-week is unset.
- **`stepfunctions.StateMachine`**: `aws_sfn_state_machine` with `definition = jsonencode({...})` rendered from the chain; role + policy for each task's grants.
- **`logs.LogGroup`**: default retention `TWO_YEARS` (731 days) for the L2 `LogGroup`; `RetentionDays.ONE_WEEK` → 7.
- **`kms.Key`**: `aws_kms_key` (+ `aws_kms_alias` when `alias` is set); CDK's default key policy grants the account root full access - reproduce it.
- **`secretsmanager.Secret`**: `aws_secretsmanager_secret` + (when generating) `aws_secretsmanager_secret_version` with `random_password`, or leave value unmanaged and note it.
- **`cloudfront.Distribution`** with an S3 origin: `aws_cloudfront_distribution`, `aws_cloudfront_origin_access_control` (CDK ≥ 2.156 `S3BucketOrigin.withOriginAccessControl`) or OAI for older `S3Origin`, and the bucket policy statement allowing `cloudfront.amazonaws.com` with `AWS:SourceArn`.
- **`s3deploy.BucketDeployment`**: no native TF equivalent - emit `aws_s3_object` with `for_each = fileset(...)` and `etag = filemd5(...)`, and note content-type handling.
- **Custom resources (`cr.AwsCustomResource`, `CustomResource`, `Provider`)**: convert what they *do* when a TF resource exists (e.g. `putParameter` → `aws_ssm_parameter`); otherwise record a `manual` note. Never try to reproduce CDK's framework Lambdas.
- **Things to drop**: `CDKMetadata`, bootstrap version rules/parameters, asset staging buckets/parameters, `cdk.CfnOutput`s that only exist for CDK internals.

## Imports (adopting deployed resources)

CloudFormation-managed resources must be removed from their stack with
`DeletionPolicy: Retain` before Terraform adopts them. For each resource whose
physical ID is determinable from source (explicit name) emit in root `imports.tf`:

```hcl
import {
  to = module.storage_stack.aws_s3_bucket.assets_bucket
  id = "my-explicit-bucket-name"
}
```

For resources with generated names, emit the block commented out with
`id = "TODO: <how to find it, e.g. aws cloudformation describe-stack-resources --stack-name X>"`.

## Quality bar

- Every resource from the source must appear (or have a note explaining why not).
- No hard-coded account IDs or regions unless the CDK hard-codes them.
- Prefer references over string ARNs (`aws_s3_bucket.x.arn`, not `"arn:aws:s3:::name"`).
- Use `jsonencode()` / `aws_iam_policy_document`, never heredoc JSON.
- Keep resources in dependency-correct form; use `depends_on` only where CDK had an explicit `node.addDependency` or an implicit ordering TF can't infer (e.g. API Gateway deployment after methods).
