import os

import pytest


@pytest.fixture(autouse=True)
def _no_review_password(monkeypatch):
    """load_config() carrega o .env real; nos testes a fila roda sem senha."""
    monkeypatch.delenv("REVIEW_PASSWORD", raising=False)
    os.environ.pop("REVIEW_PASSWORD", None)
