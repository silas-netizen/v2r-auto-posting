"""Command line interface for the V2R auto-posting program."""

from __future__ import annotations

import sys

import click

from . import __version__
from .config import AppConfig
from .content import load_posts
from .engine import publish_posts
from .publishers import available_types
from .scheduler import run_every


@click.group(help="V2R 자동 글 발행 프로그램 - automated blog post publisher.")
@click.version_option(__version__, prog_name="v2r-autopost")
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="Path to a YAML config file. Defaults to a console dry-run target.",
)
@click.pass_context
def main(ctx: click.Context, config_path: str | None) -> None:
    ctx.ensure_object(dict)
    ctx.obj["config"] = AppConfig.load(config_path)


@main.command("list", help="List posts that would be published.")
@click.option("--posts-dir", default=None, help="Override the posts directory.")
@click.pass_context
def list_posts(ctx: click.Context, posts_dir: str | None) -> None:
    config: AppConfig = ctx.obj["config"]
    directory = posts_dir or config.posts_dir
    posts = load_posts(directory)
    if not posts:
        click.echo(f"No posts found in {directory!r}.")
        return
    click.echo(f"Found {len(posts)} post(s) in {directory!r}:")
    for post in posts:
        tags = f" [{', '.join(post.tags)}]" if post.tags else ""
        click.echo(f"  • {post.title}{tags} — {post.summary()}")


@main.command("targets", help="Show configured publish targets.")
@click.pass_context
def show_targets(ctx: click.Context) -> None:
    config: AppConfig = ctx.obj["config"]
    click.echo(f"Available target types: {', '.join(available_types())}")
    if not config.targets:
        click.echo("No targets configured (default: console dry-run).")
        return
    click.echo("Configured targets:")
    for target in config.targets:
        state = "enabled" if target.enabled else "disabled"
        click.echo(f"  • {target.name} ({target.type}) — {state}")


@main.command("publish", help="Publish all posts to the configured targets.")
@click.option("--posts-dir", default=None, help="Override the posts directory.")
@click.option("--dry-run", is_flag=True, help="Force console dry-run for every post.")
@click.pass_context
def publish(ctx: click.Context, posts_dir: str | None, dry_run: bool) -> None:
    config: AppConfig = ctx.obj["config"]
    if posts_dir:
        config.posts_dir = posts_dir

    results = publish_posts(config, force_dryrun=dry_run)
    if not results:
        click.echo("Nothing to publish.")
        return

    for result in results:
        click.echo(result.format_line())

    failures = [r for r in results if not r.success]
    click.echo(f"\nDone: {len(results) - len(failures)} succeeded, {len(failures)} failed.")
    if failures:
        sys.exit(1)


@main.command("run", help="Continuously publish on an interval (auto mode).")
@click.option("--interval", default=3600.0, type=float, help="Seconds between runs.")
@click.option("--iterations", default=1, type=int, help="How many times to run (0 = forever).")
@click.option("--dry-run", is_flag=True, help="Force console dry-run for every post.")
@click.pass_context
def run(ctx: click.Context, interval: float, iterations: int, dry_run: bool) -> None:
    config: AppConfig = ctx.obj["config"]

    def job() -> None:
        click.echo("── publishing cycle ──")
        for result in publish_posts(config, force_dryrun=dry_run):
            click.echo(result.format_line())

    max_iterations = None if iterations <= 0 else iterations
    count = run_every(interval, job, max_iterations=max_iterations)
    click.echo(f"Completed {count} publishing cycle(s).")


if __name__ == "__main__":  # pragma: no cover
    main()
