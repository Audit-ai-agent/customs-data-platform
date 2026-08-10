"""Tracked, non-secret source allowlist for the 65-law Evidence history profile.

The list is a reviewed candidate registry only.  It is never a source of law
text: every build obtains the effective official bytes with ``src.lawgo`` and
stores those bytes exclusively below the ignored local profile directory.
"""

from __future__ import annotations

from datetime import date

LAW_HISTORY_PROFILE = "functional-uat-law-history"
LAW_HISTORY_BASELINE_AS_OF = date(2026, 8, 5)

# These inputs are a fixed *contract snapshot* at the law-history baseline,
# not a claim that the files themselves carry a 2026-08-05 collection receipt.
# The profile verifies the exact tracked bytes and the resulting projections
# before it labels HSK/requirements with this agreed UAT reference date.
LAW_HISTORY_FROZEN_INPUTS_VERSION = "tracked-input-contract/v1"
LAW_HISTORY_HSK_INPUTS: tuple[tuple[str, str, int], ...] = (
    (
        "data/관세청_HS부호 단위별 품목명_20260101.xlsx",
        "9a5a942b5e44a817bf3fb9ef6145fa23ef110a9692e4f3e0b84eb33672409ba2",
        800_868,
    ),
    (
        "data/관세청_HS부호_20260101.xlsx",
        "020661f75ac044be0eec049013b074be77ca8fb6d13f8283666f1f3e594054e5",
        1_419_595,
    ),
)
LAW_HISTORY_HSK_NODE_COUNT = 17_072
LAW_HISTORY_HSK_LEAF_COUNT = 11_327
LAW_HISTORY_CCCT_INPUT = (
    "data/ccct_progress.jsonl",
    "39413241c3829e7c86b8ef0fd2135876bfd5bf8c67afd7e690fc13d7f824c005",
    5_257_148,
)
LAW_HISTORY_CCCT_SNAPSHOT_ID = (
    "ccct_212f4c459765d5a79f5a42e2eef77bcbe43bd7adb5c4d526a9ecc3588622e107"
)
LAW_HISTORY_CCCT_QUERY_COUNT = 22_654
LAW_HISTORY_CCCT_RAW_ROW_COUNT = 14_726
LAW_HISTORY_CCCT_REQUIREMENT_COUNT = 14_726
LAW_HISTORY_CCCT_DOCUMENT_COUNT = 63

