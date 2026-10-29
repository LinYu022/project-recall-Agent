"""Offline task validation, schema export, and synthetic execution demo."""

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from domain.tasks import TaskSpec
from models.adapter import FakeModelAdapter
from runtime.artifacts import ArtifactIntegrityError, ArtifactStore
from runtime.config import load_settings
from runtime.demo import run_demo
from tools.fake_query import FakeQueryTool


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["validate-task", "export-task-schema", "run-demo", "inspect-artifact"]
    )
    parser.add_argument("path", type=Path)
    parser.add_argument("--config", type=Path, default=Path("examples/dev-readonly.toml"))
    parser.add_argument("--tool", default="fake_query")
    parser.add_argument(
        "--scenario",
        choices=["success", "empty", "missing-source", "tool-error"],
        default="success",
    )
    parser.add_argument("--sha256")
    parser.add_argument("--with-model", action="store_true")
    parser.add_argument(
        "--model-scenario", choices=["success", "model-error", "bad-citation"], default="success"
    )
    args = parser.parse_args()
    if args.command == "inspect-artifact" and args.sha256 is None:
        parser.error("inspect-artifact requires --sha256")
    try:
        if args.command == "export-task-schema":
            schema = TaskSpec.model_json_schema()
            schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
            args.path.parent.mkdir(parents=True, exist_ok=True)
            args.path.write_text(
                json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            result = {"status": "exported", "schema_version": "exploration-task/0.2"}
        elif args.command == "inspect-artifact":
            result = ArtifactStore(args.path).read_json(args.sha256)
        elif args.command == "run-demo":
            task = TaskSpec.model_validate_json(args.path.read_text(encoding="utf-8"))
            settings = load_settings(args.config)
            model = FakeModelAdapter(args.model_scenario) if args.with_model else None
            result = run_demo(
                task,
                settings,
                {"fake_query": FakeQueryTool(args.scenario)},
                tool_name=args.tool,
                scenario=args.scenario,
                model=model,
            )
            print(json.dumps(result, ensure_ascii=False))
            return {"succeeded": 0, "denied": 4, "failed": 5}[result["status"]]
        else:
            task = TaskSpec.model_validate_json(args.path.read_text(encoding="utf-8"))
            result = {"status": "valid", "task_id": task.task_id, "check": "contract_only"}
    except ValidationError as error:
        result = {
            "status": "invalid",
            "errors": error.errors(include_input=False, include_context=False, include_url=False),
        }
        print(json.dumps(result, ensure_ascii=False))
        return 2
    except ArtifactIntegrityError:
        print(json.dumps({"status": "integrity_error"}))
        return 6
    except (OSError, UnicodeError) as error:
        print(json.dumps({"status": "io_error", "error_type": type(error).__name__}))
        return 3
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
