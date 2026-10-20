import pytest

from cdkt import prompts
from cdkt.config import ConfigError, Settings, load_dotenv, load_settings, make_client


def test_defaults_without_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_settings(env={}) == Settings()


def test_precedence_file_then_env_then_flags(tmp_path):
    cfg = tmp_path / "cdkt.toml"
    cfg.write_text('[cdkt]\nmodel = "from-file"\neffort = "low"\nparallel = 2\nuse_fallbacks = false\n')
    s = load_settings(cfg, overrides={"parallel": 8, "model": None}, env={"CDKT_EFFORT": "max", "CDKT_USE_COMPACTION": "no"})
    assert s.model == "from-file"      # file beats default, unset flag doesn't override
    assert s.effort == "max"           # env beats file
    assert s.parallel == 8             # flag beats file
    assert s.use_fallbacks is False and s.use_compaction is False


@pytest.mark.parametrize("key", ["api_key", "ANTHROPIC_API_KEY", "token"])
def test_secrets_in_config_file_are_refused(tmp_path, key):
    cfg = tmp_path / "cdkt.toml"
    cfg.write_text(f'[cdkt]\n{key} = "sk-ant-xxx"\n')
    with pytest.raises(ConfigError, match="credentials don't belong"):
        load_settings(cfg, env={})


def test_unknown_and_invalid_settings(tmp_path):
    cfg = tmp_path / "cdkt.toml"
    cfg.write_text('[cdkt]\nmodle = "typo"\n')
    with pytest.raises(ConfigError, match="unknown setting"):
        load_settings(cfg, env={})
    with pytest.raises(ConfigError):
        load_settings(None, env={"CDKT_PARALLEL": "many"})
    with pytest.raises(ConfigError):
        load_settings(None, overrides={"effort": "extreme"}, env={})


def test_knowledge_paths_relative_to_config_and_included_in_prompt(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "org.md").write_text("Always use our tagging module.")
    cfg = tmp_path / "cdkt.toml"
    cfg.write_text('[cdkt]\nextra_knowledge = ["docs/org.md"]\naws_provider_version = "~> 5.80"\n')
    s = load_settings(cfg, env={})
    system = prompts.system_prompt("converter", s)
    assert "Always use our tagging module." in system and '"~> 5.80"' in system
    # deterministic, so agents of one role share a cache prefix
    assert system == prompts.system_prompt("converter", s)


def test_missing_knowledge_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(None, overrides={"extra_knowledge": [str(tmp_path / "nope.md")]}, env={})


def test_client_reads_key_only_from_named_env(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "MY_KEY", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MY_KEY", "sk-test")
    assert make_client(Settings(api_key_env="MY_KEY")).api_key == "sk-test"
    monkeypatch.delenv("MY_KEY")
    with pytest.raises(ConfigError, match="MY_KEY"):
        make_client(Settings(api_key_env="MY_KEY"))


def test_missing_credentials_fail_fast(monkeypatch, tmp_path):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no `ant auth login` profile
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(ConfigError, match="console.anthropic.com"):
        make_client(Settings())


def test_dotenv_does_not_override_existing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nexport CDKT_TEST_A="from-file"\nCDKT_TEST_B=file\n')
    monkeypatch.setenv("CDKT_TEST_B", "from-shell")
    monkeypatch.delenv("CDKT_TEST_A", raising=False)
    load_dotenv(env)
    import os
    assert os.environ["CDKT_TEST_A"] == "from-file" and os.environ["CDKT_TEST_B"] == "from-shell"
