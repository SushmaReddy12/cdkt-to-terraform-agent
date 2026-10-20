"""End-to-end run of the pipeline against the sample app with scripted agents."""

import json

from cdkt.config import Settings
from cdkt.pipeline import convert, normalize_plan, snake

from conftest import FakeClient, message, tool

PLAN = {
    "project_summary": "two stacks",
    "cdk_language": "typescript",
    "stacks": [
        {"stack_id": "StorageStack", "module_name": "StorageStack", "source_files": ["lib/storage-stack.ts"],
         "description": "s3 + ddb", "constructs": ["ItemsTable: dynamodb.Table"], "inputs": ["environment"],
         "outputs": ["table name"], "depends_on_stacks": []},
        {"stack_id": "ApiStack", "module_name": "api-stack", "source_files": ["lib/api-stack.ts"],
         "description": "lambda + apigw", "constructs": ["ItemsHandler: lambda.Function"],
         "inputs": ["table from StorageStack"], "outputs": ["ApiUrl"], "depends_on_stacks": ["StorageStack"]},
    ],
    "app_settings": "region us-east-1",
    "risks": ["logRetention custom resource"],
}

STORAGE_TF = '''variable "environment" {
  type = string
}
resource "aws_dynamodb_table" "items_table" {
  name         = "sample-app-items-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  attribute {
    name = "pk"
    type = "S"
  }
}
output "table_name" {
  value = aws_dynamodb_table.items_table.name
}
'''

API_TF = '''variable "table_name" {
  type = string
}
output "api_url" {
  value = "https://example/${var.table_name}"
}
'''

ROOT_TF = '''module "storage_stack" {
  source      = "./modules/storage_stack"
  environment = var.environment
}
module "api_stack" {
  source     = "./modules/api_stack"
  table_name = module.storage_stack.table_name
}
variable "environment" {
  type    = string
  default = "dev"
}
'''


def router(kwargs):
    system = kwargs["system"][0]["text"]
    if "role: planner" in system:
        return "planner"
    if "role: integrator" in system:
        return "integrator"
    task = kwargs["messages"][0]["content"]
    return "storage" if "modules/storage_stack" in task else "api"


def test_snake():
    assert snake("StorageStack") == "storage_stack"
    assert snake("api-stack") == "api_stack"
    assert snake("3rdStack") == "stack_3rd_stack"


def test_normalize_plan_dedupes():
    plan = normalize_plan({"stacks": [{"stack_id": "A", "module_name": "x"}, {"stack_id": "B", "module_name": "X"}]})
    assert [s["module_name"] for s in plan["stacks"]] == ["x", "x_2"]


def test_end_to_end(tmp_path, sample_app):
    client = FakeClient({
        "planner": [message(tool("list_source_files")), message(tool("submit_plan", **PLAN))],
        "storage": [
            message(tool("write_terraform_file", path="modules/storage_stack/main.tf", content=STORAGE_TF)),
            message(tool("record_note", kind="import", construct="StorageStack/ItemsTable", detail="id = table name")),
            message(tool("validate_terraform", directory="modules/storage_stack")),
            message(tool("finish", summary="storage done")),
        ],
        "api": [
            message(tool("write_terraform_file", path="modules/api_stack/main.tf", content=API_TF)),
            message(tool("finish", summary="api done")),
        ],
        "integrator": [
            message(tool("write_terraform_file", path="main.tf", content=ROOT_TF)),
            message(tool("validate_terraform")),
            message(tool("finish", summary="wired")),
        ],
    }, router=router)
    out = tmp_path / "tf"
    report = convert(sample_app, out, Settings(run_terraform=False, parallel=2), client=client, log=lambda m: None)

    assert report.validation_ok, report.validation_report
    assert report.stack_results == {"storage_stack": "storage done", "api_stack": "api done"}
    assert json.loads((out / ".cdkt" / "plan.json").read_text())["stacks"][1]["module_name"] == "api_stack"
    notes = (out / "MIGRATION_NOTES.md").read_text()
    assert "StorageStack/ItemsTable" in notes and "| ApiStack | `modules/api_stack` | converted |" in notes

    # converters got the asset path relative to the output dir, integrator got both summaries
    api_task = next(r for r in client.requests if router(r) == "api")["messages"][0]["content"]
    assert '"../../' in api_task or '"..' in api_task
    integ_task = next(r for r in client.requests if router(r) == "integrator")["messages"][0]["content"]
    assert "storage done" in integ_task and "api done" in integ_task


def test_failed_stack_is_reported_and_integration_continues(tmp_path, sample_app):
    client = FakeClient({
        "planner": [message(tool("submit_plan", **PLAN))],
        "storage": [message(tool("write_terraform_file", path="modules/storage_stack/main.tf", content=STORAGE_TF)),
                    message(tool("finish", summary="ok"))],
        "api": [message(tool("list_source_files"))] * 3,  # runs out of turns
        "integrator": [message(tool("finish", summary="partial"))],
    }, router=router)
    report = convert(sample_app, tmp_path / "tf", Settings(run_terraform=False, max_turns=3), client=client, log=lambda m: None)
    assert "api_stack" in report.failed_stacks
    assert "| ApiStack | `modules/api_stack` | failed |" in (tmp_path / "tf" / "MIGRATION_NOTES.md").read_text()
