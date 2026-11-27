# Component Guide: `git_deliverer_v1`

Implementation guide for the Tier 2 shared delivery component (`git_deliverer_v1`), responsible for Git branch publication, Pull Request creation, and transactional Tier 2 lineage registration.

---

## 1. Component Identity & Architectural Role

| Attribute | Value |
|---|---|
| **Component ID** | `git_deliverer_v1` (`version: "1.0.0"` / `"2.1.0"`) |
| **Declaration in `agent.yaml`** | `dependencies.shared_components[]` (`usage_type: "import"`) |
| **Declaration in `stages/stage_deliver.yaml`** | `processing.tools[]` (`type: shared_component`, `access: side_effecting`) |
| **Execution Node** | Deterministic Delivery Node (`node_deliver_stage` in `src/graph.py`) |
| **Error Semantics (Decision 30)** | **Fail-Fast Transactional** — raises `RuntimeError` immediately on Git or DB errors; never swallowed in callbacks |

---

## 2. Mandatory Human Gate Prerequisite

`git_deliverer_v1` performs irreversible external mutations (pushing remote Git branches, opening Pull Requests, and writing Tier 2 metadata). Per `PAT-01` (`01-staged-gated-pipeline.md`) and Decision 30:

1. `node_deliver_stage` **must** assert `state.get("human_approved", False)` is `True` before invoking any `git_deliverer_v1` function.
2. Validate both the human sign-off contract (`ReviewedArtifactBundle`) and the Claim Check pointer (`DbtArtifactBundle`) so only frozen, checksum-verified files (`artifact_sha256`) are published.

---

## 3. Python Interface Contract (`src/tools/git_deliverer.py`)

Generated agents using the `langgraph` strategy wire two deterministic callables inside `node_deliver_stage` (`builder.md` §4.1):

### 3.1 `publish_dbt_pull_request`
```python
from typing import List, Tuple

def publish_dbt_pull_request(
    repo_uri: str,
    branch_name: str,
    source_dir: str,
    files: List[str],
    commit_message: str,
) -> Tuple[str, str]:
    """Create Git branch, commit staged artifacts, push, and open a Pull Request.

    Returns:
        Tuple of (commit_sha, pull_request_url).
    """
```

**Implementation Requirements**:
- **Branch Naming**: Enforce `feat/<scope>-<run_id[:8]>` (e.g., `f"feat/dbt-models-{state['run_id'][:8]}"`).
- **Path Containment**: Validate every entry in `files` resolves cleanly under `Path(source_dir).resolve()` via `is_relative_to()` before copying into the cloned working tree.
- **Credential Externalization**: Authenticate pushes and PR API requests using `GITHUB_TOKEN` (from `AgentSettings` / env). Never embed tokens in `repo_uri` logs or `DeliveryManifest`.
- **PR Summary Body**: Include model inventory, dbt test checklist, `bundle_id`, `artifact_sha256`, and `run_id` traceability.

### 3.2 `register_pipeline_delivery`
```python
def register_pipeline_delivery(
    metadata_db_uri: str,
    run_id: str,
    bundle: DbtArtifactBundle,
    commit_sha: str,
    pr_url: str,
) -> None:
    """Transactionally register delivered contracts and lineage in ddlc_shared_metadata."""
```

**Tier 2 Metadata Writes**:
Uses `CatalogClient` (`src/metadata/catalog.py`) and `LineageClient` (`src/metadata/lineage.py`) over `MetadataDatabase(db_uri=metadata_db_uri)` to atomically persist:
- Contract definitions in `ddlc_shared_metadata.contract_registry` (`register_contract`)
- Producer -> consumer lineage in `ddlc_shared_metadata.contract_lineage` (`record_contract_lineage`) and `agent_dependency` (`record_agent_dependency`)
- Shared component consumption in `ddlc_shared_metadata.component_usage` (`record_component_usage` with `usage_type="import"`)

---

## 4. `node_deliver_stage` Invocation & `DeliveryManifest` Output

```python
def node_deliver_stage(state: AgentState) -> Dict[str, Any]:
    settings = get_settings()
    if not state.get("human_approved", False):
        raise RuntimeError("Delivery attempted without explicit human review approval.")

    reviewed_bundle = ReviewedArtifactBundle.model_validate(
        state["contracts"]["reviewed_artifact_bundle"]
    )
    bundle = DbtArtifactBundle.model_validate(state["contracts"]["dbt_artifact_bundle"])

    try:
        branch_name = f"feat/dbt-models-{state['run_id'][:8]}"
        commit_sha, pr_url = publish_dbt_pull_request(
            repo_uri=settings.destinations.git.repository_url,
            branch_name=branch_name,
            source_dir=bundle.storage_uri,
            files=bundle.file_manifest,
            commit_message=f"feat(dbt): automated dbt pipeline models [run_id: {state['run_id']}]",
        )
        register_pipeline_delivery(
            metadata_db_uri=settings.METADATA_DB_URI,
            run_id=state["run_id"],
            bundle=bundle,
            commit_sha=commit_sha,
            pr_url=pr_url,
        )
        manifest = DeliveryManifest(
            manifest_id=str(uuid.uuid4()),
            run_id=state["run_id"],
            repository=settings.destinations.git.repository_url,
            branch_name=branch_name,
            commit_sha=commit_sha,
            pull_request_url=pr_url,
            artifact_sha256=bundle.artifact_sha256,
            delivered_at=datetime.now(timezone.utc),
        )
        return {
            "contracts": {**state["contracts"], "delivery_manifest": manifest.model_dump()},
            "diagnostics": [],
        }
    except Exception as e:
        raise RuntimeError(f"Phase 8 Delivery Failed: {str(e)}") from e
```

### Configuration & Testing
- **Config Keys (`config.yaml`)**: `destinations.git.repository_url`, `destinations.git.target_branch`, `destinations.git.base_branch`, plus secret `github_token`.
- **Unit Testing**: Mock `publish_dbt_pull_request` and `register_pipeline_delivery` (or inject `_FakePool` into `MetadataDatabase`) to assert that `node_deliver_stage` raises `RuntimeError` when `human_approved=False` and emits a valid `DeliveryManifest` when approved.
