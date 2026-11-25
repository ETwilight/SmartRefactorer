"""Load the local retrieval encoder and keep its per-run vectors and usage records."""

import json
from pathlib import Path
from audit.control import RunStopped
from algorithms.smartrefactorer.library import atomic, digest

from runtime.storage import MODELS_ROOT


class DenseEmbeddings:
    """Provide retrieval vectors with a persistent cache for this run.

    directory is the method-state Path containing embedding-cache.json. recorder
    collects inference usage and response records; guard checks the run limits.
    device and precision select the local BGE backend. encoder is initially None
    and becomes a MeteredDense on the first embed() call.

    The saved cache contains the encoder identity and a mapping from text hashes to
    float vectors. A cache is loaded when its identity matches the current encoder.
    New inference is recorded before its vectors are written to that cache.

    Example, using the same query/description pair as retrieval::

        embeddings = DenseEmbeddings(directory, recorder, guard)
        query, skill = embeddings.embed([task_text, skill_description])
        similarity = sum(a * b for a, b in zip(query, skill))

    similarity is the dot-product score used to rank this skill for the task.
    """

    def __init__(self, directory, recorder, guard, *, device='cpu', precision='float32'):
        """Keep the cache directory, recorder, run-limit callback, and backend options."""

        self.directory = Path(directory)
        self.recorder = recorder
        self.guard = guard
        self.encoder = None
        self.device = device
        self.precision = precision

    def embed(self, texts):
        """Return one vector per text, recording new inference and updating the saved cache."""
        if self.encoder is None:
            import threading
            from runtime.models.embedding import local_bge

            root = MODELS_ROOT / 'bge-m3'
            revision = json.loads((root / 'model-source.json').read_text())['revision']
            try:
                self.encoder = local_bge(
                    self.recorder,
                    root,
                    revision,
                    self.guard,
                    threading.RLock(),
                    device=self.device,
                    precision=self.precision,
                )
            except (RuntimeError, MemoryError, OSError) as exc:
                self.recorder.event(
                    'smartrefactorer.embedding.backend_unavailable',
                    error={'type': type(exc).__name__, 'message': str(exc)},
                    device=self.device,
                    precision=self.precision,
                )
                raise RunStopped(
                    'operator_stop',
                    'Embedding backend could not load; preserve state without attributing this to task code',
                ) from exc
            self.encoder.identity = {**self.encoder.identity, 'request_batch_size': 1}
            import math

            def normalized(vector):
                """Return a unit-length float list, or None when the vector is invalid."""

                try:
                    if (
                        not isinstance(vector, (list, tuple))
                        or not vector
                        or any((type(x) not in (int, float) for x in vector))
                    ):
                        return None
                    # values: list[float], a converted copy of the numeric input vector.
                    values = [float(x) for x in vector]
                    if self.encoder.dimensions and len(values) != self.encoder.dimensions:
                        return None
                    norm = math.sqrt(sum((x * x for x in values)))
                    if (
                        not math.isfinite(norm)
                        or not norm
                        or any((not math.isfinite(x) for x in values))
                    ):
                        return None
                    return [x / norm for x in values]
                except (ValueError, TypeError, OverflowError):
                    return None

            cache_path = self.directory / 'embedding-cache.json'
            if cache_path.exists():
                cached = json.loads(cache_path.read_text(encoding='utf-8'))
                if cached.get('identity') == self.encoder.identity:
                    # vectors: dict[str, list[float]], mapping text-and-model hashes to saved
                    # vectors.
                    vectors = cached.get('vectors', {})
                    if not isinstance(vectors, dict) or any(
                        (
                            not isinstance(k, str)
                            or len(k) != 64
                            or any((c not in '0123456789abcdef' for c in k))
                            or (normalized(v) is None)
                            or (abs(sum((float(x) * float(x) for x in v)) - 1) > 1e-05)
                            for k, v in vectors.items()
                        )
                    ):
                        self.recorder.event(
                            'smartrefactorer.embedding.cache_invalid', file=str(cache_path)
                        )
                        raise RunStopped(
                            'operator_stop',
                            'Invalid durable embedding cache; preserve evidence instead of silently recomputing or using invalid vectors',
                        )
                    self.encoder.cache.update(vectors)
            backend = self.encoder.backend

            def observed_backend(values):
                """Record backend inputs and outputs, then save the valid vectors for reuse."""
                # values: list[str]; vectors: list[list[float]], one model output per input
                # text.

                vectors = backend(values)
                calls = getattr(self.recorder, 'models', [])
                call_id = calls[-1].get('call_id') if calls else None
                self.recorder.event(
                    'smartrefactorer.embedding.response',
                    call_id=call_id,
                    input=values,
                    output=vectors,
                    identity=self.encoder.identity,
                )
                valid = [normalized(v) for v in vectors]
                if len(valid) == len(values) and all((v is not None for v in valid)):
                    cached_vectors = dict(self.encoder.cache)
                    cached_vectors.update(
                        {
                            digest([self.encoder.identity, text]): v
                            for text, v in zip(values, valid)
                        }
                    )
                    try:
                        atomic(
                            cache_path,
                            {'identity': self.encoder.identity, 'vectors': cached_vectors},
                        )
                    except OSError as exc:
                        self._embedding_cache_error = exc
                return vectors

            self.encoder.backend = observed_backend
        vectors = []
        try:
            for text in texts:
                vector = self.encoder.embed([text], 'smartrefactorer_retrieve')[0]
                if getattr(self, '_embedding_cache_error', None) is not None:
                    exc = self._embedding_cache_error
                    self.recorder.event(
                        'smartrefactorer.embedding.cache_write_failed',
                        error={'type': type(exc).__name__, 'message': str(exc)},
                    )
                    raise RunStopped(
                        'operator_stop',
                        'Embedding completed with known usage, but cache persistence failed; retain response evidence before any next input',
                    ) from exc
                vectors.append(vector)
        except (RuntimeError, MemoryError) as exc:
            self.recorder.event(
                'smartrefactorer.embedding.backend_failed',
                error={'type': type(exc).__name__, 'message': str(exc)},
                device=self.device,
                precision=self.precision,
            )
            raise RunStopped(
                'operator_stop',
                'Embedding backend failed; preserve actual usage and response evidence without CPU fallback or task-code repair',
            ) from exc
        except ValueError as exc:
            self.recorder.event(
                'smartrefactorer.embedding.invalid_output',
                error={'type': type(exc).__name__, 'message': str(exc)},
            )
            raise RunStopped(
                'operator_stop',
                'Embedding input/output rejected; preserve model accounting instead of attributing this to task code',
            ) from exc
        return vectors
