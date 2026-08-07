from __future__ import annotations

import re
from dataclasses import dataclass, field


class ContentFormatError(ValueError):
    pass


@dataclass(slots=True)
class CommentNode:
    label: str
    text: str
    depth: int
    index: int
    children: list["CommentNode"] = field(default_factory=list)


@dataclass(slots=True)
class ParsedArticle:
    title: str
    body: str
    keyword: str
    tag: str
    comments: list[CommentNode]


SECTION_PATTERN = re.compile(r"^\s*(제목|본문)\s*:\s*(.*)$")
COMMENT_PATTERN = re.compile(r"^\s*(대*)댓글\s*(\d+)\s*:\s*(.*)$")


def _join(lines: list[str]) -> str:
    return "\n".join(lines).strip()


def parse_article(keyword: str, source: str) -> ParsedArticle:
    """Parse the user's Title/Body/Comment hierarchy convention from one cell."""
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    sections: dict[str, list[str]] = {"제목": [], "본문": []}
    current: str | CommentNode | None = None
    roots: list[CommentNode] = []
    stack: list[CommentNode] = []

    for raw_line in lines:
        section_match = SECTION_PATTERN.match(raw_line)
        if section_match:
            current = section_match.group(1)
            initial = section_match.group(2).strip()
            if initial:
                sections[current].append(initial)
            continue

        comment_match = COMMENT_PATTERN.match(raw_line)
        if comment_match:
            depth = len(comment_match.group(1))
            index = int(comment_match.group(2))
            node = CommentNode(
                label=raw_line.split(":", 1)[0].strip(),
                text=comment_match.group(3).strip(),
                depth=depth,
                index=index,
            )
            if depth == 0:
                roots.append(node)
                stack = [node]
            else:
                if len(stack) < depth:
                    raise ContentFormatError(
                        f"'{node.label}'의 바로 위 부모 댓글이 없습니다"
                    )
                parent = stack[depth - 1]
                parent.children.append(node)
                stack = stack[:depth]
                stack.append(node)
            current = node
            continue

        if isinstance(current, CommentNode):
            current.text = _join([current.text, raw_line]) if current.text else raw_line.strip()
        elif isinstance(current, str):
            sections[current].append(raw_line)

    title = _join(sections["제목"])
    body = _join(sections["본문"])
    if not title:
        raise ContentFormatError("'제목 :' 뒤에 제목이 없습니다")
    if not body:
        raise ContentFormatError("'본문 :' 뒤에 본문이 없습니다")

    def validate(nodes: list[CommentNode]) -> None:
        for node in nodes:
            if not node.text:
                raise ContentFormatError(f"'{node.label}' 뒤에 댓글 내용이 없습니다")
            validate(node.children)

    validate(roots)
    tag = re.sub(r"\s+", "", keyword)
    if not tag:
        raise ContentFormatError("키워드가 비어 있어 태그를 만들 수 없습니다")
    return ParsedArticle(title=title, body=body, keyword=keyword, tag=tag, comments=roots)
