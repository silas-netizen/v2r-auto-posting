from v2r_autopost.content import load_post, load_posts


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_load_post_parses_front_matter(tmp_path):
    path = _write(
        tmp_path / "hello.md",
        """---
title: "Hello World"
tags: [a, b]
categories: news
status: draft
---
Body text here.
""",
    )
    post = load_post(path)
    assert post.title == "Hello World"
    assert post.tags == ["a", "b"]
    assert post.categories == ["news"]
    assert post.status == "draft"
    assert post.body == "Body text here."
    assert post.source_path == path


def test_load_post_title_falls_back_to_filename(tmp_path):
    path = _write(tmp_path / "my-cool_post.md", "Just body, no front matter.")
    post = load_post(path)
    assert post.title == "My Cool Post"
    assert post.tags == []


def test_load_posts_sorted_and_filtered(tmp_path):
    _write(tmp_path / "b.md", "b")
    _write(tmp_path / "a.md", "a")
    _write(tmp_path / "ignore.txt", "nope")
    posts = load_posts(str(tmp_path))
    assert [p.body for p in posts] == ["a", "b"]


def test_load_posts_missing_dir_raises(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        load_posts(str(tmp_path / "does-not-exist"))
