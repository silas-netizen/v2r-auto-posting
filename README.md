# v2r-auto-posting

**V2R 자동 글 발행 프로그램** — a small, dependency-light toolkit that loads
Markdown posts and publishes them to one or more blog platforms automatically.

It ships with a **console dry-run** target that needs no credentials (great for
local development and CI) and a **WordPress** target that publishes through the
WordPress REST API.

## Features

- Write posts once as Markdown with YAML front matter (`title`, `tags`, `categories`, `status`).
- Publish to multiple targets in one run.
- Dry-run mode previews exactly what would be sent — no secrets required.
- Simple interval scheduler for hands-off "auto" publishing.
- Secrets are injected from environment variables via `${VAR}` placeholders in config.

## Requirements

- Python 3.10+ (developed against 3.12).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime + test/lint tooling
pip install -e .                      # installs the `v2r-autopost` command
```

## Usage

The CLI defaults to a safe console dry-run target when no config is given.

```bash
# List the posts that would be published
v2r-autopost list

# Show configured targets
v2r-autopost targets

# Publish all posts in ./posts as a dry-run (prints, does not send)
v2r-autopost publish --dry-run

# Publish using a real config (e.g. WordPress)
cp config/config.example.yaml config/config.yaml
export WORDPRESS_SITE_URL="https://your-blog.example.com"
export WORDPRESS_USERNAME="you"
export WORDPRESS_APP_PASSWORD="xxxx xxxx xxxx xxxx"
v2r-autopost --config config/config.yaml publish

# Auto mode: run every hour (Ctrl-C to stop)
v2r-autopost --config config/config.yaml run --interval 3600 --iterations 0
```

You can also run it as a module: `python -m v2r_autopost --help`.

## Writing a post

Create a Markdown file in `posts/`:

```markdown
---
title: "My first post"
tags: ["news", "v2r"]
categories: ["announcements"]
status: "publish"
---

The body of the post goes here.
```

## Development

```bash
pip install -r requirements-dev.txt
ruff check .        # lint
pytest -q           # run the test suite
```

## Project layout

```
src/v2r_autopost/       # application package
  cli.py                # Click-based CLI
  engine.py             # ties config + content + publishers together
  content.py            # Markdown/front-matter loading
  config.py             # layered YAML config with env-var expansion
  scheduler.py          # tiny interval scheduler
  publishers/           # dryrun + wordpress targets (registry)
posts/                  # example Markdown posts
config/config.example.yaml
tests/                  # pytest suite
```
