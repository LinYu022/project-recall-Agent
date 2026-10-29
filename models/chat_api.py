"""One explicit DeepSeek-compatible JSON call, with no retries or credential serialization."""

import json
import os
from pathlib import Path
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from ingestion.snapshots import NoRedirects


class ModelCallFailed(RuntimeError):
    def __init__(self, code, diagnostics=None):
        self.code = code
        self.diagnostics = diagnostics
        super().__init__(code)


def local_configuration(path=Path(".env")) -> dict:
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in {
                "DEEPSEEK_API_KEY",
                "DEEPSEEK_BASE_URL",
                "RECALL_MODEL_API_KEY",
                "RECALL_MODEL_BASE_URL",
                "RECALL_MODEL_ID",
            }:
                values[key.strip()] = value.strip().strip("\"'")
    return values | {
        k: v
        for k, v in os.environ.items()
        if k
        in {
            "DEEPSEEK_API_KEY",
            "DEEPSEEK_BASE_URL",
            "RECALL_MODEL_API_KEY",
            "RECALL_MODEL_BASE_URL",
            "RECALL_MODEL_ID",
        }
    }


class ChatJSONModel:
    def __init__(self, config=None, *, opener=None):
        config = local_configuration() if config is None else config
        self.key = config.get("RECALL_MODEL_API_KEY") or config.get("DEEPSEEK_API_KEY")
        self.base = (
            config.get("RECALL_MODEL_BASE_URL")
            or config.get("DEEPSEEK_BASE_URL")
            or "https://api.deepseek.com"
        ).rstrip("/")
        url = urlsplit(self.base)
        if (
            url.scheme != "https"
            or url.hostname != "api.deepseek.com"
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.port not in {None, 443}
            or url.path not in {"", "/v1"}
        ):
            raise ValueError("model endpoint differs from user-approved DeepSeek HTTPS endpoint")
        self.model_id = config.get("RECALL_MODEL_ID", "deepseek-flash")
        self.identity = {
            "provider": "deepseek",
            "model_id": self.model_id,
            "endpoint": self.base,
            "is_simulated": False,
            "protocol": "chat_completions_json_object",
            "temperature": 0,
            "thinking": "disabled",
            "adapter_version": "chat-json/0.1",
            "provider_weights_revision": None,
        }
        self.opener = opener or build_opener(NoRedirects())

    def generate(self, system: str, payload: dict, *, max_tokens=2000) -> dict:
        if not self.key:
            raise ModelCallFailed("model_key_not_configured")
        body = {
            "model": self.model_id,
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        request = Request(
            self.base + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"},
            method="POST",
        )
        started = perf_counter()
        try:
            with self.opener.open(request, timeout=60) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ModelCallFailed("model_response_too_large")
            decoded = json.loads(raw)
        except HTTPError as error:
            raise ModelCallFailed("model_http_" + str(error.code)) from None
        except (OSError, URLError, TimeoutError):
            raise ModelCallFailed("model_transport_failed") from None
        except ValueError:
            raise ModelCallFailed("model_response_not_json") from None
        content, finish = None, None
        try:
            choice = decoded["choices"][0]
            content = choice["message"]["content"]
            finish = choice["finish_reason"]
            if not isinstance(content, str) or not content.strip():
                raise ModelCallFailed("model_content_empty")
            proposal = json.loads(content)
        except (KeyError, TypeError, IndexError, ValueError):
            diagnostics = {
                "identity": self.identity,
                "reported_model": decoded.get("model") if isinstance(decoded, dict) else None,
                "provider_response_id": decoded.get("id") if isinstance(decoded, dict) else None,
                "usage": decoded.get("usage") if isinstance(decoded, dict) else None,
                "finish_reason": finish,
                "raw_content": content,
                "elapsed_ms": (perf_counter() - started) * 1000,
                "estimated_cost": None,
                "max_tokens": max_tokens,
                "accepted_complete": False,
                "error_code": "model_content_invalid_json",
            }
            raise ModelCallFailed("model_content_invalid_json", diagnostics) from None
        return {
            "identity": self.identity,
            "reported_model": decoded.get("model"),
            "provider_response_id": decoded.get("id"),
            "finish_reason": finish,
            "proposal": proposal,
            "raw_content": content,
            "usage": decoded.get("usage"),
            "elapsed_ms": (perf_counter() - started) * 1000,
            "estimated_cost": None,
            "max_tokens": max_tokens,
            "accepted_complete": finish == "stop",
        }
