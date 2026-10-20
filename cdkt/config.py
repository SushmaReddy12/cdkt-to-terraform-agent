"""Settings and credentials.

Nothing about a run is fixed in code. Each setting resolves, lowest to highest
precedence, from:

    built-in default  <  cdkt.toml [cdkt] table  <  CDKT_* environment variable  <  CLI flag

Credentials are never read from cdkt.toml (it is meant to be committed). The
API key comes from the environment variable named by ``api_key_env`` (default
ANTHROPIC_API_KEY), which may be loaded from a git-ignored ``.env`` file. With
no key set, the SDK's own resolution applies (ANTHROPIC_AUTH_TOKEN, or a
profile from ``ant auth login``).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

import anthropic

CONFIG_FILENAME = "cdkt.toml"
SECRET_KEYS = {"api_key", "anthropic_api_key", "auth_token", "key", "token", "secret"}


class ConfigError(ValueError):
    pass


@dataclass
class Settings:
    # model and request shape
    model: str = "claude-opus-5"
    effort: str = "high"
    max_tokens: int = 64000
    use_fallbacks: bool = True  # fallbacks="default" + beta header; disable where unsupported
    use_compaction: bool = True  # server-side compaction beta for long conversions
    # orchestration
    parallel: int = 4
    max_turns: int = 80
    # output
    run_terraform: bool = True
    emit_imports: bool = True
    aws_provider_version: str = "~> 6.0"
    extra_knowledge: list[str] = field(default_factory=list)  # extra reference .md files for the agents
    # connection (never the secret itself)
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: str | None = None
    timeout_seconds: float = 900.0
    max_retries: int = 4


EFFORTS = {"low", "medium", "high", "xhigh", "max"}
_FIELDS = {f.name: f for f in fields(Settings)}


def _coerce(name: str, value, source: str):
    default = getattr(Settings(), name)
    try:
        if isinstance(default, bool):
            if isinstance(value, bool):
                return value
            if str(value).strip().lower() in {"1", "true", "yes", "on"}:
                return True
            if str(value).strip().lower() in {"0", "false", "no", "off"}:
                return False
            raise ValueError(value)
        if isinstance(default, int):
            return int(value)
        if isinstance(default, float):
            return float(value)
        if isinstance(default, list):
            if isinstance(value, list):
                return [str(v) for v in value]
            return [v.strip() for v in str(value).split(",") if v.strip()]
        return None if value in ("", None) else str(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{source}: invalid value {value!r} for {name}") from None


def load_dotenv(path: Path) -> None:
    """Minimal .env loader; never overrides variables already set in the environment."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_settings(config_path: Path | None = None, overrides: dict | None = None, env: dict | None = None) -> Settings:
    env = os.environ if env is None else env
    values: dict = {}

    path = config_path or (Path.cwd() / CONFIG_FILENAME)
    if config_path is not None and not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    if path.is_file():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        table = data.get("cdkt", data)
        secrets = SECRET_KEYS & {k.lower() for k in table}
        if secrets:
            raise ConfigError(
                f"{path}: remove {sorted(secrets)} - credentials don't belong in config files. "
                "Set the key in the environment (or a git-ignored .env) and, if needed, "
                "name the variable with api_key_env."
            )
        for key, value in table.items():
            if key not in _FIELDS:
                raise ConfigError(f"{path}: unknown setting '{key}' (known: {', '.join(sorted(_FIELDS))})")
            values[key] = _coerce(key, value, str(path))
        # relative knowledge paths are relative to the config file
        values["extra_knowledge"] = [str((path.parent / p).resolve()) for p in values.get("extra_knowledge", [])]

    for name in _FIELDS:
        env_key = f"CDKT_{name.upper()}"
        if env_key in env:
            values[name] = _coerce(name, env[env_key], f"${env_key}")

    for name, value in (overrides or {}).items():
        if value is not None:
            values[name] = _coerce(name, value, f"--{name.replace('_', '-')}")

    settings = Settings(**values)
    if settings.effort not in EFFORTS:
        raise ConfigError(f"effort must be one of {sorted(EFFORTS)}, got {settings.effort!r}")
    if settings.parallel < 1 or settings.max_turns < 1 or settings.max_tokens < 1024:
        raise ConfigError("parallel and max_turns must be >= 1 and max_tokens >= 1024")
    for kpath in settings.extra_knowledge:
        if not Path(kpath).is_file():
            raise ConfigError(f"extra_knowledge file not found: {kpath}")
    return settings


def make_client(settings: Settings) -> anthropic.Anthropic:
    """Build the API client from settings; the key is only ever read from the environment."""
    kwargs: dict = {"timeout": settings.timeout_seconds, "max_retries": settings.max_retries}
    if settings.base_url:
        kwargs["base_url"] = settings.base_url
    key = os.environ.get(settings.api_key_env)
    if key:
        kwargs["api_key"] = key
    elif settings.api_key_env != "ANTHROPIC_API_KEY":
        raise ConfigError(f"api_key_env is {settings.api_key_env} but that environment variable is not set")
    # otherwise let the SDK resolve ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / an `ant auth login` profile
    client = anthropic.Anthropic(**kwargs)
    # Fail fast: without this the first request raises a bare TypeError mid-run.
    if not any(getattr(client, attr, None) for attr in ("api_key", "auth_token", "credentials")):
        raise ConfigError(
            f"no Anthropic credentials found. Create an API key at https://console.anthropic.com/settings/keys "
            f"and export {settings.api_key_env}=... (or put it in a git-ignored .env), or run `ant auth login`."
        )
    return client
