"""Groq model resolver (post-deprecación llama-3.1-8b-instant)."""

from ia_groq import DEFAULT_MODEL, modelo_groq


def test_modelo_default():
    assert DEFAULT_MODEL == "openai/gpt-oss-20b"
    assert modelo_groq({}) == "openai/gpt-oss-20b"


def test_remapea_llama_retirado():
    cfg = {"groq": {"model": "llama-3.1-8b-instant"}}
    assert modelo_groq(cfg) == "openai/gpt-oss-20b"


def test_respeta_modelo_custom():
    cfg = {"groq": {"model": "openai/gpt-oss-120b"}}
    assert modelo_groq(cfg) == "openai/gpt-oss-120b"
