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


def test_parses_markdown_comment_headings_and_ignores_separators() -> None:
    article = parse_article(
        "보건소 산전검사",
        """제목:
제목
본문:
본문 내용

# 보건소 산전검사 관련 댓글 세트

## 댓글1:
댓글 하나
### 대댓글1:
답글 하나
---
## 댓글2:
댓글 둘
### 대댓글2:
답글 둘
#### 대대댓글2:
깊은 답글
##### 대대대댓글2:
더 깊은 답글
---
## 댓글3:
댓글 셋
### 대댓글3:
답글 셋
## 댓글4:
댓글 넷
### 대댓글4:
답글 넷
## 댓글5:
댓글 다섯
### 대댓글5:
답글 다섯""",
    )

    assert article.body == "본문 내용"
    assert [node.label for node in article.comments] == [
        "댓글1",
        "댓글2",
        "댓글3",
        "댓글4",
        "댓글5",
    ]
    assert article.comments[0].children[0].label == "대댓글1"
    assert (
        article.comments[1].children[0].children[0].children[0].label
        == "대대대댓글2"
    )
    assert "---" not in article.comments[0].children[0].text


def test_parses_bold_sections_and_unnumbered_comment_labels() -> None:
    article = parse_article(
        "키워드",
        """### **제목:**
제목 내용

## **본문:**
본문 내용

**댓글:**
첫 댓글
**대댓글:**
첫 답글

## **댓글:**
둘째 댓글
### **대댓글:**
둘째 답글
#### **대대댓글:**
깊은 답글
##### **대대대댓글:**
더 깊은 답글""",
    )

    assert article.title == "제목 내용"
    assert article.body == "본문 내용"
    assert [node.label for node in article.comments] == ["댓글1", "댓글2"]
    assert article.comments[0].children[0].label == "대댓글1"
    assert article.comments[1].children[0].label == "대댓글2"
    assert article.comments[1].children[0].children[0].label == "대대댓글2"
    assert (
        article.comments[1].children[0].children[0].children[0].label
        == "대대대댓글2"
    )
