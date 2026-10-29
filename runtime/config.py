"""Explicit, synthetic-only development configuration."""

import os
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from domain.tasks import Contract, Text

DEMO_RELEASE = "demo-fixture-v1"
DEMO_SOURCE = "SIM-COMPLAINT-001"


class DevSettings(Contract):
    profile: Literal["development_readonly"]
    data_mode: Literal["synthetic"]
    release_id: Literal["demo-fixture-v1"]
    grant_id: Text
    revision_id: Text
    allowed_tools: list[Literal["fake_query"]]
    allowed_source_ids: list[Literal["SIM-COMPLAINT-001"]]
    max_tool_calls: Annotated[int, Field(strict=True, ge=0, le=1)]
    allowed_models: list[Text] = Field(default_factory=list)
    max_model_calls: Annotated[int, Field(strict=True, ge=0, le=1)] = 0
    artifact_dir: Path


def load_settings(path: Path) -> DevSettings:
    values = tomllib.loads(path.read_text(encoding="utf-8"))
    if "RECALL_ARTIFACT_DIR" in os.environ:
        values["artifact_dir"] = os.environ["RECALL_ARTIFACT_DIR"]
    return DevSettings.model_validate(values)
