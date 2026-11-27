# LiteLLM — Unified LLM & Embedding Reference

Technical guide for `litellm>=1.50.0` usage in DDLC Agent Studio specialists, RAG embeddings, and generated agents.

---

## 1. Provider & Model String Conventions

All model assignments are externalized in `config/model_tiers.yaml` (Layer 1) or `impl_docs/config.yaml` (Layer 2) using LiteLLM `<provider>/<model>` identifiers:

| Tier | Config Key | Current Studio Model (`config/model_tiers.yaml`) | Use Case |
|---|---|---|---|
| **Heavy** | `tiers.heavy` | `gemini/gemini-2.5-pro` | Gap detection, architecture design, change impact |
| **Standard** | `tiers.standard` | `anthropic/claude-3-5-sonnet-20241022` | Feasibility research, impl docs, Leaf Coder |
| **Fast** | `tiers.fast` | `anthropic/claude-3-5-haiku-20241022` | Mechanical checks, validation summaries |
| **Embedding** | `tiers.embedding` | `text-embedding-3-small` (`dimensions: 1536`) | RAG corpus & `component_registry` vectors |

- **API Keys**: Resolved solely via environment variables (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`). Never pass hardcoded API keys to LiteLLM calls.
- **Air-Gapped / Local Pricing**: Set `LITELLM_LOCAL_MODEL_COST_MAP=True` in restricted environments so LiteLLM loads its bundled offline model cost table without making outbound GitHub fetches at import time.

---

## 2. Synchronous Completion & Structured Output Binding

In generated LangGraph and `python_litellm` agents, `litellm.completion` is wrapped by Instructor (`instructor.from_litellm`) rather than calling raw unvalidated chat strings:

```python
import instructor
import litellm

client = instructor.from_litellm(litellm.completion)

plan = client.chat.completions.create(
    model=settings.llm.model_tiers.reasoning,
    response_model=TransformationPlan,
    messages=[
        {"role": "system", "content": "You are an expert analytics engineer."},
        {"role": "user", "content": prompt_content},
    ],
    temperature=0.1,
)
```

For async pipelines (`python_litellm` async workers or Meta-Agent specialists), bind `instructor.from_litellm(litellm.acompletion)`.

---

## 3. Async Batch Embeddings (`litellm.aembedding`)

`LiteLLMEmbedder` (`src/rag/embeddings/embedder.py`) generates 1536-dimensional embeddings in configurable batches (`rag.embedding.batch_size: 64`) with `tenacity` backoff:

```python
from typing import List
import litellm
from tenacity import retry, stop_after_attempt, wait_exponential

class LiteLLMEmbedder:
    def __init__(self, model_name: str = "text-embedding-3-small", dimensions: int = 1536):
        self.model_name = model_name
        self.dimensions = dimensions

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        response = await litellm.aembedding(
            model=self.model_name,
            input=texts,
            dimensions=self.dimensions,
        )
        return [item["embedding"] for item in response.data]
```

> [!IMPORTANT]
> **Two-Phase Commit Rule (`rag.md` §9.1)**: Never call `litellm.aembedding()` or `litellm.completion()` inside an open PostgreSQL database transaction (`with conn.transaction():`). Generate all embeddings asynchronously first, then open the atomic DB transaction solely for SQL writes.

---

## 4. Error Handling, Retries & Telemetry Normalization

### Transient vs. Fatal LiteLLM Exceptions
Catch and retry **only** transient exceptions via `tenacity`:
- **Retryable**: `litellm.RateLimitError`, `litellm.Timeout`, `litellm.APIConnectionError`, `litellm.ServiceUnavailableError`, `litellm.InternalServerError`.
- **Fail Fast (Do Not Retry)**: `litellm.AuthenticationError`, `litellm.BadRequestError`, `litellm.PermissionDeniedError`.

```python
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

@retry(
    retry=retry_if_exception_type((litellm.RateLimitError, litellm.Timeout, litellm.APIConnectionError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
def call_with_backoff(...):
    ...
```

### Usage Telemetry Fields
`extract_llm_telemetry()` (`src/metadata/callbacks.py`) normalizes provider-specific token keys across LiteLLM responses (`prompt_tokens`/`input_tokens`, `completion_tokens`/`output_tokens`, `cache_read_input_tokens`/`cached_tokens`, and `reasoning_tokens`/`thoughts_token_count`) and splits `<provider>/<model>` strings into `model_provider` and `model_used` for `agent_metadata.execution_log`.
