"""hermes-recall embedders (spec Task 3, REQ-016/019/026): FRIDA, pplx-embed-v1-0.6b and
pplx-embed-context-v1-0.6b behind one ``Embedder`` interface, on ONNX with no remote code.

``load(name)`` takes a short name (``frida``, ``pplx-v1``, ``pplx-context``) or a ``model_id`` and reads
``$HERMES_HOME/recall/models/<name>/{model.onnx,tokenizer.json}``. The session and tokenizer load lazily
on the first embed call; ``unload()`` drops them. ``IdleEmbedder`` (embedders_idle) adds the 15-minute
idle unload.

Model parameters come from the model folder when it states them, else from the defaults below (FRIDA
takes only its pooling from the folder; its dimensions, token limit and prefixes are pinned by the spec):

* dimensions: ``config.json`` ``hidden_size``;
* token limit: ``sentence_bert_config.json`` ``max_seq_length``, else ``tokenizer_config.json``
  ``model_max_length``; an input above it (prefix included, counted with the model's tokenizer) is refused;
* prefixes: ``config_sentence_transformers.json`` ``prompts.query`` / ``prompts.document``;
* pooling: ``1_Pooling/config.json`` (``pooling_mode_cls_token`` / ``pooling_mode_mean_tokens``) applied to
  ``last_hidden_state``; a graph output named ``sentence_embedding`` is used as is, ``pooler_output`` only
  when there is no ``last_hidden_state`` (in BERT exports it is tanh(dense(CLS)), not the sentence pooling).

A present but unreadable parameter file is an error, not a default.

Defaults (assumptions, no model card was fetched):

* FRIDA (``ai-forever/FRIDA@int8``): 1536 dims, 512 tokens, ``search_query: `` / ``search_document: ``
  prefixes (design.md), CLS pooling (the FRIDA usage example pools the first token);
* pplx-embed-v1-0.6b and pplx-embed-context-v1-0.6b (``perplexity-ai/<name>@onnx``): 1024 dims, 32768
  tokens, no prefixes, mean pooling over the attention mask (as the llm-wiki pplx adapter documents).
  pplx-context embeds each text on its own; contextual (late-chunking) document batches are not used.

Vectors are L2-normalised.
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from hermes_recall.embedders_idle import IdleEmbedder

__all__ = ["Embedder", "IdleEmbedder", "ModelSpec", "MODELS", "OnnxEmbedder", "QueryEmbedder", "load", "models_dir",
           "name_for"]

QUERY_WAIT = 1.5  # seconds a query embedding waits behind a background document batch (~25 s on CPU)


class QueryEmbedder(IdleEmbedder):
    """``IdleEmbedder`` whose query path gives up on a busy embedder: ``embed_query_bounded`` returns None
    when a background batch still holds it after ``wait`` seconds, so a search falls back to BM25."""

    def embed_query_bounded(self, text: str, wait: float = QUERY_WAIT) -> list[float] | None:
        if not self._lock.acquire(timeout=wait):
            return None
        try:
            self._loaded = True
            try:
                return self.inner.embed_query(text)
            finally:
                self._last = self._now()
        finally:
            self._lock.release()


class Embedder(Protocol):
    model_id: str
    dimensions: int
    max_tokens: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def unload(self) -> None: ...


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    dimensions: int
    max_tokens: int
    query_prefix: str
    document_prefix: str
    pooling: str  # "mean" or "cls"


MODELS: dict[str, ModelSpec] = {
    "frida": ModelSpec("ai-forever/FRIDA@int8", 1536, 512, "search_query: ", "search_document: ", "cls"),
    "pplx-v1": ModelSpec("perplexity-ai/pplx-embed-v1-0.6b@onnx", 1024, 32768, "", "", "mean"),
    "pplx-context": ModelSpec("perplexity-ai/pplx-embed-context-v1-0.6b@onnx", 1024, 32768, "", "", "mean"),
}
_BY_MODEL_ID = {spec.model_id: name for name, spec in MODELS.items()}
_REQUIRED = ("model.onnx", "tokenizer.json")
_PAD_TOKENS = ("<pad>", "[PAD]", "<|endoftext|>")
_HUGE = 1_000_000  # tokenizer_config's "no limit" sentinel is ~1e30


def models_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "recall" / "models"


def name_for(model_id: str) -> str:
    """The short name of a registered ``model_id``."""
    if model_id not in _BY_MODEL_ID:
        raise ValueError("unknown embedding model id %r; known: %s" % (model_id, ", ".join(_BY_MODEL_ID)))
    return _BY_MODEL_ID[model_id]


def load(name: str) -> "OnnxEmbedder":
    """The embedder ``name`` (short name or model id); its files are checked now, loaded on first use."""
    short = _BY_MODEL_ID.get(name, name)
    folder = models_dir() / short
    if short not in MODELS:
        raise ValueError("unknown embedding model %r: expected one of %s, with files in %s"
                         % (name, ", ".join(MODELS), folder))
    for fname in _REQUIRED:
        if not (folder / fname).is_file():
            raise FileNotFoundError("embedding model %r: missing %s" % (short, folder / fname))
    spec = _spec_from_files(MODELS[short], folder)
    if short == "frida":
        spec = dataclasses.replace(MODELS["frida"], pooling=spec.pooling)
    return OnnxEmbedder(spec, folder)


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError("unreadable model parameter file %s: %s" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise ValueError("unreadable model parameter file %s: expected a JSON object" % path)
    return data


def _spec_from_files(spec: ModelSpec, folder: Path) -> ModelSpec:
    config = _json(folder / "config.json")
    limit = _json(folder / "sentence_bert_config.json").get("max_seq_length")
    if not isinstance(limit, int):
        limit = _json(folder / "tokenizer_config.json").get("model_max_length")
    prompts = _json(folder / "config_sentence_transformers.json").get("prompts") or {}
    pool = _json(folder / "1_Pooling" / "config.json")
    pooling = "cls" if pool.get("pooling_mode_cls_token") else "mean" if pool.get("pooling_mode_mean_tokens") else None
    dims = config.get("hidden_size")
    return ModelSpec(
        model_id=spec.model_id,
        dimensions=dims if isinstance(dims, int) and dims > 0 else spec.dimensions,
        max_tokens=limit if isinstance(limit, int) and 0 < limit < _HUGE else spec.max_tokens,
        query_prefix=prompts.get("query", spec.query_prefix) if isinstance(prompts, dict) else spec.query_prefix,
        document_prefix=(prompts.get("document", spec.document_prefix) if isinstance(prompts, dict)
                         else spec.document_prefix),
        pooling=pooling or spec.pooling,
    )


class OnnxEmbedder:
    """An ``Embedder`` on an ONNX encoder; ``loaded`` is true from the first embed call until ``unload()``."""

    def __init__(self, spec: ModelSpec, folder: Path):
        self.spec, self.folder = spec, folder
        self.model_id, self.dimensions, self.max_tokens = spec.model_id, spec.dimensions, spec.max_tokens
        self._session: Any = None
        self._tokenizer: Any = None
        self._lock = threading.RLock()

    @property
    def loaded(self) -> bool:
        return self._session is not None

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([self.spec.document_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self.spec.query_prefix + text])[0]

    def unload(self) -> None:
        with self._lock:
            self._session = self._tokenizer = None

    def _load(self) -> tuple[Any, Any]:
        with self._lock:
            if self._session is None:
                # Looked up on the modules at call time, so a test's fake session is the one used.
                import onnxruntime
                import tokenizers

                tokenizer = tokenizers.Tokenizer.from_file(str(self.folder / "tokenizer.json"))
                tokenizer.no_truncation()
                tokenizer.no_padding()
                session = onnxruntime.InferenceSession(str(self.folder / "model.onnx"),
                                                       providers=["CPUExecutionProvider"])
                self._session, self._tokenizer = session, tokenizer
            return self._session, self._tokenizer

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            session, tokenizer = self._load()
            encoded = [tokenizer.encode(t).ids for t in texts]
            for ids in encoded:
                if len(ids) > self.max_tokens:
                    raise ValueError("%s accepts at most %d tokens, the input has %d"
                                     % (self.model_id, self.max_tokens, len(ids)))
            pad = next((i for i in map(tokenizer.token_to_id, _PAD_TOKENS) if i is not None), 0)
            width = max(len(ids) for ids in encoded) or 1
            ids = np.full((len(encoded), width), pad, dtype=np.int64)
            mask = np.zeros((len(encoded), width), dtype=np.int64)
            for row, e in enumerate(encoded):
                ids[row, : len(e)], mask[row, : len(e)] = e, 1
            pooled = self._pool(session, ids, mask)
        if pooled.shape[1] != self.dimensions:
            raise ValueError("%s produced %d dimensions, expected %d" % (self.model_id, pooled.shape[1], self.dimensions))
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        if not np.isfinite(pooled).all() or (norms <= 0).any():
            raise ValueError("%s produced an invalid embedding vector" % self.model_id)
        return (pooled / norms).tolist()

    def _pool(self, session: Any, ids: np.ndarray, mask: np.ndarray) -> np.ndarray:
        known = {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids),
                 "position_ids": np.maximum(mask.cumsum(1) - 1, 0)}
        feed = {}
        for i in session.get_inputs():
            if i.name not in known:
                raise ValueError("%s: unsupported ONNX input %r" % (self.model_id, i.name))
            feed[i.name] = known[i.name]
        names = [o.name for o in session.get_outputs()]
        if "sentence_embedding" in names:
            return np.asarray(session.run(["sentence_embedding"], feed)[0], dtype=np.float64)
        if "last_hidden_state" in names:
            hidden = np.asarray(session.run(["last_hidden_state"], feed)[0], dtype=np.float64)
        elif "pooler_output" in names:
            return np.asarray(session.run(["pooler_output"], feed)[0], dtype=np.float64)
        else:
            hidden = np.asarray(session.run(None, feed)[0], dtype=np.float64)
        if hidden.ndim == 2:
            return hidden
        if self.spec.pooling == "cls":
            return hidden[:, 0, :]
        weights = mask[..., None].astype(np.float64)
        return (hidden * weights).sum(1) / np.maximum(weights.sum(1), 1.0)
