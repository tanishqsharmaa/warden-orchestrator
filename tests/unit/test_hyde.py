import time

from warden_orchestrator.hyde import HyDEExpander


def test_hyde_expands_query_with_contextual_keywords():
    expander = HyDEExpander()
    query = "bereavement leave policy"
    expanded = expander.expand_query(query, caller_role="Employee")

    assert "bereavement" in expanded.lower()
    assert "leave" in expanded.lower()
    assert "policy" in expanded.lower()
    assert "days" in expanded.lower() or "eligibility" in expanded.lower() or "employee" in expanded.lower()

def test_hyde_latency_budget_under_15ms():
    expander = HyDEExpander()
    start = time.perf_counter()
    for _ in range(100):
        _ = expander.expand_query("tuition reimbursement eligibility", caller_role="Employee")
    avg_latency_ms = ((time.perf_counter() - start) / 100.0) * 1000.0

    assert avg_latency_ms < 15.0

def test_hyde_word_budget():
    expander = HyDEExpander()
    expanded = expander.expand_query("parental leave duration for fathers", caller_role="Employee")
    words = expanded.split()
    assert len(words) <= 60

def test_hyde_handles_empty_or_generic_queries():
    expander = HyDEExpander()
    assert expander.expand_query("", caller_role="Employee") == ""
    generic = expander.expand_query("hello world", caller_role="Employee")
    assert "hello world" in generic
