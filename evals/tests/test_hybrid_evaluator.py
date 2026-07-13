from evals.hybrid.evaluator import evaluate_hybrid_case, summarize_hybrid
from evals.contract import stable_evidence_id


def _response(ranking):
    evidence = []
    for index, (law_id, article_no) in enumerate(ranking):
        source_record_id = f"law_article:{index + 1}"
        version_hash = f"{index + 1:064x}"
        evidence.append({
            "evidence_id": stable_evidence_id(source_record_id, version_hash),
            "source_record_id": source_record_id,
            "source_type": "law_article",
            "item_id": "law_query_item",
            "title": f"Law {article_no}",
            "text": "Article text",
            "law_id": law_id,
            "law_name": "Law",
            "article_no": article_no,
            "source_uri": f"https://example.test/law/{index + 1}",
            "valid_from": "2026-01-01",
            "valid_to": None,
            "version_hash": version_hash,
            "matched_hs_code": None,
            "trade_type": "import",
            "required_document_code": None,
            "required_document_name": None,
            "agencies": [],
            "retrieval_methods": ["dense", "bm25"],
            "eligibility": "supporting",
            "score": 0.9,
            "raw_scores": {"rrf": 0.03},
        })
    return {
        "schema_version": "1.0",
        "request_id": "hybrid_eval:test",
        "status": "ok",
        "knowledge_snapshot_id": "snapshot-1",
        "results": [{
            "item_id": "law_query_item",
            "hs_resolution": "exact",
            "resolved_hsk10": "7318151000",
            "candidate_hsk10s": ["7318151000"],
            "coverage": "complete",
            "requirements_found": False,
            "evidence": evidence,
            "errors": [],
        }],
        "errors": [],
    }


def test_hybrid_metrics_use_rank_and_relevance_gain():
    case = {
        "case_id": "test",
        "description": "test",
        "query": "query",
        "relevant": [
            {"law_id": "L", "article_no": "제1조", "gain": 3},
            {"law_id": "L", "article_no": "제2조", "gain": 2},
        ],
    }
    report = evaluate_hybrid_case(
        case,
        _response([("L", "제1조"), ("X", "제9조"), ("L", "제2조")]),
    )
    assert report["passed"] is True
    assert report["metrics"]["hit_at_5"] == 1
    assert report["metrics"]["recall_at_10"] == 1.0
    assert 0.8 < report["metrics"]["ndcg_at_10"] < 1.0
    assert summarize_hybrid([report])["all_passed"] is True


def test_hybrid_gate_fails_when_relevant_article_is_missing():
    case = {
        "case_id": "test",
        "description": "test",
        "query": "query",
        "relevant": [{"law_id": "L", "article_no": "제1조", "gain": 3}],
    }
    report = evaluate_hybrid_case(case, _response([("X", "제9조")]))
    assert report["passed"] is False
    assert report["metrics"]["hit_at_5"] == 0
    assert report["metrics"]["recall_at_10"] == 0.0
