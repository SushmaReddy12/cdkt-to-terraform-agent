"""Tests for cdkt/cli.py's --apply flag and the review-and-apply flow.

There is no auto-approve: a plan must always be reviewed and confirmed
interactively before `terraform apply` runs. Mocks stand in for
deploy.plan/apply so no real terraform or AWS calls happen.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cdkt.cli import _review_and_apply, build_parser
from cdkt.deploy import DeployError


def test_convert_parser_accepts_apply_flag():
    args = build_parser().parse_args(["convert", "some-dir", "--apply"])
    assert args.apply is True


def test_convert_parser_defaults_apply_to_false():
    args = build_parser().parse_args(["convert", "some-dir"])
    assert args.apply is False


def test_convert_parser_has_no_yes_flag():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["convert", "some-dir", "--apply", "--yes"])


def test_review_and_apply_no_changes_skips_apply(tmp_path, capsys):
    with patch("cdkt.cli.current_aws_identity", return_value=None), \
         patch("cdkt.cli.tf_plan", return_value=(0, tmp_path / "tfplan")), \
         patch("cdkt.cli.tf_apply") as mock_apply:
        code = _review_and_apply(tmp_path)
    assert code == 0
    mock_apply.assert_not_called()
    assert "No changes needed" in capsys.readouterr().out


def test_review_and_apply_always_prompts_before_applying(tmp_path, capsys):
    with patch("cdkt.cli.current_aws_identity", return_value="111111111111  arn:aws:iam::111111111111:user/me"), \
         patch("cdkt.cli.tf_plan", return_value=(2, tmp_path / "tfplan")), \
         patch("builtins.input", return_value="yes") as mock_input, \
         patch("cdkt.cli.tf_apply") as mock_apply, \
         patch("cdkt.cli.tf_discard") as mock_discard:
        code = _review_and_apply(tmp_path)
    assert code == 0
    mock_input.assert_called_once()
    mock_apply.assert_called_once()
    mock_discard.assert_called_once()
    assert "Apply complete" in capsys.readouterr().out


def test_review_and_apply_declined_prompt_skips_apply(tmp_path, capsys):
    with patch("cdkt.cli.current_aws_identity", return_value=None), \
         patch("cdkt.cli.tf_plan", return_value=(2, tmp_path / "tfplan")), \
         patch("builtins.input", return_value="no"), \
         patch("cdkt.cli.tf_apply") as mock_apply:
        code = _review_and_apply(tmp_path)
    assert code == 0
    mock_apply.assert_not_called()
    assert "Aborted" in capsys.readouterr().out


def test_review_and_apply_confirmed_prompt_applies(tmp_path, capsys):
    with patch("cdkt.cli.current_aws_identity", return_value=None), \
         patch("cdkt.cli.tf_plan", return_value=(2, tmp_path / "tfplan")), \
         patch("builtins.input", return_value="yes"), \
         patch("cdkt.cli.tf_apply") as mock_apply, \
         patch("cdkt.cli.tf_discard"):
        code = _review_and_apply(tmp_path)
    assert code == 0
    mock_apply.assert_called_once()


def test_review_and_apply_plan_error_discards_stray_plan_file(tmp_path, capsys):
    with patch("cdkt.cli.current_aws_identity", return_value=None), \
         patch("cdkt.cli.tf_plan", side_effect=DeployError("terraform plan failed; see output above")), \
         patch("cdkt.cli.tf_discard") as mock_discard:
        code = _review_and_apply(tmp_path)
    assert code == 2
    mock_discard.assert_called_once_with(tmp_path / "tfplan")
    assert "terraform plan failed" in capsys.readouterr().err
