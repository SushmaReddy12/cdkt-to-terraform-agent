from cdkt.validation import static_check, validate

ROOT_OK = '''
variable "region" {
  type    = string
  default = "us-east-1"
}
module "storage" {
  source      = "./modules/storage"
  environment = "dev"
}
module "api" {
  source     = "./modules/api"
  table_name = module.storage.table_name
}
output "table" {
  value = module.storage.table_name
}
'''

STORAGE = '''
variable "environment" {
  type = string
}
resource "aws_dynamodb_table" "items" {
  name     = "items-${var.environment}"
  hash_key = "pk"
  attribute {
    name = "pk"
    type = "S"
  }
}
output "table_name" {
  value = aws_dynamodb_table.items.name
}
'''

API = '''
variable "table_name" {
  type = string
}
data "aws_region" "current" {}
locals {
  name = "api-${data.aws_region.current.region}"
}
resource "aws_iam_role" "handler" {
  name               = local.name
  assume_role_policy = jsonencode({ Statement = [] })
}
'''


def write_tree(tmp_path, root=ROOT_OK, storage=STORAGE, api=API):
    (tmp_path / "modules" / "storage").mkdir(parents=True)
    (tmp_path / "modules" / "api").mkdir(parents=True)
    (tmp_path / "main.tf").write_text(root)
    (tmp_path / "modules" / "storage" / "main.tf").write_text(storage)
    (tmp_path / "modules" / "api" / "main.tf").write_text(api)
    return tmp_path


def test_clean_tree_has_no_problems(tmp_path):
    assert static_check(write_tree(tmp_path)) == []


def test_detects_undeclared_references(tmp_path):
    api = API + '''
resource "aws_lambda_function" "f" {
  role          = aws_iam_role.missing.arn
  function_name = var.nope
  description   = local.undefined
  handler       = data.aws_caller_identity.me.account_id
}
'''
    problems = "\n".join(static_check(write_tree(tmp_path, api=api)))
    assert "aws_iam_role.missing" in problems
    assert "var.nope" in problems
    assert "local.undefined" in problems
    assert "data.aws_caller_identity.me" in problems


def test_detects_module_interface_mismatches(tmp_path):
    root = ROOT_OK.replace("environment = \"dev\"", "env = \"dev\"").replace(
        "module.storage.table_name\n}", "module.storage.table_arn\n}"
    )
    problems = "\n".join(static_check(write_tree(tmp_path, root=root)))
    assert 'passes "env"' in problems
    assert 'required variable "environment"' in problems
    assert 'no output "table_arn"' in problems


def test_missing_module_source(tmp_path):
    root = ROOT_OK + 'module "ghost" {\n  source = "./modules/ghost"\n}\n'
    assert any("does not exist" in p for p in static_check(write_tree(tmp_path, root=root)))


def test_syntax_error_reported(tmp_path):
    problems = static_check(write_tree(tmp_path, api='resource "aws_iam_role" "x" {\n  name = \n'))
    assert any("HCL syntax error" in p for p in problems)


def test_comments_are_ignored(tmp_path):
    api = API + "# var.commented_out and aws_s3_bucket.gone are only in a comment\n"
    assert static_check(write_tree(tmp_path, api=api)) == []


def test_validate_empty_dir(tmp_path):
    ok, report = validate(tmp_path, run_terraform=False)
    assert not ok and "no .tf files" in report


def test_import_addresses_are_checked_as_addresses(tmp_path):
    tree = write_tree(tmp_path)
    (tree / "imports.tf").write_text(
        'import {\n  to = module.storage.aws_dynamodb_table.items\n  id = "t"\n}\n'
        'import {\n  to = module.storage.aws_s3_bucket.nope\n  id = "b"\n}\n'
    )
    problems = static_check(tree)
    assert len(problems) == 1 and "aws_s3_bucket.nope" in problems[0]


def test_comment_markers_inside_strings_are_not_comments(tmp_path):
    api = API + 'output "u" {\n  value = "https://${var.missing}.example.com#frag" # real comment var.ignored\n}\n'
    problems = "\n".join(static_check(write_tree(tmp_path, api=api)))
    assert "var.missing" in problems and "var.ignored" not in problems


def test_resource_prefixes_come_from_the_tree(tmp_path):
    # a provider cdkt has never heard of is still checked once it's declared
    api = API + '''
terraform {
  required_providers {
    datadog = {
      source = "DataDog/datadog"
    }
  }
}
resource "aws_iam_role_policy" "p" {
  role   = datadog_monitor.missing.id
  policy = "{}"
}
'''
    assert any("datadog_monitor.missing" in p for p in static_check(write_tree(tmp_path, api=api)))
