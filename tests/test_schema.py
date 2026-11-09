"""Configuration UI contract; not a real AstrBot startup test."""

import json
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
    assert metadata["version"] == "v0.1.0"
    assert metadata["astrbot_version"] == ">=4.29.4"
    assert metadata["repo"] == "https://github.com/timeance/astrbot_plugin_openai_native"
