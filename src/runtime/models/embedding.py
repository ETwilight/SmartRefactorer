"""Metered local dense embedding transport shared by independent methods.

Extracted from the existing generic BGE utility, not from its algorithm. Model
identity, float32 CLS pooling, actual token counts and durable usage are retained.
No component graph, learning, retrieval policy or implicit download is imported.
"""

from pathlib import Path
import hashlib
import json
import math
import threading
from audit.recorder import TokenBudgetExceeded, budget_bounded
from audit.control import RunStopped


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False
        ).encode()
    ).hexdigest()


class MeteredDense:
    """Injected dense backend; cache is per run and keyed by revision+exact text."""

    def __init__(
        self,
        recorder,
        identity,
        backend,
        token_count,
        guard=lambda: None,
        lock=None,
        dimensions=None,
        max_length=8192,
    ):
        self.recorder, self.identity = recorder, identity
        self.backend, self.token_count, self.guard = backend, token_count, guard
        self.lock, self.cache = lock or threading.RLock(), {}
        self.dimensions, self.max_length = dimensions, max_length

    def embed(self, texts, scope):
        if any(not isinstance(t, str) for t in texts):
            raise ValueError('Embedding inputs must be strings')
        with self.lock:
            self.guard()
            keys = [digest([self.identity, text]) for text in texts]
            pending = {}
            for key, text in zip(keys, texts):
                if key not in self.cache:
                    pending[key] = text
            if pending:
                counts = [self.token_count(t) for t in pending.values()]
                if any(type(n) is not int or n < 1 or n > self.max_length for n in counts):
                    raise ValueError(
                        'Embedding input would be truncated or has invalid token count'
                    )
                payload = {
                    'model': self.identity,
                    'input': list(pending.values()),
                    'max_output_tokens': 16,
                }
                call = self.recorder.before_model(payload, scope + '_embedding', sum(counts))
                try:
                    self.guard()
                    self.recorder.before_send(call)
                except BaseException as exc:
                    self.recorder.cancel_model(
                        call, {'type': type(exc).__name__, 'message': str(exc)}
                    )
                    raise
                try:
                    vectors = [
                        list(map(float, v)) for v in self.backend(list(pending.values()))
                    ]
                    # Local inference completed: input count is known even if the
                    # backend output turns out invalid. Record it before validation.
                    self.recorder.after_model(
                        call,
                        {
                            'usage': {
                                'input_tokens': sum(counts),
                                'output_tokens': 0,
                                'total_tokens': sum(counts),
                            },
                            'status': 'completed',
                            'local_inference': True,
                        },
                    )
                    if len(vectors) != len(pending):
                        raise ValueError('Incomplete dense embedding result')
                    dimensions = self.dimensions or (len(vectors[0]) if vectors else 0)
                    for vector in vectors:
                        if (
                            len(vector) != dimensions
                            or not vector
                            or any(not math.isfinite(x) for x in vector)
                        ):
                            raise ValueError('Invalid embedding shape/value')
                        norm = math.sqrt(sum(x * x for x in vector))
                        if not norm:
                            raise ValueError('Zero dense vector')
                        vector[:] = [x / norm for x in vector]
                    self.dimensions = dimensions
                    self.cache.update(zip(pending, vectors))
                except BaseException as exc:
                    if '_started' in call:
                        self.recorder.after_model(
                            call, error={'type': type(exc).__name__, 'message': str(exc)}
                        )
                    if isinstance(exc, TokenBudgetExceeded):
                        raise
                    if isinstance(exc, Exception) and not budget_bounded(call):
                        raise RunStopped(
                            'model_usage_unknown',
                            'Local embedding was interrupted; usage remains reserved',
                        ) from exc
                    raise
            self.recorder.event(
                'embedding.cache',
                inputs=len(texts),
                misses=len(pending),
                identity=self.identity,
            )
            self.guard()
            return [list(self.cache[k]) for k in keys]


def local_bge(
    recorder, model_path, revision, guard, lock, *, device='cpu', precision='float32'
):
    """Dense CLS pooling with HF local_files_only, per BAAI BGE-M3 model card.

    Optional torch/transformers dependencies are loaded only on explicit live use.
    This backend is not exercised by fixed-vector offline tests.
    """
    root = Path(model_path).resolve()
    if not root.is_dir() or not revision:
        raise ValueError('An existing local BGE-M3 directory and revision are required')
    relevant = sorted(
        p
        for p in root.rglob('*')
        if p.is_file() and not any(part.startswith('.') for part in p.relative_to(root).parts)
    )
    if not relevant:
        raise ValueError('Empty BGE model directory')
    files = {}
    for path in relevant:
        sha = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                sha.update(chunk)
        files[path.relative_to(root).as_posix()] = sha.hexdigest()
    if device not in ('cpu', 'cuda:0') or precision not in ('float32', 'float16'):
        raise ValueError('Unsupported embedding device or precision')
    if device == 'cpu' and precision != 'float32':
        raise ValueError('CPU embeddings retain float32; no silent precision fallback')
    identity = {
        'model': 'BAAI/bge-m3',
        'revision': revision,
        'local_files': files,
        'pooling': 'cls-normalized',
        'precision': precision,
        'device': device,
    }
    guard()
    import torch

    if device.startswith('cuda') and not torch.cuda.is_available():
        raise RunStopped(
            'operator_stop',
            'Configured embedding GPU is unavailable; preserve the checkpoint without CPU fallback',
        )
    torch.set_num_threads(2)
    identity['cpu_threads'] = 2
    import transformers
    from transformers import AutoModel, AutoTokenizer

    dtype = torch.float32 if precision == 'float32' else torch.float16
    load_options = {}
    if device.startswith('cuda'):
        # Place parameters directly on the configured GPU through meta loading;
        # do not first materialize a second full float32 model in host RAM.
        load_options = {
            'device_map': device,
            'low_cpu_mem_usage': True,
            'dtype': dtype,
            'attn_implementation': 'sdpa',
        }
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        identity.update(
            torch_version=torch.__version__,
            transformers_version=transformers.__version__,
            cuda_runtime=torch.version.cuda,
            gpu_name=torch.cuda.get_device_name(0),
            allow_tf32=False,
            attention='sdpa',
        )
    tokenizer = AutoTokenizer.from_pretrained(str(root), local_files_only=True)
    model = AutoModel.from_pretrained(str(root), local_files_only=True, **load_options).eval()
    if device == 'cpu':
        model = model.float()
    if model.config.hidden_size != 1024 or model.config.model_type != 'xlm-roberta':
        raise ValueError('Expected the BGE-M3 XLM-RoBERTa dense model (1024 dims)')

    def count(text):
        return len(tokenizer.encode(text, add_special_tokens=True, truncation=False))

    def encode(texts):
        values = []
        for offset in range(0, len(texts), 8):
            batch = tokenizer(
                texts[offset : offset + 8], padding=True, truncation=False, return_tensors='pt'
            )
            batch = {name: tensor.to(device) for name, tensor in batch.items()}
            with torch.inference_mode():
                vectors = model(**batch).last_hidden_state[:, 0]
                values.extend(
                    torch.nn.functional.normalize(vectors.float(), p=2, dim=1).cpu().tolist()
                )
        return values

    return MeteredDense(recorder, identity, encode, count, guard, lock, dimensions=1024)
