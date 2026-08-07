from v2r_autopost.config import AppConfig


def test_default_config_has_dryrun_target():
    config = AppConfig.load(None)
    assert config.posts_dir == "posts"
    assert len(config.enabled_targets()) == 1
    assert config.enabled_targets()[0].type == "dryrun"


def test_env_expansion(monkeypatch, tmp_path):
    monkeypatch.setenv("WP_PW", "supersecret")
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        """
posts_dir: my_posts
targets:
  - name: wp
    type: wordpress
    enabled: true
    site_url: https://example.com
    app_password: ${WP_PW}
  - name: off
    type: dryrun
    enabled: false
""",
        encoding="utf-8",
    )
    config = AppConfig.load(str(cfg))
    assert config.posts_dir == "my_posts"
    assert len(config.targets) == 2
    assert len(config.enabled_targets()) == 1
    wp = config.enabled_targets()[0]
    assert wp.options["app_password"] == "supersecret"


def test_missing_config_raises(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        AppConfig.load(str(tmp_path / "nope.yaml"))
