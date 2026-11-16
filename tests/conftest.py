from pathlib import Path

import pytest

from solakit.config import EffectiveProfile, OperatorConfig, resolve_profile


@pytest.fixture
def profile(tmp_path: Path) -> EffectiveProfile:
    return resolve_profile(
        OperatorConfig.model_validate(
            {
                "data_dir": str(tmp_path / "data"),
                "model": {"pin": "fixture-qwen"},
                "defaults": {"workspace_roots": [str(tmp_path)]},
            }
        )
    )
