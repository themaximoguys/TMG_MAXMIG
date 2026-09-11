import pytest

from maxmig.config import MissingEnvVarError, load_env, parse_dotenv


def test_parse_dotenv_strips_full_line_and_inline_comments(tmp_path):
    env_file = tmp_path / ".env.masdev"
    env_file.write_text(
        "# full line comment\n"
        "MASDEV_MANAGE_URL=https://masdev.example.com/maximo/  # trailing comment\n"
        "MASDEV_MANAGE_USERNAME=maxadmin\n"
        "\n"
        "MASDEV_MANAGE_PASSWORD=secret123\n"
    )
    values = parse_dotenv(env_file)
    assert values == {
        "MASDEV_MANAGE_URL": "https://masdev.example.com/maximo/",
        "MASDEV_MANAGE_USERNAME": "maxadmin",
        "MASDEV_MANAGE_PASSWORD": "secret123",
    }


def test_load_env_resolves_prefix_vars_case_insensitively(tmp_path):
    env_file = tmp_path / ".env.masdev"
    env_file.write_text(
        "masdev_manage_url=https://masdev.example.com/maximo/\n"
        "masdev_manage_username=maxadmin\n"
        "masdev_manage_password=secret123\n"
    )
    cfg = load_env("MASDEV", env_file)
    assert cfg.manage_url == "https://masdev.example.com/maximo/"
    assert cfg.username == "maxadmin"
    assert cfg.password == "secret123"
    assert cfg.prefix == "MASDEV"


def test_load_env_raises_named_error_on_missing_var(tmp_path):
    env_file = tmp_path / ".env.masdev"
    env_file.write_text("MASDEV_MANAGE_URL=https://masdev.example.com/maximo/\n")
    with pytest.raises(MissingEnvVarError) as exc_info:
        load_env("MASDEV", env_file)
    assert exc_info.value.var_name == "MASDEV_MANAGE_USERNAME"


def test_load_env_accepts_apikey_with_no_username_or_password(tmp_path):
    env_file = tmp_path / ".env.maxdemo"
    env_file.write_text(
        "MAXDEMO_MANAGE_URL=https://maxdemo.manage.example.com/maximo/\n"
        "MAXDEMO_MANAGE_APIKEY=some-generated-key-value\n"
    )
    cfg = load_env("MAXDEMO", env_file)
    assert cfg.api_key == "some-generated-key-value"
    assert cfg.username is None
    assert cfg.password is None


def test_load_env_falls_back_to_requiring_username_password_when_apikey_blank(tmp_path):
    """A present-but-empty MANAGE_APIKEY line must not short-circuit the
    username/password requirement — blank is treated the same as absent."""
    env_file = tmp_path / ".env.masdev"
    env_file.write_text(
        "MASDEV_MANAGE_URL=https://masdev.example.com/maximo/\n"
        "MASDEV_MANAGE_APIKEY=\n"
    )
    with pytest.raises(MissingEnvVarError) as exc_info:
        load_env("MASDEV", env_file)
    assert exc_info.value.var_name == "MASDEV_MANAGE_USERNAME"
