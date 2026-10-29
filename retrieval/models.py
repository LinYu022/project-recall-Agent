"""CPU-only ONNX adapters, with local verified weights and recorded truncation."""

import hashlib
import json
from pathlib import Path
from typing import Protocol

import numpy as np
import onnxruntime as ort
import tokenizers
from tokenizers import Tokenizer


class Encoder(Protocol):
    identity: dict

    def encode(self, texts: list[str]) -> tuple[np.ndarray, dict]: ...


class Reranker(Protocol):
    identity: dict

    def score(self, query: str, passages: list[str]) -> tuple[list[float], dict]: ...


class LocalModel:
    def __init__(
        self,
        role: str,
        root=Path(".local/retrieval/models"),
        manifest=Path("examples/retrieval-models.json"),
    ):
        spec = next(m for m in json.loads(manifest.read_bytes())["models"] if m["role"] == role)
        base = root / role / spec["revision"]
        for asset in spec["files"]:
            path = (base / asset["path"]).resolve()
            if not path.is_relative_to(base.resolve()):
                raise ValueError("model asset outside pinned directory")
            body = path.read_bytes()
            if (
                len(body) != asset["byte_size"]
                or hashlib.sha256(body).hexdigest() != asset["sha256"]
            ):
                raise ValueError("model asset hash mismatch")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(base / "onnx/model_quantized.onnx"), options, providers=["CPUExecutionProvider"]
        )
        self.tokenizer = Tokenizer.from_file(str(base / "tokenizer.json"))
        self.raw_tokenizer = Tokenizer.from_file(str(base / "tokenizer.json"))
        self.tokenizer.enable_truncation(
            max_length=spec["max_length"],
            strategy="only_second" if role == "reranker" else "longest_first",
        )
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        self.role = role
        self.dimensions = spec["dimensions"]
        self.identity = {
            "spec": spec,
            "onnxruntime": ort.__version__,
            "tokenizers": tokenizers.__version__,
            "numpy": np.__version__,
            "provider": "CPUExecutionProvider",
            "batch_size": 8,
            "threads": 1,
            "adapter_version": "local-minilm/0.1",
            "synthetic": False,
        }

    def _infer(self, texts: list[str], query: str | None = None) -> tuple[list, dict]:
        if query is not None and len(self.raw_tokenizer.encode(query).ids) > 128:
            raise ValueError("reranker query exceeds 128 tokens; shorten it explicitly")
        outputs, truncated = [], 0
        for start in range(0, len(texts), 8):
            batch = texts[start : start + 8]
            encoded = self.tokenizer.encode_batch(
                [(query, t) for t in batch] if query is not None else batch
            )
            truncated += sum(bool(e.overflowing) for e in encoded)
            tensors = {
                "input_ids": np.asarray([e.ids for e in encoded], dtype=np.int64),
                "attention_mask": np.asarray([e.attention_mask for e in encoded], dtype=np.int64),
                "token_type_ids": np.asarray([e.type_ids for e in encoded], dtype=np.int64),
            }
            inputs = {i.name: tensors[i.name] for i in self.session.get_inputs()}
            result = self.session.run(None, inputs)[0]
            if not np.isfinite(result).all():
                raise ValueError("nonfinite model output")
            if self.role == "encoder":
                mask = tensors["attention_mask"][..., None]
                result = (result * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1)
                norm = np.linalg.norm(result, axis=1, keepdims=True)
                if np.any(norm <= 0):
                    raise ValueError("zero embedding")
                result = result / norm
            else:
                if result.shape != (len(batch), 1):
                    raise ValueError("reranker requires one raw logit per query/passage")
                result = result[:, 0]
            outputs.extend(result.tolist())
        return outputs, {
            "inputs": len(texts),
            "truncated_inputs": truncated,
            "batches": (len(texts) + 7) // 8,
            "max_length": self.identity["spec"]["max_length"],
        }

    def encode(self, texts: list[str]) -> tuple[np.ndarray, dict]:
        if self.role != "encoder":
            raise ValueError("encoder role required")
        values, usage = self._infer(texts)
        return np.asarray(values, dtype=np.float32).reshape((-1, self.dimensions)), usage

    def score(self, query: str, passages: list[str]) -> tuple[list[float], dict]:
        if self.role != "reranker":
            raise ValueError("reranker role required")
        return self._infer(passages, query)
