from warden_orchestrator.compressor import ContextCompressor
from warden_orchestrator.models import Passage


def test_compress_passages_reduces_tokens_and_preserves_citations():
    compressor = ContextCompressor()
    boilerplate = "CONFIDENTIAL: This policy is internal proprietary information. All rights reserved. Do not distribute. "
    substance1 = "Full-time employees accrue 18 days of paid time off annually. PTO accrual begins on the first day of employment."
    substance2 = "Unused PTO up to 5 days may be carried over into the following calendar year. Excess unused PTO is forfeited on December 31."

    passages = [
        Passage(
            doc_id="DOC-HR-LEAVE-2026",
            chunk_index=0,
            content=f"{boilerplate * 10} {substance1}",
            source_url="file:///data/policies/pto_policy_2026.md",
            rrf_score=0.032,
            calibrated_score=0.94,
        ),
        Passage(
            doc_id="DOC-HR-LEAVE-2026",
            chunk_index=1,
            content=f"{boilerplate * 10} {substance2}",
            source_url="file:///data/policies/pto_policy_2026.md",
            rrf_score=0.028,
            calibrated_score=0.88,
        ),
    ]

    compressed_text, token_count, retained = compressor.compress_passages(passages, target_tokens=800)

    assert token_count <= 800
    assert len(retained) == 2
    assert "[Doc: DOC-HR-LEAVE-2026, Chunk: 0]" in compressed_text
    assert "[Doc: DOC-HR-LEAVE-2026, Chunk: 1]" in compressed_text
    assert "18 days of paid time off" in compressed_text
    assert "carried over into the following calendar year" in compressed_text
    # Boilerplate was stripped
    assert "CONFIDENTIAL: This policy is internal proprietary information" not in compressed_text

def test_compress_passages_deduplicates_overlapping_sentences():
    compressor = ContextCompressor()
    shared_sentence = "Bereavement leave covers up to 5 consecutive business days for immediate family members."

    passages = [
        Passage(
            doc_id="DOC-HR-LEAVE-2026",
            chunk_index=2,
            content=f"Section 4: Bereavement Provisions. {shared_sentence} Immediate family includes spouse and children.",
            source_url="file:///data/policies/pto_policy_2026.md",
            rrf_score=0.03,
            calibrated_score=0.91,
        ),
        Passage(
            doc_id="DOC-HR-LEAVE-2026",
            chunk_index=3,
            content=f"{shared_sentence} Additional unpaid leave up to 10 days may be requested through HR.",
            source_url="file:///data/policies/pto_policy_2026.md",
            rrf_score=0.02,
            calibrated_score=0.85,
        ),
    ]

    compressed_text, token_count, retained = compressor.compress_passages(passages, target_tokens=800)
    assert len(retained) == 2
    # The shared sentence should only appear once in the extracted context
    count = compressed_text.count("Bereavement leave covers up to 5 consecutive business days for immediate family members.")
    assert count == 1
    assert "Additional unpaid leave" in compressed_text

def test_compress_passages_fallback_on_empty():
    compressor = ContextCompressor()
    text, token_count, retained = compressor.compress_passages([])
    assert text == ""
    assert token_count == 0
    assert retained == []

def test_compress_passages_fallback_when_all_boilerplate():
    compressor = ContextCompressor()
    passages = [
        Passage(
            doc_id="DOC-BOILERPLATE",
            chunk_index=0,
            content="CONFIDENTIAL: Internal use only. All rights reserved.",
            source_url="file:///data/boilerplate.md",
            rrf_score=0.01,
            calibrated_score=0.5,
        )
    ]
    text, token_count, retained = compressor.compress_passages(passages, target_tokens=800)
    # Since stripping resulted in empty spans, fallback retains raw chunk safely
    assert "[Doc: DOC-BOILERPLATE, Chunk: 0]" in text
    assert len(text) > 0
    assert token_count > 0
    assert len(retained) == 1
