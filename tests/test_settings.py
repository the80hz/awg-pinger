import pytest
from pydantic import ValidationError

from shared.settings import ClientSettings, ServerConfig


def test_settings_requires_unique_server_ids() -> None:
    with pytest.raises(ValidationError):
        ClientSettings(
            client_secret="test-secret-value-that-is-long-enough",
            api_base_url="http://api-side:8000",
            servers=[
                ServerConfig(id="vps-1", config="one.conf", ping_host="10.8.0.1"),
                ServerConfig(id="vps-1", config="two.conf", ping_host="10.8.0.1"),
            ],
        )


def test_config_must_stay_inside_data_dir() -> None:
    with pytest.raises(ValidationError):
        ServerConfig(id="vps-1", config="../secret.conf", ping_host="10.8.0.1")


def test_config_must_be_conf_file() -> None:
    with pytest.raises(ValidationError):
        ServerConfig(id="vps-1", config="vps-1.txt", ping_host="10.8.0.1")


def test_api_base_url_requires_http() -> None:
    with pytest.raises(ValidationError):
        ClientSettings(
            client_secret="test-secret-value-that-is-long-enough",
            api_base_url="api-side:8000",
        )


def test_client_secret_is_required() -> None:
    with pytest.raises(ValidationError):
        ClientSettings(api_base_url="http://api-side:8000")
