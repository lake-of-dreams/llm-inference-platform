import pytest
from app.cache import CacheScope, SemanticCache, cosine, embed
from app.router import Classification

INTERNAL = CacheScope.of(Classification.INTERNAL, "qwen-small")


def test_numeric_near_miss_is_blocked():
    c = SemanticCache(threshold=0.80)
    c.put("what is the balance for account 118", "Balance is 42", INTERNAL)
    # same wording, different account. must not be a hit.
    assert c.get("what is the balance for account 811", INTERNAL) is None
    assert c.false_hits_blocked == 1


def test_true_paraphrase_is_served():
    c = SemanticCache(threshold=0.50)
    c.put("what is the capital of france", "Paris", INTERNAL)
    assert c.get("what is the capital of france", INTERNAL) == "Paris"
    assert c.hits == 1


def test_restricted_answer_never_reaches_a_public_caller():
    c = SemanticCache(threshold=0.50)
    restricted = CacheScope.of(Classification.RESTRICTED, "qwen-small")
    public = CacheScope.of(Classification.PUBLIC, "qwen-small")
    c.put("summarise the payroll file", "Salaries: ...", restricted)
    assert c.get("summarise the payroll file", public) is None
    assert c.get("summarise the payroll file", restricted) == "Salaries: ..."


@pytest.mark.parametrize("other", [
    CacheScope.of(Classification.INTERNAL, "phi4-mini"),
    CacheScope.of(Classification.INTERNAL, "qwen-small", temperature=0.9),
    CacheScope.of(Classification.INTERNAL, "qwen-small", max_tokens=16),
    CacheScope.of(Classification.INTERNAL, "qwen-small", system_prompt="answer in French"),
])
def test_answers_do_not_cross_model_or_sampling_settings(other):
    c = SemanticCache(threshold=0.50)
    c.put("what is the capital of france", "Paris", INTERNAL)
    assert c.get("what is the capital of france", other) is None


def test_least_recently_used_entry_is_evicted_first():
    c = SemanticCache(threshold=0.99, max_entries=2)
    c.put("alpha question", "a", INTERNAL)
    c.put("beta question", "b", INTERNAL)
    assert c.get("alpha question", INTERNAL) == "a"   # alpha is now the most recent
    c.put("gamma question", "g", INTERNAL)
    assert len(c) == 2 and c.evicted == 1
    assert c.get("beta question", INTERNAL) is None
    assert c.get("alpha question", INTERNAL) == "a"


def test_entries_expire():
    now = [1000.0]
    c = SemanticCache(threshold=0.99, ttl_seconds=60, clock=lambda: now[0])
    c.put("alpha question", "a", INTERNAL)
    now[0] += 61
    assert c.get("alpha question", INTERNAL) is None
    assert c.expired == 1 and len(c) == 0


def test_pluggable_dense_embedder():
    table = {"cats": [1.0, 0.0], "felines": [0.96, 0.28], "dogs": [0.0, 1.0]}
    c = SemanticCache(threshold=0.9, embedder=lambda p: table[p])
    c.put("cats", "meow", INTERNAL)
    assert c.get("felines", INTERNAL) == "meow"
    assert c.get("dogs", INTERNAL) is None


def test_cosine_rejects_mixed_vector_kinds():
    with pytest.raises(TypeError):
        cosine(embed("a b"), [1.0, 0.0])
