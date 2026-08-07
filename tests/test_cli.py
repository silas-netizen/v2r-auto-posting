from click.testing import CliRunner

from v2r_autopost.cli import main


def _make_posts(tmp_path):
    posts = tmp_path / "posts"
    posts.mkdir()
    (posts / "a.md").write_text(
        '---\ntitle: "Post A"\ntags: [t1]\n---\nHello A body.\n', encoding="utf-8"
    )
    return str(posts)


def test_list_command(tmp_path):
    posts_dir = _make_posts(tmp_path)
    result = CliRunner().invoke(main, ["list", "--posts-dir", posts_dir])
    assert result.exit_code == 0, result.output
    assert "Post A" in result.output
    assert "Found 1 post" in result.output


def test_targets_command_default():
    result = CliRunner().invoke(main, ["targets"])
    assert result.exit_code == 0, result.output
    assert "dryrun" in result.output


def test_publish_dry_run(tmp_path):
    posts_dir = _make_posts(tmp_path)
    result = CliRunner().invoke(main, ["publish", "--posts-dir", posts_dir, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert "1 succeeded, 0 failed" in result.output


def test_run_command_single_iteration(tmp_path):
    posts_dir = _make_posts(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"posts_dir: {posts_dir}\ntargets:\n  - name: console\n    type: dryrun\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        main,
        ["--config", str(cfg), "run", "--interval", "1", "--iterations", "1"],
    )
    assert result.exit_code == 0, result.output
    assert "Completed 1 publishing cycle" in result.output
    assert "Post A" in result.output
