from __future__ import annotations

import pytest

from bandi.core.config import BandiConfig


@pytest.fixture
def test_config() -> BandiConfig:
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    cfg.agent.max_iterations = 1
    return cfg
