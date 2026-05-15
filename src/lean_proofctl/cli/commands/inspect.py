"""Implementation of `proofctl inspect`."""

import argparse
from pathlib import Path

from lean_proofctl.engine.config import discover_tool_paths, ensure_tool_layout, resolve_workspace_binding
from lean_proofctl.engine.inspection import run_inspect
from lean_proofctl.engine.models import InspectResult
from lean_proofctl.engine.retrieval import EmbeddingRetrievalConfig


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the `inspect` subcommand."""
    parser = subparsers.add_parser("inspect", help="Inspect one theorem context without patching the workspace.")
    parser.add_argument("file", help="Lean file to inspect.")
    parser.add_argument(
        "--workspace",
        help="Explicit Lake workspace root. When omitted, infer from the target file, then cwd.",
    )
    parser.add_argument("--theorem", required=True, help="Exact theorem or lemma name to inspect.")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="Timeout for semantic probes and the optional full-file verification check.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--read-only",
        action="store_true",
        help="Run Lean-backed semantic inspection without creating a writable sandbox.",
    )
    mode_group.add_argument(
        "--verify-file-state",
        action="store_true",
        help="Run one extra full-file `lake lean` check in the sandbox before reporting results.",
    )
    parser.add_argument(
        "--enable-embedding-retrieval",
        action="store_true",
        help="Add optional embedding-based retrieval scoring on top of the default heuristic ranker.",
    )
    parser.add_argument(
        "--embedding-provider",
        default=None,
        help="Embedding provider name. Defaults to the configured OpenAI-compatible provider.",
    )
    parser.add_argument(
        "--embedding-model",
        default=None,
        help="Embedding model name to use when embedding retrieval is enabled.",
    )
    parser.add_argument(
        "--embedding-endpoint",
        default=None,
        help="Embedding API endpoint to use when embedding retrieval is enabled.",
    )
    parser.add_argument(
        "--embedding-api-key-env",
        default=None,
        help="Environment variable name that holds the embedding API key.",
    )
    parser.add_argument(
        "--embedding-weight",
        type=int,
        default=None,
        help="Maximum additive score contributed by embedding similarity.",
    )
    parser.set_defaults(handler=run_command)


def run_command(args: argparse.Namespace) -> tuple[InspectResult, int]:
    """Execute one theorem inspection run."""
    tool_paths = discover_tool_paths()
    ensure_tool_layout(tool_paths)
    target_file = Path(args.file).expanduser().resolve()
    if not target_file.is_file():
        msg = f"Target file does not exist: {target_file}"
        raise FileNotFoundError(msg)
    workspace = resolve_workspace_binding(
        tool_paths=tool_paths,
        workspace=getattr(args, "workspace", None),
        target_file=target_file,
    )
    embedding_config = _embedding_config_from_args(args)
    return run_inspect(
        tool_paths=tool_paths,
        workspace=workspace,
        target_file=target_file,
        theorem=args.theorem,
        timeout_seconds=args.timeout_seconds,
        read_only=args.read_only,
        verify_file_state=getattr(args, "verify_file_state", False),
        embedding_config=embedding_config,
    )


def _embedding_config_from_args(args: argparse.Namespace) -> EmbeddingRetrievalConfig | None:
    if not getattr(args, "enable_embedding_retrieval", False):
        return None
    config = EmbeddingRetrievalConfig.from_environment()
    weight = args.embedding_weight if args.embedding_weight is not None else config.weight
    if weight < 1:
        msg = "`--embedding-weight` must be at least 1."
        raise ValueError(msg)
    return EmbeddingRetrievalConfig(
        enabled=True,
        provider=args.embedding_provider or config.provider,
        model=args.embedding_model or config.model,
        endpoint=args.embedding_endpoint or config.endpoint,
        api_key_env=args.embedding_api_key_env or config.api_key_env,
        weight=weight,
    )
