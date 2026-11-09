"""Configuration UI contract; not a real AstrBot startup test."""

import json
import tomllib
from pathlib import Path

import yaml

from native.config import DEFAULTS, Settings

ROOT = Path(__file__).resolve().parents[1]


def test_schema_defaults_match_runtime():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert set(schema) == set(DEFAULTS)
    loaded = {}
    for name, values in DEFAULTS.items():
        assert schema[name]["type"] == "object"
        assert set(schema[name]["items"]) == set(values)
        loaded[name] = {key: entry["default"] for key, entry in schema[name]["items"].items()}
        assert loaded[name] == values
    assert Settings(loaded).data == DEFAULTS


def test_provider_selector_and_dictionary_fields():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    for feature in ("search", "image"):
        fields = schema[feature]["items"]
        assert fields["provider_id"]["_special"] == "select_provider"
        assert fields["mode"]["options"] == ["current", "provider", "manual"]
        for key in ("headers", "extra_body", "paths"):
            assert fields[key]["type"] == "dict"
            assert isinstance(fields[key]["default"], dict)
        assert fields["keys"]["secret"] is True


def test_metadata_has_supported_version_and_authorized_repository():
    metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["name"] == "astrbot_plugin_openai_native"
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["version"].removeprefix("v") == project["project"]["version"]
    assert metadata["astrbot_version"] == ">=4.29.4"
    assert metadata["repo"] == "https://github.com/timeance/astrbot_plugin_openai_native"


def test_native_page_shows_only_essential_feature_fields():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    for feature in ("search", "image"):
        fields = schema[feature]["items"]
        visible = {key for key, entry in fields.items() if not entry.get("invisible")}
        expected = {"enabled", "mode", "provider_id", "model"}
        if feature == "image":
            expected.add("host_model")
            assert fields["host_model"]["condition"] == {"protocol": "responses"}
        assert visible == expected
        assert fields["provider_id"]["condition"] == {"mode": "provider"}
        assert fields["mode"]["labels"] == ["当前会话供应商", "指定供应商", "手动配置"]
    for section in ("gif", "limits", "storage", "network"):
        assert schema[section]["invisible"] is True
        assert all(field["invisible"] for field in schema[section]["items"].values())


def test_hidden_fields_keep_types_and_existing_values():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    old = {
        "search": {"keys": ["env:KEY"], "headers": {"X-Custom": "value"}},
        "limits": {"user_image": -1},
        "gif": {"enabled": True},
    }
    settings = Settings(old)
    assert settings.feature("search")["keys"] == ["env:KEY"]
    assert settings.feature("search")["headers"] == {"X-Custom": "value"}
    assert settings.limits["user_image"] == -1
    assert settings.gif["enabled"] is True
    for section, defaults in DEFAULTS.items():
        for key, default in defaults.items():
            expected_type = {bool: "bool", int: "int", str: "string", list: "list", dict: "dict"}[
                type(default)
            ]
            assert schema[section]["items"][key]["type"] == expected_type
