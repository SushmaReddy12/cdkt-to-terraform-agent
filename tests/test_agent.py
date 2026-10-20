import pytest

from cdkt.agent import Agent, AgentError
from cdkt.tools import CONVERTER_TOOLS, ToolBox, ToolError, validate_input, TOOL_DEFS
from cdkt.workspace import Workspace

from conftest import FakeClient, message, text, tool


def make_agent(tmp_path, script, tools=CONVERTER_TOOLS, write_prefix=None):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "app.ts").write_text("new Bucket(this, 'B');\n")
    ws = Workspace(src, tmp_path / "out")
    client = FakeClient({"default": script})
    box = ToolBox(ws, tools, run_terraform=False, write_prefix=write_prefix, log=lambda m: None)
    return Agent(client, "test", "system", box, model="claude-opus-5", log=lambda m: None), client, ws


def tool_results(request):
    return request["messages"][-1]["content"]


def test_runs_tools_then_returns_terminal_input(tmp_path):
    agent, client, ws = make_agent(tmp_path, [
        message(tool("read_source_file", path="app.ts"), tool("write_terraform_file", path="main.tf", content='resource "aws_s3_bucket" "b" {}')),
        message(text("done"), tool("finish", summary="wrote main.tf")),
    ])
    result = agent.run("convert")
    assert result.terminal_tool == "finish"
    assert result.terminal_input == {"summary": "wrote main.tf"}
    assert (ws.output_root / "main.tf").exists()
    # both tool results returned together in a single user message
    results = tool_results(client.requests[1])
    assert [r["type"] for r in results] == ["tool_result", "tool_result"]
    assert "new Bucket" in results[0]["content"]


def test_request_shape(tmp_path):
    agent, client, _ = make_agent(tmp_path, [message(tool("finish", summary="x"))])
    agent.run("go")
    req = client.requests[0]
    assert req["model"] == "claude-opus-5"
    assert req["thinking"] == {"type": "adaptive"}
    assert req["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in req["betas"]
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    write = next(t for t in req["tools"] if t["name"] == "write_terraform_file")
    assert write["eager_input_streaming"] is True


def test_tool_errors_are_returned_not_raised(tmp_path):
    agent, client, _ = make_agent(tmp_path, [
        message(tool("read_source_file", path="../etc/passwd"), tool("read_source_file")),
        message(tool("finish", summary="x")),
    ])
    agent.run("go")
    results = tool_results(client.requests[1])
    assert all(r["is_error"] for r in results)
    assert "escapes" in results[0]["content"]
    assert "missing required field 'path'" in results[1]["content"]


def test_truncated_tool_call_is_not_executed(tmp_path):
    agent, client, ws = make_agent(tmp_path, [
        message(tool("write_terraform_file", path="main.tf", content="resource \"x"), stop_reason="max_tokens"),
        message(tool("finish", summary="x")),
    ])
    agent.run("go")
    assert not (ws.output_root / "main.tf").exists()
    result = tool_results(client.requests[1])[0]
    assert result["is_error"] and "max_tokens" in result["content"]


def test_nudges_when_turn_ends_without_terminal_tool(tmp_path):
    agent, client, _ = make_agent(tmp_path, [
        message(text("I think I'm done")),
        message(tool("finish", summary="ok")),
    ])
    result = agent.run("go")
    assert result.terminal_tool == "finish"
    assert "without calling finish" in client.requests[1]["messages"][-1]["content"]


def test_gives_up_after_nudges(tmp_path):
    agent, _, _ = make_agent(tmp_path, [message(text("a")), message(text("b")), message(text("c"))])
    result = agent.run("go")
    assert result.terminal_tool is None and result.final_text == "c"


def test_refusal_raises(tmp_path):
    agent, _, _ = make_agent(tmp_path, [message(text(""), stop_reason="refusal")])
    with pytest.raises(AgentError):
        agent.run("go")


def test_malformed_stream_json_is_retried(tmp_path):
    agent, client, _ = make_agent(tmp_path, [ValueError("Unable to parse tool parameter JSON"), message(tool("finish", summary="x"))])
    assert agent.run("go").terminal_tool == "finish"
    assert len(client.requests) == 2


def test_write_scope_is_enforced(tmp_path):
    agent, client, ws = make_agent(tmp_path, [
        message(
            tool("write_terraform_file", path="main.tf", content="x"),
            tool("write_terraform_file", path="modules/api/../storage/main.tf", content="x"),
            tool("write_terraform_file", path="modules/api/main.tf", content="# ok"),
        ),
        message(tool("finish", summary="x")),
    ], write_prefix="modules/api")
    agent.run("go")
    results = tool_results(client.requests[1])
    assert results[0]["is_error"] and results[1]["is_error"]
    assert "is_error" not in results[2]
    assert not (ws.output_root / "modules" / "storage").exists()


def test_validate_input_rejects_wrong_types():
    schema = TOOL_DEFS["record_note"]["input_schema"]
    validate_input(schema, {"kind": "assumption", "construct": "A", "detail": "d"})
    with pytest.raises(ToolError):
        validate_input(schema, {"kind": "nonsense", "construct": "A", "detail": "d"})
    with pytest.raises(ToolError):
        validate_input(schema, {"kind": "assumption", "construct": 3, "detail": "d"})
    with pytest.raises(ToolError):
        validate_input(schema, {"kind": "assumption", "construct": "A", "detail": "d", "extra": 1})


def test_beta_features_can_be_turned_off(tmp_path):
    agent, client, _ = make_agent(tmp_path, [message(tool("finish", summary="x"))])
    agent.use_fallbacks = agent.use_compaction = False
    agent.run("go")
    req = client.requests[0]
    assert "fallbacks" not in req and "context_management" not in req and "betas" not in req