LAW_HISTORY_LAWS: tuple[tuple[str, str], ...] = (
    ("001556", "관세법"),
    ("002421", "관세법 시행령"),
    ("006392", "관세법 시행규칙"),
    ("010097", "자유무역협정의 이행을 위한 관세법의 특례에 관한 법률"),
    ("010175", "자유무역협정의 이행을 위한 관세법의 특례에 관한 법률 시행령"),
    ("010172", "자유무역협정의 이행을 위한 관세법의 특례에 관한 법률 시행규칙"),
    ("000592", "수출용 원재료에 대한 관세 등 환급에 관한 특례법"),
    ("004043", "수출용 원재료에 대한 관세 등 환급에 관한 특례법 시행령"),
    ("007591", "수출용 원재료에 대한 관세 등 환급에 관한 특례법 시행규칙"),
    ("001467", "대외무역법"),
    ("003313", "대외무역법 시행령"),
    ("001513", "식물방역법"),
    ("012247", "수입식품안전관리 특별법"),
    ("001783", "약사법"),
    ("002015", "화장품법"),
    ("001459", "전기용품 및 생활용품 안전관리법"),
    ("013572", "첨단재생의료 및 첨단바이오의약품 안전 및 지원에 관한 법률"),
    ("009514", "의료기기법"),
    ("009609", "인체조직안전 및 관리 등에 관한 법률"),
    ("002025", "마약류 관리에 관한 법률"),
    ("001805", "식품위생법"),
    ("001507", "축산물 위생관리법"),
    ("009353", "건강기능식품에 관한 법률"),
    ("000419", "친환경농어업 육성 및 유기식품 등의 관리ㆍ지원에 관한 법률"),
    ("000474", "양곡관리법"),
    ("000165", "먹는물관리법"),
    ("012863", "위생용품 관리법"),
    ("001504", "가축전염병 예방법"),
    ("000422", "종자산업법"),
    ("001509", "축산법"),
    ("001499", "사료관리법"),
    ("001514", "농약관리법"),
    ("000416", "비료관리법"),
    ("010602", "수산생물질병 관리법"),
    ("001978", "농수산물 품질관리법"),
    ("000162", "화학물질관리법"),
    ("011857", "화학물질의 등록 및 평가 등에 관한 법률"),
    ("013098", "생활화학제품 및 살생물제의 안전관리에 관한 법률"),
    ("010383", "잔류성오염물질 관리법"),
    ("000324", "오존층 보호 등을 위한 특정물질의 관리에 관한 법률"),
    ("000154", "폐기물의 국가 간 이동 및 그 처리에 관한 법률"),
    ("001771", "폐기물관리법"),
    ("000155", "자원의 절약과 재활용촉진에 관한 법률"),
    (
        "000317",
        "화학무기ㆍ생물무기의 금지와 특정화학물질ㆍ생물작용제 등의 제조ㆍ수출입 규제 등에 관한 법률",
    ),
    ("011435", "원자력안전법"),
    ("011433", "생활주변방사선 안전관리법"),
    ("001732", "전파법"),
    ("001457", "계량에 관한 법률"),
    ("001766", "산업안전보건법"),
    ("000239", "건설기계관리법"),
    ("001747", "자동차관리법"),
    ("000167", "소음ㆍ진동관리법"),
    ("001867", "에너지이용 합리화법"),
    ("001850", "고압가스 안전관리법"),
    ("012070", "어린이제품 안전 특별법"),
    ("009683", "야생생물 보호 및 관리에 관한 법률"),
    ("011540", "생물다양성 보전 및 이용에 관한 법률"),
    ("012788", "유전자원의 접근ㆍ이용 및 이익 공유에 관한 법률"),
    ("010499", "농업생명자원의 보존ㆍ관리 및 이용에 관한 법률"),
    ("011632", "해양수산생명자원의 확보ㆍ관리 및 이용 등에 관한 법률"),
    ("011620", "목재의 지속가능한 이용에 관한 법률"),
    ("001860", "석유 및 석유대체연료 사업법"),
    ("010107", "방위사업법"),
    ("001642", "총포ㆍ도검ㆍ화약류 등의 안전관리에 관한 법률"),
    ("001607", "문화유산의 보존 및 활용에 관한 법률"),
)

LAW_HISTORY_LAW_IDS: tuple[str, ...] = tuple(law_id for law_id, _name in LAW_HISTORY_LAWS)
LAW_HISTORY_TARGET_LAWS: tuple[dict[str, str], ...] = tuple(
    {"law_id": law_id, "name": law_name} for law_id, law_name in LAW_HISTORY_LAWS
)

if len(LAW_HISTORY_LAWS) != 65 or len(set(LAW_HISTORY_LAW_IDS)) != len(LAW_HISTORY_LAWS):
    raise RuntimeError("law-history source registry must contain 65 unique law IDs")

__all__ = [
    "LAW_HISTORY_BASELINE_AS_OF",
    "LAW_HISTORY_CCCT_DOCUMENT_COUNT",
    "LAW_HISTORY_CCCT_INPUT",
    "LAW_HISTORY_CCCT_QUERY_COUNT",
    "LAW_HISTORY_CCCT_RAW_ROW_COUNT",
    "LAW_HISTORY_CCCT_REQUIREMENT_COUNT",
    "LAW_HISTORY_CCCT_SNAPSHOT_ID",
    "LAW_HISTORY_FROZEN_INPUTS_VERSION",
    "LAW_HISTORY_HSK_INPUTS",
    "LAW_HISTORY_HSK_LEAF_COUNT",
    "LAW_HISTORY_HSK_NODE_COUNT",
    "LAW_HISTORY_LAW_IDS",
    "LAW_HISTORY_LAWS",
    "LAW_HISTORY_PROFILE",
    "LAW_HISTORY_TARGET_LAWS",
]
