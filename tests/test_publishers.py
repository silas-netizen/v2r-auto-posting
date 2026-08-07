import io

import responses

from v2r_autopost.config import TargetConfig
from v2r_autopost.models import Post
from v2r_autopost.publishers import available_types, build_publisher
from v2r_autopost.publishers.dryrun import DryRunPublisher
from v2r_autopost.publishers.wordpress import WordPressPublisher


def _post():
    return Post(title="T", body="Body", tags=["x"], categories=["c"], status="publish")


def test_registry_lists_types():
    types = available_types()
    assert "dryrun" in types
    assert "wordpress" in types


def test_build_publisher_unknown_type_raises():
    import pytest

    with pytest.raises(ValueError):
        build_publisher(TargetConfig(name="n", type="nope"))


def test_dryrun_publishes_to_stream():
    stream = io.StringIO()
    publisher = DryRunPublisher(name="console", stream=stream)
    result = publisher.publish(_post())
    assert result.success is True
    assert "would publish" in stream.getvalue()
    assert "T" in stream.getvalue()


def test_wordpress_missing_config_fails_gracefully():
    publisher = WordPressPublisher(name="wp")
    result = publisher.publish(_post())
    assert result.success is False
    assert "missing configuration" in result.message


@responses.activate
def test_wordpress_publishes_via_rest_api():
    responses.add(
        responses.POST,
        "https://blog.example.com/wp-json/wp/v2/posts",
        json={"id": 42, "link": "https://blog.example.com/?p=42"},
        status=201,
    )
    publisher = WordPressPublisher(
        name="wp",
        site_url="https://blog.example.com/",
        username="admin",
        app_password="secret",
    )
    result = publisher.publish(_post())
    assert result.success is True
    assert result.url == "https://blog.example.com/?p=42"
    assert "id=42" in result.message

    sent = responses.calls[0].request
    assert sent.headers["Authorization"].startswith("Basic ")


@responses.activate
def test_wordpress_http_error_is_reported():
    responses.add(
        responses.POST,
        "https://blog.example.com/wp-json/wp/v2/posts",
        status=401,
    )
    publisher = WordPressPublisher(
        name="wp",
        site_url="https://blog.example.com",
        username="admin",
        app_password="bad",
    )
    result = publisher.publish(_post())
    assert result.success is False
