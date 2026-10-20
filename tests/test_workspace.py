import pytest

from cdkt.workspace import Workspace, WorkspaceError


def make(tmp_path):
    src = tmp_path / "src"
    (src / "lib").mkdir(parents=True)
    (src / "lib" / "stack.ts").write_text("new s3.Bucket(this, 'B');\n")
    (src / "node_modules" / "x").mkdir(parents=True)
    (src / "node_modules" / "x" / "index.js").write_text("ignored")
    (src / "cdk.out").mkdir()
    (src / "cdk.out" / "t.json").write_text("{}")
    return Workspace(src, tmp_path / "out")


def test_lists_source_but_skips_generated_dirs(tmp_path):
    ws = make(tmp_path)
    assert ws.list_source_files() == ["lib/stack.ts"]


@pytest.mark.parametrize("bad", ["../secret", "/etc/passwd", "lib/../../x"])
def test_rejects_paths_outside_roots(tmp_path, bad):
    ws = make(tmp_path)
    with pytest.raises(WorkspaceError):
        ws.read_source_file(bad)
    with pytest.raises(WorkspaceError):
        ws.write_output_file(bad + ".tf", "x")


def test_write_and_read_output(tmp_path):
    ws = make(tmp_path)
    assert "created" in ws.write_output_file("modules/a/main.tf", 'resource "x" "y" {}')
    assert "overwrote" in ws.write_output_file("modules/a/main.tf", 'resource "x" "z" {}')
    assert '1| resource "x" "z" {}' in ws.read_output_file("modules/a/main.tf")


def test_refuses_unexpected_extensions(tmp_path):
    ws = make(tmp_path)
    with pytest.raises(WorkspaceError):
        ws.write_output_file("evil.exe", "x")


def test_search(tmp_path):
    ws = make(tmp_path)
    assert ws.search_source(r"s3\.Bucket") == ["lib/stack.ts:1: new s3.Bucket(this, 'B');"]
    with pytest.raises(WorkspaceError):
        ws.search_source("(")
