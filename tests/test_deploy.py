"""Tests for cdkt/deploy.py: the opt-in `terraform plan`/`apply` review flow.

These mock `subprocess.run` entirely -- no real terraform binary or AWS
credentials are needed to exercise the logic.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cdkt import deploy


def _completed(returncode: int) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout="", stderr="")


def test_plan_raises_if_terraform_missing(tmp_path):
    with patch("shutil.which", return_value=None):
        with pytest.raises(deploy.DeployError, match="terraform CLI not found"):
            deploy.plan(tmp_path)


def test_plan_raises_if_init_fails(tmp_path):
    with patch("shutil.which", return_value="/usr/bin/terraform"), \
         patch("subprocess.run", return_value=_completed(1)):
        with pytest.raises(deploy.DeployError, match="init failed"):
            deploy.plan(tmp_path)


def test_plan_no_changes_returns_zero(tmp_path):
    calls = [_completed(0), _completed(0)]  # init, then plan with no changes
    with patch("shutil.which", return_value="/usr/bin/terraform"), \
         patch("subprocess.run", side_effect=calls):
        code, plan_file = deploy.plan(tmp_path)
    assert code == 0
    assert plan_file == tmp_path / deploy.PLAN_FILE_NAME


def test_plan_changes_present_returns_two(tmp_path):
    calls = [_completed(0), _completed(2)]  # init ok, plan has changes (-detailed-exitcode)
    with patch("shutil.which", return_value="/usr/bin/terraform"), \
         patch("subprocess.run", side_effect=calls):
        code, plan_file = deploy.plan(tmp_path)
    assert code == 2
    assert plan_file.name == deploy.PLAN_FILE_NAME


def test_plan_raises_on_unexpected_exit_code(tmp_path):
    calls = [_completed(0), _completed(1)]  # init ok, plan errored
    with patch("shutil.which", return_value="/usr/bin/terraform"), \
         patch("subprocess.run", side_effect=calls):
        with pytest.raises(deploy.DeployError, match="plan failed"):
            deploy.plan(tmp_path)


def test_apply_success(tmp_path):
    plan_file = tmp_path / deploy.PLAN_FILE_NAME
    with patch("shutil.which", return_value="/usr/bin/terraform"), \
         patch("subprocess.run", return_value=_completed(0)):
        deploy.apply(tmp_path, plan_file)  # no exception == success


def test_apply_failure_raises(tmp_path):
    plan_file = tmp_path / deploy.PLAN_FILE_NAME
    with patch("shutil.which", return_value="/usr/bin/terraform"), \
         patch("subprocess.run", return_value=_completed(1)):
        with pytest.raises(deploy.DeployError, match="apply failed"):
            deploy.apply(tmp_path, plan_file)


def test_discard_removes_existing_file(tmp_path):
    plan_file = tmp_path / deploy.PLAN_FILE_NAME
    plan_file.write_text("fake plan contents")
    deploy.discard(plan_file)
    assert not plan_file.exists()


def test_discard_is_noop_if_missing(tmp_path):
    plan_file = tmp_path / deploy.PLAN_FILE_NAME
    deploy.discard(plan_file)  # should not raise


def test_current_aws_identity_returns_none_without_aws_cli():
    with patch("shutil.which", return_value=None):
        assert deploy.current_aws_identity() is None


def test_current_aws_identity_parses_output():
    with patch("shutil.which", return_value="/usr/bin/aws"), \
         patch("subprocess.run", return_value=SimpleNamespace(
             returncode=0, stdout="123456789012\tarn:aws:iam::123456789012:user/me\n", stderr="")):
        identity = deploy.current_aws_identity()
    assert identity == "123456789012  arn:aws:iam::123456789012:user/me"
