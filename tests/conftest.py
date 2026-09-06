from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def load_tool_module(name: str) -> Any:
    """Load a tools/*.py build script as an importable module.

    The build scripts are executables rather than a package, so tests load them
    the same way the pipeline does (build_final_platform_dataset.py:41-48).
    """
    path = REPO_ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"roopsee_tools_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def auto_scorer() -> Any:
    """tools/build_automated_scores.py — ingredient matching and product scoring."""
    return load_tool_module("build_automated_scores")


@pytest.fixture(scope="session")
def canonical_population() -> Any:
    """tools/canonical_population.py — the canonical v2 population loader."""
    return load_tool_module("canonical_population")


@pytest.fixture(scope="session")
def onboarding() -> Any:
    """tools/onboarding.py — eligibility and onboarding selection."""
    return load_tool_module("onboarding")


@pytest.fixture(scope="session")
def source_dir() -> Path:
    return REPO_ROOT / "data" / "source"


@pytest.fixture(scope="session")
def has_canonical_sources(source_dir: Path) -> bool:
    return (source_dir / "canonical_scoring_population_v2.csv").exists() and (
        source_dir / "scoring_input_ingredient_mapping_v2.csv"
    ).exists()
