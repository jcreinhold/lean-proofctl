"""Tests for local retrieval over source text and artifact store entries."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from kan_prover.engine.cache import append_jsonl, ensure_store, store_paths
from kan_prover.engine.config import bind_workspace, discover_tool_paths
from kan_prover.engine.lean_helpers import (
    ProbePoint,
    ProbeSpan,
    ProofctlDeclarationInventory,
    ProofctlDeclarationMetadata,
    ProofctlGroupMembership,
    ProofctlWorkspaceDeclarationIndex,
    ProofctlWorkspaceDeclarationIndexEntry,
)
from kan_prover.engine.proofctl import ProbeLayer, ProbeResult, ProbeStatus
from kan_prover.engine.retrieval import EmbeddingRetrievalConfig, collect_retrieval_explanation, collect_retrieval_hits
from kan_prover.engine.semantic_cache import clear_memory_cache
from kan_prover.engine.workspace_declaration_index import clear_workspace_index_memory_cache


class DeterministicEmbeddingRetriever:
    """Deterministic test double for embedding-based retrieval scoring."""

    def __init__(self) -> None:
        self.calls = 0

    def provider_name(self) -> str:
        return "test-double"

    def model_name(self) -> str | None:
        return "deterministic"

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        vectors: list[list[float]] = []
        for text in texts:
            if (text.startswith("Theorem:") and "normalize_collection" in text) or "shape_preserving_transform" in text:
                vectors.append([1.0, 0.0])
            elif "normalize_helper" in text:
                vectors.append([0.0, 1.0])
            else:
                vectors.append([0.2, 0.2])
        return vectors


class TestRetrieval:
    """Regression tests for same-file, namespace, and prior-artifact retrieval."""

    @classmethod
    def setup_class(cls) -> None:
        cls.tool_paths = discover_tool_paths()
        cls.repo_root = cls.tool_paths.root.parent.parent

    def test_retrieval_surfaces_same_file_and_same_namespace(self) -> None:
        fixture = (Path(__file__).resolve().parent / "fixtures" / "retrieval_namespace.lean").read_text(
            encoding="utf-8"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            target_file = temp_root / "retrieval_namespace.lean"
            target_file.write_text(fixture, encoding="utf-8")
            store = store_paths(temp_root / "artifacts")
            ensure_store(store)
            hits, _helpers = collect_retrieval_hits(
                workspace_root=temp_root,
                target_module_path=Path("retrieval_namespace.lean"),
                target_file=target_file,
                theorem="target",
                source_text=fixture,
                store=store,
                failure_classes=["unknown-name"],
            )
        kinds = {hit.source_kind.value for hit in hits}
        titles = {hit.title for hit in hits}
        assert "same-file" in kinds
        assert "same-namespace" in kinds
        assert "helper_one" in titles
        assert "global_helper" in titles

    def test_retrieval_surfaces_prior_local_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            store = store_paths(temp_root / "artifacts")
            ensure_store(store)
            target_file = temp_root / "Target.lean"
            target_file.write_text("theorem target : True := by\n  exact True.intro\n", encoding="utf-8")
            append_jsonl(
                store.accepted_patches,
                {
                    "run_id": "run-1",
                    "workspace_root": str(temp_root.resolve()),
                    "target_module_path": "Target.lean",
                    "theorem": "target",
                    "body": "by\n  exact True.intro\n",
                    "candidate_body_path": str(temp_root / "candidate.body.lean"),
                },
            )
            append_jsonl(
                store.proved_helpers,
                {
                    "run_id": "run-2",
                    "workspace_root": str(temp_root.resolve()),
                    "target_module_path": "Target.lean",
                    "theorem": "target",
                    "helper_name": "helper_truth",
                    "module_name": "KanProverGenerated.Helpers.Target.HelperTruth",
                    "file_path": str(temp_root / "helper_truth.lean"),
                    "statement": "theorem helper_truth : True :=",
                    "helper_module_root": "KanProverGenerated.Helpers",
                },
            )
            append_jsonl(
                store.failure_summaries,
                {
                    "run_id": "run-3",
                    "workspace_root": str(temp_root.resolve()),
                    "target_module_path": "Target.lean",
                    "theorem": "target",
                    "failure_classes": ["unknown-name"],
                    "summary": "unknown identifier helper_truth",
                },
            )
            hits, helpers = collect_retrieval_hits(
                workspace_root=temp_root,
                target_module_path=Path("Target.lean"),
                target_file=target_file,
                theorem="target",
                source_text=target_file.read_text(encoding="utf-8"),
                store=store,
                failure_classes=["unknown-name"],
            )
        kinds = {hit.source_kind.value for hit in hits}
        assert "accepted-patch" in kinds
        assert "proved-helper" in kinds
        assert "failure-summary" in kinds
        assert len(helpers) == 1
        assert helpers[0].helper_name == "helper_truth"

    def test_same_namespace_retrieval_scans_bound_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            problems = temp_root / "Problems"
            problems.mkdir(parents=True, exist_ok=True)
            target_file = problems / "Target.lean"
            helper_file = problems / "Support.lean"
            target_file.write_text(
                "namespace Demo\n\ntheorem helper_target : True := by\n  exact namespace_helper\n\nend Demo\n",
                encoding="utf-8",
            )
            helper_file.write_text(
                "namespace Demo\n\ntheorem namespace_helper : True := by\n  exact True.intro\n\nend Demo\n",
                encoding="utf-8",
            )
            store = store_paths(temp_root / "artifacts")
            ensure_store(store)
            hits, _helpers = collect_retrieval_hits(
                workspace_root=temp_root,
                target_module_path=Path("Problems/Target.lean"),
                target_file=target_file,
                theorem="helper_target",
                source_text=target_file.read_text(encoding="utf-8"),
                store=store,
                failure_classes=["unknown-name"],
            )
        namespace_hits = [hit for hit in hits if hit.source_kind.value == "same-namespace"]
        assert any(hit.title == "namespace_helper" for hit in namespace_hits)
        assert any(hit.path == "Problems/Support.lean" for hit in namespace_hits)

    def test_retrieval_uses_lean_inventory_for_group_boundary_sensitive_hits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            target_file = temp_root / "Grouped.lean"
            source = (
                "mutual\n\n"
                "private theorem target : True := by\n"
                "  exact helper_one\n\n"
                "private theorem helper_one : True := by\n"
                "  exact True.intro\n\n"
                "end\n\n"
                "theorem after_group : True := by\n"
                "  exact True.intro\n"
            )
            target_file.write_text(source, encoding="utf-8")
            store = store_paths(temp_root / "artifacts")
            ensure_store(store)
            inventory = ProofctlDeclarationInventory(
                entries=[
                    self._declaration_metadata(
                        short_name="target",
                        declaration_name="target",
                        start_line=3,
                        end_line=6,
                        body_start_line=3,
                        body_start_column=31,
                        member_index=1,
                    ),
                    self._declaration_metadata(
                        short_name="helper_one",
                        declaration_name="helper_one",
                        start_line=6,
                        end_line=9,
                        body_start_line=6,
                        body_start_column=35,
                        member_index=2,
                    ),
                    self._declaration_metadata(
                        short_name="after_group",
                        declaration_name="after_group",
                        start_line=11,
                        end_line=13,
                        body_start_line=11,
                        body_start_column=36,
                        member_index=None,
                        group_kind="standalone",
                        group_id=None,
                    ),
                ]
            )
            with patch(
                "kan_prover.engine.retrieval.declaration_inventory",
                return_value=ProbeResult(
                    layer=ProbeLayer.SEMANTIC,
                    command="declaration-inventory",
                    status=ProbeStatus.OK,
                    file=str(target_file),
                    theorem="*",
                    result=inventory,
                ),
            ):
                hits, _helpers = collect_retrieval_hits(
                    workspace_root=temp_root,
                    target_module_path=Path("Grouped.lean"),
                    target_file=target_file,
                    theorem="target",
                    source_text=source,
                    store=store,
                    failure_classes=[],
                    tool_paths=self.tool_paths,
                    workspace=bind_workspace(self.repo_root),
                )
        helper_hit = next(hit for hit in hits if hit.title == "helper_one")
        assert "private theorem helper_one" in helper_hit.content
        assert "\nend\n" not in helper_hit.content

    def test_retrieval_reuses_persistent_declaration_inventory_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            cache_root = temp_root / "semantic-cache"
            target_file = temp_root / "Grouped.lean"
            target_file.write_text(
                "theorem target : True := by\n  exact helper_one\n\n"
                "theorem helper_one : True := by\n  exact True.intro\n",
                encoding="utf-8",
            )
            (temp_root / "lakefile.toml").write_text('name = "Demo"\n', encoding="utf-8")
            (temp_root / "lake-manifest.json").write_text('{"packages":[]}', encoding="utf-8")
            (temp_root / "lean-toolchain").write_text("leanprover/lean4:stable\n", encoding="utf-8")
            workspace = bind_workspace(temp_root)
            store = store_paths(temp_root / "artifacts")
            ensure_store(store)
            inventory = ProofctlDeclarationInventory(
                entries=[
                    self._declaration_metadata(
                        short_name="target",
                        declaration_name="target",
                        start_line=1,
                        end_line=4,
                        body_start_line=1,
                        body_start_column=23,
                        member_index=None,
                        group_kind="standalone",
                        group_id=None,
                    ),
                    self._declaration_metadata(
                        short_name="helper_one",
                        declaration_name="helper_one",
                        start_line=4,
                        end_line=6,
                        body_start_line=4,
                        body_start_column=27,
                        member_index=None,
                        group_kind="standalone",
                        group_id=None,
                    ),
                ]
            )
            probe = ProbeResult(
                layer=ProbeLayer.SEMANTIC,
                command="declaration-inventory",
                status=ProbeStatus.OK,
                file=str(target_file),
                theorem="*",
                result=inventory,
            )

            clear_memory_cache()
            with (
                patch.dict("os.environ", {"KAN_PROVER_SEMANTIC_CACHE_ROOT": str(cache_root)}),
                patch("kan_prover.engine.proofctl.run_lean_helper", return_value=probe) as runner,
            ):
                first_hits, _helpers = collect_retrieval_hits(
                    workspace_root=temp_root,
                    target_module_path=Path("Grouped.lean"),
                    target_file=target_file,
                    theorem="target",
                    source_text=target_file.read_text(encoding="utf-8"),
                    store=store,
                    failure_classes=[],
                    tool_paths=self.tool_paths,
                    workspace=workspace,
                )
            assert runner.call_count == 1

            clear_memory_cache()
            with (
                patch.dict("os.environ", {"KAN_PROVER_SEMANTIC_CACHE_ROOT": str(cache_root)}),
                patch(
                    "kan_prover.engine.proofctl.run_lean_helper",
                    side_effect=AssertionError("warm retrieval should use the persistent cache"),
                ),
            ):
                second_hits, _helpers = collect_retrieval_hits(
                    workspace_root=temp_root,
                    target_module_path=Path("Grouped.lean"),
                    target_file=target_file,
                    theorem="target",
                    source_text=target_file.read_text(encoding="utf-8"),
                    store=store,
                    failure_classes=[],
                    tool_paths=self.tool_paths,
                    workspace=workspace,
                )
        assert [hit.title for hit in first_hits] == [hit.title for hit in second_hits]

    def test_workspace_retrieval_ignores_checked_in_artifact_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            problems = temp_root / "Problems"
            problems.mkdir(parents=True, exist_ok=True)
            artifact_dir = temp_root / "tools" / "kan-prover" / "artifacts" / "runs" / "run-001"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            target_file = problems / "Target.lean"
            target_file.write_text(
                "namespace Demo\n\ntheorem helper_target : True := by\n  exact namespace_helper\n\nend Demo\n",
                encoding="utf-8",
            )
            (problems / "Support.lean").write_text(
                "namespace Demo\n\ntheorem namespace_helper : True := by\n  exact True.intro\n\nend Demo\n",
                encoding="utf-8",
            )
            (artifact_dir / "Artifact.lean").write_text(
                "namespace Demo\n\ntheorem artifact_helper : True := by\n  exact True.intro\n\nend Demo\n",
                encoding="utf-8",
            )
            store = store_paths(temp_root / "artifacts")
            ensure_store(store)
            fake_tool_paths = self.tool_paths.__class__(
                root=temp_root / "tools" / "kan-prover",
                prompts=self.tool_paths.prompts,
                artifacts=self.tool_paths.artifacts,
                artifact_runs=self.tool_paths.artifact_runs,
                artifact_index=self.tool_paths.artifact_index,
                artifact_sessions=self.tool_paths.artifact_sessions,
                bundled_demo_runtime=self.tool_paths.bundled_demo_runtime,
                packaged_probe_sources=self.tool_paths.packaged_probe_sources,
            )
            hits, _helpers = collect_retrieval_hits(
                workspace_root=temp_root,
                target_module_path=Path("Problems/Target.lean"),
                target_file=target_file,
                theorem="helper_target",
                source_text=target_file.read_text(encoding="utf-8"),
                store=store,
                failure_classes=["unknown-name"],
                tool_paths=fake_tool_paths,
            )
        titles = {hit.title for hit in hits}
        assert "namespace_helper" in titles
        assert "artifact_helper" not in titles

    def test_goal_retrieval_surfaces_imported_declarations_with_auditable_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            demo_dir = temp_root / "Demo"
            demo_dir.mkdir(parents=True, exist_ok=True)
            target_file = demo_dir / "Target.lean"
            support_file = demo_dir / "Support.lean"
            target_file.write_text(
                (
                    "import Demo.Support\n\n"
                    "namespace Demo\n\n"
                    "theorem pair_goal : True /\\ True := by\n"
                    "  sorry\n\n"
                    "end Demo\n"
                ),
                encoding="utf-8",
            )
            support_file.write_text(
                (
                    "namespace HelperSpace\n\n"
                    "theorem pair_intro_true : True /\\ True := by\n"
                    "  exact And.intro True.intro True.intro\n\n"
                    "end HelperSpace\n"
                ),
                encoding="utf-8",
            )
            store = store_paths(temp_root / "artifacts")
            ensure_store(store)

            hits, _helpers = collect_retrieval_hits(
                workspace_root=temp_root,
                target_module_path=Path("Demo/Target.lean"),
                target_file=target_file,
                theorem="pair_goal",
                source_text=target_file.read_text(encoding="utf-8"),
                store=store,
                failure_classes=[],
                theorem_statement="theorem pair_goal : True /\\ True :=",
                goal_snapshot=["case main", "⊢ True /\\ True"],
                imports=["Demo.Support"],
                namespace="Demo",
            )

        goal_hit = next(hit for hit in hits if hit.source_kind.value == "goal-declaration")
        assert goal_hit.title == "pair_intro_true"
        assert goal_hit.path == "Demo/Support.lean"
        labels = {component.label for component in goal_hit.score_breakdown}
        assert "import-match" in labels
        assert "type-head-overlap" in labels
        assert goal_hit.match_reasons

    def test_goal_retrieval_expands_to_import_module_neighborhood(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            target_dir = temp_root / "Demo" / "Nested"
            support_dir = temp_root / "Demo" / "Support"
            target_dir.mkdir(parents=True, exist_ok=True)
            support_dir.mkdir(parents=True, exist_ok=True)
            target_file = target_dir / "Target.lean"
            target_file.write_text(
                (
                    "import Demo.Support.Base\n\n"
                    "namespace Demo.Nested\n\n"
                    "theorem pair_goal : True /\\ True := by\n"
                    "  sorry\n\n"
                    "end Demo.Nested\n"
                ),
                encoding="utf-8",
            )
            (support_dir / "Base.lean").write_text(
                ("namespace Demo.Support\n\ntheorem base_truth : True := by\n  exact True.intro\n\nend Demo.Support\n"),
                encoding="utf-8",
            )
            (support_dir / "Extra.lean").write_text(
                (
                    "namespace Demo.Support\n\n"
                    "theorem pair_intro_true : True /\\ True := by\n"
                    "  exact And.intro True.intro True.intro\n\n"
                    "end Demo.Support\n"
                ),
                encoding="utf-8",
            )
            store = store_paths(temp_root / "artifacts")
            ensure_store(store)

            hits, _helpers = collect_retrieval_hits(
                workspace_root=temp_root,
                target_module_path=Path("Demo/Nested/Target.lean"),
                target_file=target_file,
                theorem="pair_goal",
                source_text=target_file.read_text(encoding="utf-8"),
                store=store,
                failure_classes=[],
                theorem_statement="theorem pair_goal : True /\\ True :=",
                goal_snapshot=["case main", "⊢ True /\\ True"],
                imports=["Demo.Support.Base"],
                namespace="Demo.Nested",
            )

        imported_neighborhood_hit = next(hit for hit in hits if hit.title == "pair_intro_true")
        assert imported_neighborhood_hit.source_kind.value == "goal-declaration"
        assert imported_neighborhood_hit.path == "Demo/Support/Extra.lean"
        assert imported_neighborhood_hit.match_reasons

    def test_broadened_frontier_uses_workspace_index_on_normal_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            demo_root = temp_root / "Demo"
            nested_root = demo_root / "Nested"
            support_root = demo_root / "Support"
            nested_root.mkdir(parents=True, exist_ok=True)
            support_root.mkdir(parents=True, exist_ok=True)
            target_file = nested_root / "Target.lean"
            base_file = support_root / "Base.lean"
            extra_file = support_root / "Extra.lean"
            target_source = (
                "import Demo.Support.Base\n\n"
                "namespace Demo.Nested\n\n"
                "theorem pair_goal : True /\\ True := by\n"
                "  exact pair_intro_true\n\n"
                "end Demo.Nested\n"
            )
            target_file.write_text(target_source, encoding="utf-8")
            base_file.write_text(
                ("namespace Demo.Support\n\ntheorem base_truth : True := by\n  exact True.intro\n\nend Demo.Support\n"),
                encoding="utf-8",
            )
            extra_file.write_text(
                (
                    "namespace Demo.Support\n\n"
                    "theorem pair_intro_true : True /\\ True := by\n"
                    "  exact And.intro True.intro True.intro\n\n"
                    "end Demo.Support\n"
                ),
                encoding="utf-8",
            )
            (temp_root / "lakefile.toml").write_text('name = "Demo"\n', encoding="utf-8")
            (temp_root / "lake-manifest.json").write_text('{"packages":[]}', encoding="utf-8")
            (temp_root / "lean-toolchain").write_text("leanprover/lean4:stable\n", encoding="utf-8")
            workspace = bind_workspace(temp_root)
            store = store_paths(temp_root / "artifacts")
            ensure_store(store)
            target_inventory = ProofctlDeclarationInventory(
                entries=[
                    self._declaration_metadata(
                        short_name="pair_goal",
                        declaration_name="Demo.Nested.pair_goal",
                        start_line=5,
                        end_line=8,
                        body_start_line=5,
                        body_start_column=35,
                        member_index=None,
                        group_kind="standalone",
                        group_id=None,
                    )
                ]
            )
            workspace_index = ProofctlWorkspaceDeclarationIndex(
                entries=[
                    ProofctlWorkspaceDeclarationIndexEntry(
                        file_path=str(base_file),
                        module_path="Demo.Support.Base",
                        declaration_name="Demo.Support.base_truth",
                        short_name="base_truth",
                        namespace="Demo.Support",
                        declaration_kind="theorem",
                        declaration_span=ProbeSpan(start=ProbePoint(3, 0), end=ProbePoint(6, 0)),
                        type_text="True",
                    ),
                    ProofctlWorkspaceDeclarationIndexEntry(
                        file_path=str(extra_file),
                        module_path="Demo.Support.Extra",
                        declaration_name="Demo.Support.pair_intro_true",
                        short_name="pair_intro_true",
                        namespace="Demo.Support",
                        declaration_kind="theorem",
                        declaration_span=ProbeSpan(start=ProbePoint(3, 0), end=ProbePoint(6, 0)),
                        type_text="True /\\ True",
                    ),
                ]
            )
            clear_memory_cache()
            clear_workspace_index_memory_cache()
            with (
                patch(
                    "kan_prover.engine.retrieval.declaration_inventory",
                    return_value=ProbeResult(
                        layer=ProbeLayer.SEMANTIC,
                        command="declaration-inventory",
                        status=ProbeStatus.OK,
                        file=str(target_file),
                        theorem="*",
                        result=target_inventory,
                    ),
                ) as target_runner,
                patch(
                    "kan_prover.engine.workspace_declaration_index.workspace_declaration_index",
                    return_value=ProbeResult(
                        layer=ProbeLayer.SEMANTIC,
                        command="workspace-declaration-index",
                        status=ProbeStatus.OK,
                        file=str(target_file),
                        theorem="*",
                        result=workspace_index,
                    ),
                ) as index_runner,
            ):
                hits, _helpers = collect_retrieval_hits(
                    workspace_root=temp_root,
                    target_module_path=Path("Demo/Nested/Target.lean"),
                    target_file=target_file,
                    theorem="pair_goal",
                    source_text=target_source,
                    store=store,
                    failure_classes=[],
                    theorem_statement="theorem pair_goal : True /\\ True :=",
                    goal_snapshot=["case main", "⊢ True /\\ True"],
                    imports=["Demo.Support.Base"],
                    namespace="Demo.Nested",
                    tool_paths=self.tool_paths,
                    workspace=workspace,
                )
        assert target_runner.call_count == 1
        assert index_runner.call_count == 1
        goal_hit = next(hit for hit in hits if hit.title == "pair_intro_true")
        assert goal_hit.source_kind.value == "goal-declaration"
        assert goal_hit.path == "Demo/Support/Extra.lean"
        assert goal_hit.match_reasons
        assert "theorem pair_intro_true : True /\\ True := by" in goal_hit.content

    def test_workspace_index_cache_invalidates_on_frontier_source_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            cache_root = temp_root / "semantic-cache"
            demo_root = temp_root / "Demo"
            nested_root = demo_root / "Nested"
            support_root = demo_root / "Support"
            nested_root.mkdir(parents=True, exist_ok=True)
            support_root.mkdir(parents=True, exist_ok=True)
            target_file = nested_root / "Target.lean"
            base_file = support_root / "Base.lean"
            extra_file = support_root / "Extra.lean"
            target_file.write_text(
                (
                    "import Demo.Support.Base\n\n"
                    "namespace Demo.Nested\n\n"
                    "theorem pair_goal : True /\\ True := by\n"
                    "  exact pair_intro_true\n\n"
                    "end Demo.Nested\n"
                ),
                encoding="utf-8",
            )
            base_file.write_text(
                ("namespace Demo.Support\n\ntheorem base_truth : True := by\n  exact True.intro\n\nend Demo.Support\n"),
                encoding="utf-8",
            )
            extra_file.write_text(
                (
                    "namespace Demo.Support\n\n"
                    "theorem pair_intro_true : True /\\ True := by\n"
                    "  exact And.intro True.intro True.intro\n\n"
                    "end Demo.Support\n"
                ),
                encoding="utf-8",
            )
            (temp_root / "lakefile.toml").write_text('name = "Demo"\n', encoding="utf-8")
            (temp_root / "lake-manifest.json").write_text('{"packages":[]}', encoding="utf-8")
            (temp_root / "lean-toolchain").write_text("leanprover/lean4:stable\n", encoding="utf-8")
            workspace = bind_workspace(temp_root)
            store = store_paths(temp_root / "artifacts")
            ensure_store(store)
            target_inventory = ProofctlDeclarationInventory(
                entries=[
                    self._declaration_metadata(
                        short_name="pair_goal",
                        declaration_name="Demo.Nested.pair_goal",
                        start_line=5,
                        end_line=8,
                        body_start_line=5,
                        body_start_column=35,
                        member_index=None,
                        group_kind="standalone",
                        group_id=None,
                    )
                ]
            )
            workspace_index = ProofctlWorkspaceDeclarationIndex(
                entries=[
                    ProofctlWorkspaceDeclarationIndexEntry(
                        file_path=str(base_file),
                        module_path="Demo.Support.Base",
                        declaration_name="Demo.Support.base_truth",
                        short_name="base_truth",
                        namespace="Demo.Support",
                        declaration_kind="theorem",
                        declaration_span=ProbeSpan(start=ProbePoint(3, 0), end=ProbePoint(6, 0)),
                        type_text="True",
                    ),
                    ProofctlWorkspaceDeclarationIndexEntry(
                        file_path=str(extra_file),
                        module_path="Demo.Support.Extra",
                        declaration_name="Demo.Support.pair_intro_true",
                        short_name="pair_intro_true",
                        namespace="Demo.Support",
                        declaration_kind="theorem",
                        declaration_span=ProbeSpan(start=ProbePoint(3, 0), end=ProbePoint(6, 0)),
                        type_text="True /\\ True",
                    ),
                ]
            )
            clear_memory_cache()
            clear_workspace_index_memory_cache()
            with (
                patch.dict("os.environ", {"KAN_PROVER_SEMANTIC_CACHE_ROOT": str(cache_root)}),
                patch(
                    "kan_prover.engine.retrieval.declaration_inventory",
                    return_value=ProbeResult(
                        layer=ProbeLayer.SEMANTIC,
                        command="declaration-inventory",
                        status=ProbeStatus.OK,
                        file=str(target_file),
                        theorem="*",
                        result=target_inventory,
                    ),
                ),
                patch(
                    "kan_prover.engine.workspace_declaration_index.workspace_declaration_index",
                    return_value=ProbeResult(
                        layer=ProbeLayer.SEMANTIC,
                        command="workspace-declaration-index",
                        status=ProbeStatus.OK,
                        file=str(target_file),
                        theorem="*",
                        result=workspace_index,
                    ),
                ) as first_runner,
            ):
                collect_retrieval_hits(
                    workspace_root=temp_root,
                    target_module_path=Path("Demo/Nested/Target.lean"),
                    target_file=target_file,
                    theorem="pair_goal",
                    source_text=target_file.read_text(encoding="utf-8"),
                    store=store,
                    failure_classes=[],
                    theorem_statement="theorem pair_goal : True /\\ True :=",
                    goal_snapshot=["case main", "⊢ True /\\ True"],
                    imports=["Demo.Support.Base"],
                    namespace="Demo.Nested",
                    tool_paths=self.tool_paths,
                    workspace=workspace,
                )
            assert first_runner.call_count == 1

            clear_memory_cache()
            clear_workspace_index_memory_cache()
            with (
                patch.dict("os.environ", {"KAN_PROVER_SEMANTIC_CACHE_ROOT": str(cache_root)}),
                patch(
                    "kan_prover.engine.retrieval.declaration_inventory",
                    return_value=ProbeResult(
                        layer=ProbeLayer.SEMANTIC,
                        command="declaration-inventory",
                        status=ProbeStatus.OK,
                        file=str(target_file),
                        theorem="*",
                        result=target_inventory,
                    ),
                ),
                patch(
                    "kan_prover.engine.workspace_declaration_index.workspace_declaration_index",
                    side_effect=AssertionError("warm retrieval should reuse the persisted workspace index"),
                ),
            ):
                collect_retrieval_hits(
                    workspace_root=temp_root,
                    target_module_path=Path("Demo/Nested/Target.lean"),
                    target_file=target_file,
                    theorem="pair_goal",
                    source_text=target_file.read_text(encoding="utf-8"),
                    store=store,
                    failure_classes=[],
                    theorem_statement="theorem pair_goal : True /\\ True :=",
                    goal_snapshot=["case main", "⊢ True /\\ True"],
                    imports=["Demo.Support.Base"],
                    namespace="Demo.Nested",
                    tool_paths=self.tool_paths,
                    workspace=workspace,
                )

            extra_file.write_text(
                (
                    "namespace Demo.Support\n\n"
                    "theorem pair_intro_true : True /\\ True := by\n"
                    "  exact base_truth |> And.intro True.intro\n\n"
                    "end Demo.Support\n"
                ),
                encoding="utf-8",
            )
            clear_memory_cache()
            clear_workspace_index_memory_cache()
            with (
                patch.dict("os.environ", {"KAN_PROVER_SEMANTIC_CACHE_ROOT": str(cache_root)}),
                patch(
                    "kan_prover.engine.retrieval.declaration_inventory",
                    return_value=ProbeResult(
                        layer=ProbeLayer.SEMANTIC,
                        command="declaration-inventory",
                        status=ProbeStatus.OK,
                        file=str(target_file),
                        theorem="*",
                        result=target_inventory,
                    ),
                ),
                patch(
                    "kan_prover.engine.workspace_declaration_index.workspace_declaration_index",
                    return_value=ProbeResult(
                        layer=ProbeLayer.SEMANTIC,
                        command="workspace-declaration-index",
                        status=ProbeStatus.OK,
                        file=str(target_file),
                        theorem="*",
                        result=workspace_index,
                    ),
                ) as second_runner,
            ):
                collect_retrieval_hits(
                    workspace_root=temp_root,
                    target_module_path=Path("Demo/Nested/Target.lean"),
                    target_file=target_file,
                    theorem="pair_goal",
                    source_text=target_file.read_text(encoding="utf-8"),
                    store=store,
                    failure_classes=[],
                    theorem_statement="theorem pair_goal : True /\\ True :=",
                    goal_snapshot=["case main", "⊢ True /\\ True"],
                    imports=["Demo.Support.Base"],
                    namespace="Demo.Nested",
                    tool_paths=self.tool_paths,
                    workspace=workspace,
                )
            assert second_runner.call_count == 1

    def test_broadened_workspace_frontier_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            target_dir = temp_root / "Demo" / "Nested"
            target_dir.mkdir(parents=True, exist_ok=True)
            (temp_root / "Demo" / "Alpha").mkdir(parents=True, exist_ok=True)
            (temp_root / "Demo" / "Support").mkdir(parents=True, exist_ok=True)
            target_file = target_dir / "Target.lean"
            target_file.write_text(
                (
                    "import Demo.Support.Base\n\n"
                    "namespace Demo.Nested\n\n"
                    "theorem pair_goal : True /\\ True := by\n"
                    "  sorry\n\n"
                    "end Demo.Nested\n"
                ),
                encoding="utf-8",
            )
            (temp_root / "Demo" / "Support" / "Base.lean").write_text(
                (
                    "namespace Demo.Support\n\n"
                    "theorem base_pair : True /\\ True := by\n"
                    "  exact And.intro True.intro True.intro\n\n"
                    "end Demo.Support\n"
                ),
                encoding="utf-8",
            )
            (temp_root / "Demo" / "Alpha" / "Extra.lean").write_text(
                (
                    "namespace Demo.Alpha\n\n"
                    "theorem alpha_pair : True /\\ True := by\n"
                    "  exact And.intro True.intro True.intro\n\n"
                    "end Demo.Alpha\n"
                ),
                encoding="utf-8",
            )
            store = store_paths(temp_root / "artifacts")
            ensure_store(store)

            first_hits, _helpers = collect_retrieval_hits(
                workspace_root=temp_root,
                target_module_path=Path("Demo/Nested/Target.lean"),
                target_file=target_file,
                theorem="pair_goal",
                source_text=target_file.read_text(encoding="utf-8"),
                store=store,
                failure_classes=[],
                theorem_statement="theorem pair_goal : True /\\ True :=",
                goal_snapshot=["⊢ True /\\ True"],
                imports=["Demo.Support.Base"],
                namespace="Demo.Nested",
            )
            second_hits, _helpers = collect_retrieval_hits(
                workspace_root=temp_root,
                target_module_path=Path("Demo/Nested/Target.lean"),
                target_file=target_file,
                theorem="pair_goal",
                source_text=target_file.read_text(encoding="utf-8"),
                store=store,
                failure_classes=[],
                theorem_statement="theorem pair_goal : True /\\ True :=",
                goal_snapshot=["⊢ True /\\ True"],
                imports=["Demo.Support.Base"],
                namespace="Demo.Nested",
            )

        assert [(hit.hit_id, hit.title, hit.path) for hit in first_hits] == [
            (hit.hit_id, hit.title, hit.path) for hit in second_hits
        ]

    def test_retrieval_surfaces_verified_fragments_and_graph_states(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            artifacts = temp_root / "artifacts"
            store = store_paths(artifacts)
            ensure_store(store)
            target_file = temp_root / "Target.lean"
            target_file.write_text("theorem target_pair : True /\\ True := by\n  sorry\n", encoding="utf-8")
            fragment_path = temp_root / "fragment.body.lean"
            fragment_path.write_text("by\n  exact And.intro True.intro True.intro\n", encoding="utf-8")
            append_jsonl(
                store.accepted_patches,
                {
                    "run_id": "run-fragment",
                    "workspace_root": str(temp_root.resolve()),
                    "target_module_path": "Support.lean",
                    "theorem": "other_pair",
                    "theorem_statement": "theorem other_pair : True /\\ True :=",
                    "namespace": "Support",
                    "imports": ["Support"],
                    "goal_snapshot": ["⊢ True /\\ True"],
                    "goal_constants": ["True", "And"],
                    "type_heads": ["And"],
                    "body": fragment_path.read_text(encoding="utf-8"),
                    "candidate_body_path": str(fragment_path),
                },
            )

            run_dir = artifacts / "runs" / "run-prove-1"
            session_dir = artifacts / "sessions" / "session-1"
            snapshot_dir = session_dir / "nodes" / "session-node-001-step"
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            (artifacts / "runs").mkdir(parents=True, exist_ok=True)
            (artifacts / "sessions").mkdir(parents=True, exist_ok=True)
            append_jsonl(
                artifacts / "index.jsonl",
                {
                    "run_id": "run-prove-1",
                    "command": "prove",
                    "status": "verified",
                    "workspace_root": str(temp_root.resolve()),
                    "target_module_path": "Support.lean",
                    "theorem": "other_pair",
                    "artifact_dir": str(run_dir),
                },
            )
            (run_dir / "graph").mkdir(parents=True, exist_ok=True)
            (run_dir / "graph" / "summary.json").write_text(
                '{"branch_node_ids": ["prove-node-001-state-search"]}\n',
                encoding="utf-8",
            )
            (run_dir / "graph" / "nodes.json").write_text(
                (
                    f'[{{"node_id": "prove-node-001-state-search", '
                    f'"status": "verified", '
                    f'"session_artifact_dir": "{session_dir}", '
                    f'"session_node_id": "session-node-001-step", '
                    f'"action_payload": {{"kind": "append_tactic_text", '
                    f'"tactic_text": "  exact And.intro True.intro True.intro"}}}}]\n'
                ),
                encoding="utf-8",
            )
            (snapshot_dir / "snapshot.json").write_text(
                (
                    "{"
                    '"theorem_statement": "theorem other_pair : True /\\\\ True :=", '
                    '"imports": ["Support"], '
                    '"namespace": "Support", '
                    '"local_context": ["h : True"], '
                    '"goals": ["⊢ True /\\\\ True"], '
                    '"pretty_proof_state": "h : True\\n⊢ True /\\\\ True"'
                    "}\n"
                ),
                encoding="utf-8",
            )

            hits, _helpers = collect_retrieval_hits(
                workspace_root=temp_root,
                target_module_path=Path("Target.lean"),
                target_file=target_file,
                theorem="target_pair",
                source_text=target_file.read_text(encoding="utf-8"),
                store=store,
                failure_classes=[],
                theorem_statement="theorem target_pair : True /\\ True :=",
                goal_snapshot=["⊢ True /\\ True"],
                imports=["Support"],
                namespace="Target",
            )

        kinds = {hit.source_kind.value for hit in hits}
        assert "proof-fragment" in kinds
        assert "graph-state" in kinds
        assert "tactic-fragment" in kinds
        graph_hit = next(hit for hit in hits if hit.source_kind.value == "graph-state")
        assert graph_hit.source_run_id == "run-prove-1"
        assert graph_hit.match_reasons

    def test_retrieval_falls_back_to_text_scanning_without_helper_workspace(self) -> None:
        fixture = (Path(__file__).resolve().parent / "fixtures" / "retrieval_namespace.lean").read_text(
            encoding="utf-8"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            target_file = temp_root / "retrieval_namespace.lean"
            target_file.write_text(fixture, encoding="utf-8")
            store = store_paths(temp_root / "artifacts")
            ensure_store(store)
            hits, _helpers = collect_retrieval_hits(
                workspace_root=temp_root,
                target_module_path=Path("retrieval_namespace.lean"),
                target_file=target_file,
                theorem="target",
                source_text=fixture,
                store=store,
                failure_classes=[],
            )
        assert any(hit.source_kind.value == "same-file" for hit in hits)

    def test_retrieval_explanation_reports_ranked_hits_and_near_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            target_file = temp_root / "Crowded.lean"
            helpers = "\n".join(
                f"theorem helper_{index:02d} : True := by\n  exact True.intro\n" for index in range(1, 15)
            )
            source = f"namespace Demo\n\n{helpers}\ntheorem target : True := by\n  exact helper_14\n\nend Demo\n"
            target_file.write_text(source, encoding="utf-8")
            store = store_paths(temp_root / "artifacts")
            ensure_store(store)
            explanation = collect_retrieval_explanation(
                workspace_root=temp_root,
                target_module_path=Path("Crowded.lean"),
                target_file=target_file,
                theorem="target",
                source_text=source,
                store=store,
                failure_classes=[],
                hit_limit=12,
                near_miss_limit=2,
            )
        assert len(explanation.hits) == 12
        assert all(hit.score_breakdown for hit in explanation.hits)
        assert all(hit.source_kind.value in {"same-file", "same-namespace"} for hit in explanation.hits)
        returned_scores = [hit.score for hit in explanation.hits]
        assert returned_scores == sorted(returned_scores, reverse=True)
        assert explanation.near_misses
        assert explanation.near_misses[0].source_kind.value in {"same-file", "same-namespace"}
        assert explanation.near_misses[0].omission_reason == "ranked below the returned top 12 hits"
        assert explanation.near_misses[0].score_breakdown

    def test_embedding_disabled_leaves_default_ranking_unchanged(self) -> None:
        fixture = (Path(__file__).resolve().parent / "fixtures" / "retrieval_namespace.lean").read_text(
            encoding="utf-8"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            target_file = temp_root / "retrieval_namespace.lean"
            target_file.write_text(fixture, encoding="utf-8")
            store = store_paths(temp_root / "artifacts")
            ensure_store(store)
            baseline_hits, _helpers = collect_retrieval_hits(
                workspace_root=temp_root,
                target_module_path=Path("retrieval_namespace.lean"),
                target_file=target_file,
                theorem="target",
                source_text=fixture,
                store=store,
                failure_classes=["unknown-name"],
            )
            disabled_hits, _helpers = collect_retrieval_hits(
                workspace_root=temp_root,
                target_module_path=Path("retrieval_namespace.lean"),
                target_file=target_file,
                theorem="target",
                source_text=fixture,
                store=store,
                failure_classes=["unknown-name"],
                embedding_config=EmbeddingRetrievalConfig(enabled=False),
            )
        assert [
            (
                hit.hit_id,
                hit.title,
                hit.score,
                [(component.label, component.points) for component in hit.score_breakdown],
            )
            for hit in baseline_hits
        ] == [
            (
                hit.hit_id,
                hit.title,
                hit.score,
                [(component.label, component.points) for component in hit.score_breakdown],
            )
            for hit in disabled_hits
        ]

    def test_embedding_scoring_adds_score_component_and_can_improve_semantic_rank(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            target_file = temp_root / "Semantic.lean"
            source = (
                "namespace Demo\n\n"
                "theorem normalize_helper (n : Nat) : Nat := by\n"
                "  exact n\n\n"
                "theorem normalize_collection (xs : List Nat) : List Nat := by\n"
                "  exact xs\n\n"
                "theorem shape_preserving_transform (xs : Array Nat) : Array Nat := by\n"
                "  exact xs\n\n"
                "end Demo\n"
            )
            target_file.write_text(source, encoding="utf-8")
            store = store_paths(temp_root / "artifacts")
            ensure_store(store)
            baseline_hits, _helpers = collect_retrieval_hits(
                workspace_root=temp_root,
                target_module_path=Path("Semantic.lean"),
                target_file=target_file,
                theorem="normalize_collection",
                source_text=source,
                store=store,
                failure_classes=[],
                theorem_statement="theorem normalize_collection (xs : List Nat) : List Nat :=",
                goal_snapshot=["⊢ List Nat"],
                namespace="Demo",
            )
            retriever = DeterministicEmbeddingRetriever()
            embedding_hits, _helpers = collect_retrieval_hits(
                workspace_root=temp_root,
                target_module_path=Path("Semantic.lean"),
                target_file=target_file,
                theorem="normalize_collection",
                source_text=source,
                store=store,
                failure_classes=[],
                theorem_statement="theorem normalize_collection (xs : List Nat) : List Nat :=",
                goal_snapshot=["⊢ List Nat"],
                namespace="Demo",
                embedding_config=EmbeddingRetrievalConfig(enabled=True, weight=120),
                embedding_retriever=retriever,
            )
        assert baseline_hits[0].title == "normalize_helper"
        assert embedding_hits[0].title == "shape_preserving_transform"
        assert any(component.label == "embedding-similarity" for component in embedding_hits[0].score_breakdown)

    def test_embedding_cache_reuses_workspace_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            target_file = temp_root / "Semantic.lean"
            source = (
                "namespace Demo\n\n"
                "theorem normalize_helper (n : Nat) : Nat := by\n"
                "  exact n\n\n"
                "theorem normalize_collection (xs : List Nat) : List Nat := by\n"
                "  exact xs\n\n"
                "theorem shape_preserving_transform (xs : Array Nat) : Array Nat := by\n"
                "  exact xs\n\n"
                "end Demo\n"
            )
            target_file.write_text(source, encoding="utf-8")
            store = store_paths(temp_root / "artifacts")
            ensure_store(store)
            retriever = DeterministicEmbeddingRetriever()
            config = EmbeddingRetrievalConfig(enabled=True, weight=24)
            collect_retrieval_explanation(
                workspace_root=temp_root,
                target_module_path=Path("Semantic.lean"),
                target_file=target_file,
                theorem="normalize_collection",
                source_text=source,
                store=store,
                failure_classes=[],
                theorem_statement="theorem normalize_collection (xs : List Nat) : List Nat :=",
                goal_snapshot=["⊢ List Nat"],
                namespace="Demo",
                embedding_config=config,
                embedding_retriever=retriever,
            )
            collect_retrieval_explanation(
                workspace_root=temp_root,
                target_module_path=Path("Semantic.lean"),
                target_file=target_file,
                theorem="normalize_collection",
                source_text=source,
                store=store,
                failure_classes=[],
                theorem_statement="theorem normalize_collection (xs : List Nat) : List Nat :=",
                goal_snapshot=["⊢ List Nat"],
                namespace="Demo",
                embedding_config=config,
                embedding_retriever=retriever,
            )
        assert retriever.calls == 1

    def _declaration_metadata(
        self,
        *,
        short_name: str,
        declaration_name: str,
        start_line: int,
        end_line: int,
        body_start_line: int,
        body_start_column: int,
        member_index: int | None,
        group_kind: str = "mutual",
        group_id: str | None = "mutual-1",
        namespace: str | None = None,
        module_path: str = "Grouped",
    ) -> ProofctlDeclarationMetadata:
        return ProofctlDeclarationMetadata(
            module_path=module_path,
            short_name=short_name,
            declaration_name=declaration_name,
            namespace=namespace,
            namespace_stack=namespace.split(".") if namespace else [],
            imports=[],
            declaration_kind="theorem",
            modifiers=["private"] if group_kind == "mutual" else [],
            group=ProofctlGroupMembership(kind=group_kind, group_id=group_id, member_index=member_index),
            declaration_span=ProbeSpan(
                start=ProbePoint(line=start_line, column=0),
                end=ProbePoint(line=end_line, column=0),
            ),
            name_span=ProbeSpan(
                start=ProbePoint(line=start_line, column=16 if group_kind == "mutual" else 8),
                end=ProbePoint(line=start_line, column=(16 if group_kind == "mutual" else 8) + len(short_name)),
            ),
            body_span=ProbeSpan(
                start=ProbePoint(line=body_start_line, column=body_start_column),
                end=ProbePoint(line=end_line, column=0),
            ),
        )
