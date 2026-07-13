"""CCCT P0: raw row 무손실성·canonical ID·스냅샷 안정성."""
from __future__ import annotations

from src.ingest.ccct import build_dataset, canonical_requirement_id


def _item(**overrides):
    item = {
        "aplyStrtDt": "20200406",
        "bfhnAffcRtmTpcd": "2",
        "dcerCfrmLworCd": "89",
        "dcerCfrmLworNm": "수입식품안전관리 특별법",
        "hsSgn": "0303540000",
        "reqApreIttCd": "005",
        "reqApreIttNm": "식품의약품안전처",
        "reqCfrmIstmNm": "수입식품등 수입신고확인증",
    }
    item.update(overrides)
    return item


def test_dataset_preserves_empty_queries_and_every_raw_item():
    records = [
        {"hs": "0303540000", "tpcd": "2", "items": [_item()]},
        {"hs": "7318151000", "tpcd": "2", "items": []},
    ]
    dataset = build_dataset(records)
    assert len(dataset.queries) == 2
    assert [q["item_count"] for q in dataset.queries] == [1, 0]
    assert len(dataset.source_records) == 1
    assert dataset.source_records[0]["raw_record"] == records[0]["items"][0]


def test_requirement_id_distinguishes_agency_document_and_validity():
    base = dict(
        hsk10="3808929000",
        trade_type="import",
        law_code="01",
        agency_code="243",
        document_name="표준통관예정보고서(의약품등)",
        valid_from="2020-04-06",
        valid_to=None,
        rtm_tpcd="2",
    )
    original = canonical_requirement_id(**base)
    assert original == canonical_requirement_id(**base)
    assert original != canonical_requirement_id(**{**base, "agency_code": "261"})
    assert original != canonical_requirement_id(**{**base, "document_name": "수입허가증"})
    assert original != canonical_requirement_id(**{**base, "valid_from": "2024-07-30"})


def test_multiple_agencies_are_not_overwritten():
    records = [{
        "hs": "3808929000",
        "tpcd": "2",
        "items": [
            _item(
                hsSgn="3808929000",
                dcerCfrmLworCd="01",
                dcerCfrmLworNm="약사법",
                reqApreIttCd="243",
                reqApreIttNm="한국의약품수출입협회",
                reqCfrmIstmNm="표준통관예정보고서(의약품등)",
            ),
            _item(
                hsSgn="3808929000",
                dcerCfrmLworCd="01",
                dcerCfrmLworNm="약사법",
                reqApreIttCd="261",
                reqApreIttNm="한국동물약품협회",
                reqCfrmIstmNm="표준통관예정보고서(의약품등)",
            ),
        ],
    }]
    dataset = build_dataset(records)
    assert len(dataset.source_records) == 2
    assert len(dataset.requirements) == 2
    assert {r["agency_code"] for r in dataset.requirements} == {"243", "261"}


def test_snapshot_and_source_ids_are_deterministic():
    records = [{"hs": "0303540000", "tpcd": "2", "items": [_item()]}]
    first = build_dataset(records)
    second = build_dataset(records)
    assert first.snapshot_id == second.snapshot_id
    assert first.content_hash == second.content_hash
    assert first.source_records[0]["source_record_id"] == second.source_records[0]["source_record_id"]


def test_repository_progress_file_preserves_all_14726_raw_rows():
    dataset = build_dataset()
    assert len(dataset.queries) == 22654
    assert len(dataset.source_records) == 14726
    assert len({r["source_record_id"] for r in dataset.source_records}) == 14726
