"""Local, synchronous, synthetic-only HTTP entry point."""

import json
from dataclasses import dataclass
from pathlib import Path as FilePath
from typing import Annotated, Callable

from fastapi import Depends, FastAPI, HTTPException, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import StrictBool

from domain.tasks import Contract, TaskSpec, Text
from models.adapter import FakeModelAdapter, ModelAdapter, ModelScenario
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore
from runtime.config import DevSettings, load_settings
from runtime.demo import run_demo
from tools.fake_query import FakeQueryTool, QueryTool, Scenario

RunId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
Digest = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]


class DemoRunRequest(Contract):
    task: TaskSpec
    tool: Text = "fake_query"
    scenario: Scenario = "success"
    with_model: StrictBool = True
    model_scenario: ModelScenario = "success"


@dataclass
class Services:
    settings: DevSettings
    tool_factory: Callable[[Scenario], QueryTool]
    model_factory: Callable[[ModelScenario], ModelAdapter]


def get_services(request: Request) -> Services:
    return request.app.state.services


InjectedServices = Annotated[Services, Depends(get_services)]


def _run_directory(settings: DevSettings, run_id: str) -> FilePath:
    root = settings.artifact_dir.resolve()
    path = (root / run_id).resolve()
    if not path.is_relative_to(root) or not (path / "manifest.json").is_file():
        raise HTTPException(status_code=404, detail="run_not_found")
    return path


def _public_run(result: dict) -> dict:
    public = {key: value for key, value in result.items() if key != "run_dir"}
    base = f"/demo/runs/{result['run_id']}"
    public["links"] = {"self": base, "events": base + "/events"}
    for field in (
        "input_artifact",
        "output_artifact",
        "model_input_artifact",
        "model_output_artifact",
    ):
        ref = result.get(field)
        public["links"][field] = f"{base}/artifacts/{ref['sha256']}" if ref else None
    return public


def create_app(
    settings: DevSettings | None = None,
    *,
    tool_factory: Callable[[Scenario], QueryTool] = FakeQueryTool,
    model_factory: Callable[[ModelScenario], ModelAdapter] = FakeModelAdapter,
) -> FastAPI:
    app = FastAPI(title="Recall investigation development API", version="0.1.0")
    config_path = FilePath(__file__).resolve().parents[1] / "examples/dev-readonly.toml"
    app.state.services = Services(
        settings or load_settings(config_path), tool_factory, model_factory
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        errors = [{key: item[key] for key in ("type", "loc", "msg")} for item in error.errors()]
        return JSONResponse(status_code=422, content={"status": "invalid", "errors": errors})

    @app.exception_handler(ArtifactIntegrityError)
    async def corrupt_artifact(request: Request, error: ArtifactIntegrityError):
        return JSONResponse(status_code=409, content={"status": "integrity_error"})

    @app.exception_handler(OSError)
    async def storage_error(request: Request, error: OSError):
        return JSONResponse(status_code=500, content={"status": "io_error"})

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "data_mode": "synthetic",
            "model_provider": "fake",
            "execution": "synchronous",
        }

    @app.post(
        "/demo/runs",
        responses={
            403: {"description": "Development policy denied"},
            502: {"description": "Tool or model failed"},
        },
    )
    def create_run(body: DemoRunRequest, services: InjectedServices) -> JSONResponse:
        model = services.model_factory(body.model_scenario) if body.with_model else None
        result = run_demo(
            body.task,
            services.settings,
            {"fake_query": services.tool_factory(body.scenario)},
            tool_name=body.tool,
            scenario=body.scenario,
            model=model,
        )
        status_code = {"succeeded": 201, "denied": 403, "failed": 502}[result["status"]]
        return JSONResponse(status_code=status_code, content=_public_run(result))

    @app.get("/demo/runs/{run_id}")
    def read_run(run_id: RunId, services: InjectedServices) -> dict:
        directory = _run_directory(services.settings, run_id)
        return _public_run(json.loads((directory / "manifest.json").read_text(encoding="utf-8")))

    @app.get("/demo/runs/{run_id}/events")
    def read_events(run_id: RunId, services: InjectedServices) -> dict:
        directory = _run_directory(services.settings, run_id)
        lines = (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
        return {"run_id": run_id, "events": [json.loads(line) for line in lines]}

    @app.get("/demo/runs/{run_id}/artifacts/{digest}")
    def read_artifact(run_id: RunId, digest: Digest, services: InjectedServices) -> Response:
        directory = _run_directory(services.settings, run_id)
        try:
            return Response(
                content=ArtifactStore(directory).read_bytes(digest), media_type="application/json"
            )
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="artifact_not_found") from None

    return app
