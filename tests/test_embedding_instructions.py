"""Unit tests for embedding instruction constants."""
from src.embedding.instructions import INSTRUCT_NL_TO_CODE


def test_instruct_nl_to_code_ends_with_query_prefix():
    """The constant must end with 'Query: ' so it can be prepended to query text."""
    assert INSTRUCT_NL_TO_CODE.endswith("Query: ")


def test_instruct_nl_to_code_contains_instruct():
    assert "Instruct:" in INSTRUCT_NL_TO_CODE


def test_instruct_nl_to_code_is_string():
    assert isinstance(INSTRUCT_NL_TO_CODE, str)
    assert len(INSTRUCT_NL_TO_CODE) > 0
