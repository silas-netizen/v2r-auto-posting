"""WordPress REST API publisher.

Uses the WordPress REST API (``/wp-json/wp/v2/posts``) with an application
password. Credentials are supplied via configuration, which typically pulls
them from environment variables so nothing sensitive is committed.
"""

from __future__ import annotations

import base64

import requests

from ..models import Post, PublishResult
from .base import Publisher

_TIMEOUT = 30


class WordPressPublisher(Publisher):
    def __init__(
        self,
        name: str = "wordpress",
        *,
        site_url: str = "",
        username: str = "",
        app_password: str = "",
        **_options: object,
    ) -> None:
        super().__init__(name)
        self.site_url = site_url.rstrip("/")
        self.username = username
        self.app_password = app_password

    def _auth_header(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.username}:{self.app_password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def _validate(self) -> str | None:
        missing = [
            key
            for key, value in {
                "site_url": self.site_url,
                "username": self.username,
                "app_password": self.app_password,
            }.items()
            if not value
        ]
        if missing:
            return f"missing configuration: {', '.join(missing)}"
        return None

    def publish(self, post: Post) -> PublishResult:
        error = self._validate()
        if error:
            return PublishResult(self.name, post.title, success=False, message=error)

        endpoint = f"{self.site_url}/wp-json/wp/v2/posts"
        payload = {
            "title": post.title,
            "content": post.body,
            "status": post.status,
        }
        if post.tags:
            payload["tags"] = post.tags
        if post.categories:
            payload["categories"] = post.categories

        try:
            response = requests.post(
                endpoint,
                json=payload,
                headers=self._auth_header(),
                timeout=_TIMEOUT,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            return PublishResult(self.name, post.title, success=False, message=str(exc))

        data = response.json() if response.content else {}
        return PublishResult(
            self.name,
            post.title,
            success=True,
            url=data.get("link"),
            message=f"id={data.get('id')}",
        )
