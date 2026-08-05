"""Pure graph-reference extraction tests; no local store is required."""

from __future__ import annotations

from src.ingest import graph


REG = graph.LawRegistry(
    {
        "001556": ("관세법", "법률"),
        "002421": ("관세법 시행령", "시행령"),
        "006392": ("관세법 시행규칙", "시행규칙"),
        "001783": ("약사법", "법률"),
        "001805": ("식품위생법", "법률"),
    }
)


def test_resolve_qualifier() -> None:
    assert graph._resolve_qualifier(None, "002421", REG) == "002421"
    assert graph._resolve_qualifier("법", "002421", REG) == "001556"
    assert graph._resolve_qualifier("영", "006392", REG) == "002421"
    assert graph._resolve_qualifier("규칙", "001556", REG) == "006392"
    assert graph._resolve_qualifier("관세법시행령", "001556", REG) == "002421"
    assert graph._resolve_qualifier("이 법", "002421", REG) == "002421"
    assert graph._resolve_qualifier("외국환거래법", "002421", REG) is None
    assert graph._resolve_qualifier("영", "001783", REG) is None
    assert graph._resolve_qualifier("법", "001783", REG) == "001783"


def test_extract_refs_resolves_and_filters() -> None:
    content = (
        "[관세법 시행령 제233조(구비조건의 확인)]\n"
        "제233조(구비조건의 확인) 법 제226조에 따른 허가ㆍ승인의 증명은 "
        "제234조를 준용하며 「외국환거래법」 제5조는 적용하지 아니한다."
    )
    refs = graph.extract_refs(content, "002421", 23300, REG)
    assert ("001556", 22600) in refs
    assert ("002421", 23400) in refs
    assert ("002421", 23300) not in refs
    assert all(article_no != 500 for _, article_no in refs)


def test_extract_refs_heading_not_selfcited() -> None:
    content = "[관세법 제226조(허가ㆍ승인 등의 증명 및 확인)]\n제226조(허가) 제245조를 준용한다."
    refs = graph.extract_refs(content, "001556", 22600, REG)
    assert ("001556", 22600) not in refs
    assert ("001556", 24500) in refs


def test_extract_refs_cross_law_by_name() -> None:
    content = (
        "[수입식품안전관리 특별법 제20조(수입신고)]\n"
        "제20조(수입신고) 식품위생법 제7조에 따른 기준에 적합하여야 하며, "
        "약사법 제42조는 별도로 적용한다."
    )
    refs = graph.extract_refs(content, "012247", 2000, REG)
    assert ("001805", 700) in refs
    assert ("001783", 4200) in refs
