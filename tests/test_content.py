from v2r_auto.content import parse_article


def test_parses_question_article_and_reply_hierarchy() -> None:
    article = parse_article(
        "갓비움 후기",
        """제목 :
갓비움 후기 좋던데 진짜인가요?

본문 :
본문 첫 줄
본문 둘째 줄

댓글1:
첫 댓글
대댓글1:
첫 답글
댓글2:
두 번째 댓글
대댓글2:
두 번째 답글
대대댓글2:
두 번째 답글의 답글
대대대댓글2:
세 번째 깊이 답글
댓글3:
세 번째 댓글
대댓글3:
세 번째 답글""",
    )

    assert article.title == "갓비움 후기 좋던데 진짜인가요?"
    assert article.body == "본문 첫 줄\n본문 둘째 줄"
    assert article.tag == "갓비움후기"
    assert [node.label for node in article.comments] == ["댓글1", "댓글2", "댓글3"]
    assert article.comments[0].children[0].label == "대댓글1"
    assert article.comments[1].children[0].children[0].children[0].label == "대대대댓글2"
