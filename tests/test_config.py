from pathlib import Path

import pytest
import yaml

from nightshift.config import ConfigError, parse_config

ROOT = Path(__file__).parent.parent


def test_shipped_example_config_is_valid():
    cfg = parse_config(yaml.safe_load((ROOT / "config.example.yaml").read_text()), ROOT)
    assert cfg.repos and cfg.briefs_dir.parent == cfg.state_dir
    assert cfg.aetherbus.topic.startswith(("aetherbus.", "dev.aetherbus."))


@pytest.mark.parametrize("data,msg", [
    ({}, "at least one"),
    ({"repos": ["not-a-repo"]}, "owner/repo"),
    ({"repos": [{"name": "a/b", "watch": ["ci", "tweets"]}]}, "unknown kinds"),
    ({"repos": [{"name": "a/b", "lessons": [{"pattern": "x"}]}]}, "pattern' and 'note'"),
])
def test_bad_config_is_rejected_with_a_useful_message(data, msg, tmp_path):
    with pytest.raises(ConfigError, match=msg):
        parse_config(data, tmp_path)


def test_relative_paths_resolve_against_config_dir(tmp_path):
    cfg = parse_config({"repos": ["a/b"], "state_dir": "st"}, tmp_path)
    assert cfg.state_dir == tmp_path / "st"
