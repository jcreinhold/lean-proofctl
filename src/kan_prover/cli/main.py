"""CLI entrypoint for the verifier-first `kan-prover` surface."""

import argparse
import json
import sys
from typing import cast

from kan_prover.adapters.base import BackendCommandError
from kan_prover.cli.commands import (
    apply,
    bench,
    check,
    decompose,
    doctor,
    export,
    init,
    inspect,
    plan,
    prove,
    repair,
    replay,
    resume,
    retrieve,
    session,
    show,
    solve,
    trace,
)
from kan_prover.engine.models import (
    ApplyResult,
    BenchResult,
    CheckResult,
    DecomposeResult,
    DoctorResult,
    ExportResult,
    InitResult,
    InspectResult,
    JsonValue,
    LoopResult,
    PlanResult,
    ProveResult,
    ReplayResult,
    ResumeResult,
    RetrieveResult,
    SessionActionResult,
    ShowRunResult,
    ShowSessionResult,
    TraceResult,
)
from kan_prover.engine.proofctl import RequiredWorkspaceProbeError, UnsupportedTargetError


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI parser."""
    parser = argparse.ArgumentParser(
        prog="kan-prover",
        description="Verifier-first proof search tooling for arbitrary Lake workspaces.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="kan-prover 0.1.0",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one compact JSON object on stdout.",
    )
    parser.add_argument(
        "--jsonl",
        action="store_true",
        help="Emit JSONL events on stdout plus a final result event.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    init.add_parser(subparsers)
    doctor.add_parser(subparsers)
    check.add_parser(subparsers)
    inspect.add_parser(subparsers)
    retrieve.add_parser(subparsers)
    plan.add_parser(subparsers)
    prove.add_parser(subparsers)
    decompose.add_parser(subparsers)
    trace.add_parser(subparsers)
    replay.add_parser(subparsers)
    resume.add_parser(subparsers)
    export.add_parser(subparsers)
    apply.add_parser(subparsers)
    show.add_parser(subparsers)
    session.add_parser(subparsers)
    bench.add_parser(subparsers)
    repair.add_parser(subparsers)
    solve.add_parser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and separate human and machine output."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        code = error.code
        return code if isinstance(code, int) else 2

    try:
        payload, exit_code = args.handler(args)
    except FileNotFoundError as error:
        _emit_stderr(str(error))
        return 2
    except RequiredWorkspaceProbeError as error:
        payload = error.to_json()
        _emit_failure(payload, json_output=args.json, jsonl_output=args.jsonl)
        _emit_stderr(_human_failure(payload))
        return 4
    except UnsupportedTargetError as error:
        payload = error.to_json()
        _emit_failure(payload, json_output=args.json, jsonl_output=args.jsonl)
        _emit_stderr(_human_failure(payload))
        return 4
    except BackendCommandError as error:
        payload = error.to_json()
        _emit_failure(payload, json_output=args.json, jsonl_output=args.jsonl)
        _emit_stderr(_human_failure(payload))
        return 4
    except Exception as error:
        _emit_stderr(f"internal error: {error}")
        return 4

    _emit_machine(payload, json_output=args.json, jsonl_output=args.jsonl)
    _emit_stderr(_human_summary(payload))
    return exit_code


def _emit_machine(
    payload: InitResult
    | DoctorResult
    | CheckResult
    | InspectResult
    | RetrieveResult
    | PlanResult
    | ProveResult
    | ResumeResult
    | LoopResult
    | DecomposeResult
    | TraceResult
    | ReplayResult
    | ExportResult
    | ApplyResult
    | SessionActionResult
    | ShowRunResult
    | ShowSessionResult
    | BenchResult,
    *,
    json_output: bool,
    jsonl_output: bool,
) -> None:
    data = payload.to_json()
    if jsonl_output:
        print(json.dumps({"event": "summary", "status": payload.status.value}, sort_keys=True))
        print(json.dumps({"event": "result", "payload": data}, sort_keys=True))
        return
    if json_output:
        print(json.dumps(data, sort_keys=True))
        return
    if isinstance(payload, RetrieveResult):
        print(_render_retrieve_report(payload))
        return
    if isinstance(payload, ShowRunResult):
        print(_render_show_run_report(payload))
        return
    if isinstance(payload, ShowSessionResult):
        print(_render_show_session_report(payload))
        return
    print(json.dumps(data, indent=2, sort_keys=True))


def _emit_failure(payload: dict[str, JsonValue], *, json_output: bool, jsonl_output: bool) -> None:
    if jsonl_output:
        print(json.dumps({"event": "summary", "status": payload.get("status", "error")}, sort_keys=True))
        print(json.dumps({"event": "result", "payload": payload}, sort_keys=True))
        return
    if json_output:
        print(json.dumps(payload, sort_keys=True))


def _human_failure(payload: dict[str, JsonValue]) -> str:
    error_kind = str(payload.get("error_kind", "internal-error"))
    if error_kind == "required-semantic-probe-failed":
        return _human_probe_failure(payload)
    if error_kind == "unsupported-target-kind":
        return _human_unsupported_target_failure(payload)
    if error_kind == "backend-failed":
        return _human_backend_failure(payload)
    message = str(payload.get("message", "kan-prover failed"))
    return f"failure kind: {error_kind}\ncause: {message}"


def _human_probe_failure(payload: dict[str, JsonValue]) -> str:
    required_for = str(payload.get("required_for", "semantic-probe"))
    probe_result = payload.get("probe_result")
    probe_payload = probe_result if isinstance(probe_result, dict) else {}
    command = str(probe_payload.get("command", required_for))
    status = str(probe_payload.get("status", "error"))
    error_kind = probe_payload.get("error_kind")
    probe_error = f" ({error_kind})" if isinstance(error_kind, str) else ""
    message = str(probe_payload.get("message", payload.get("message", "semantic probe failed")))
    next_step = _probe_next_step(command=command, status=status, error_kind=error_kind, message=message)
    return (
        "failure kind: required-semantic-probe-failed\n"
        f"cause: semantic `{required_for}` probe data is unavailable because `{command}` returned "
        f"`{status}`{probe_error}: {message}\n"
        f"next step: {next_step}"
    )


def _probe_next_step(*, command: str, status: str, error_kind: JsonValue, message: str) -> str:
    error_kind_value = error_kind if isinstance(error_kind, str) else None
    message_lower = message.lower()
    if status == "unavailable" or "writable sandboxed workspace" in message_lower:
        return (
            "bind `--workspace` to a writable Lake workspace, then rerun; "
            "use `kan-prover doctor --workspace <workspace>` first if you need a quick readiness check"
        )
    if error_kind_value == "bootstrap-failed":
        return f"build or repair the bundled SemanticProbe executable for `{command}`, then rerun"
    if error_kind_value in {"execution-failed", "helper-status"}:
        return (
            f"run `kan-prover doctor --workspace <workspace>` and fix the Lean/Lake failure behind "
            f"`{command}`, then rerun"
        )
    if error_kind_value in {"malformed-json", "decode-failed"}:
        return f"inspect the `{command}` semantic probe output and fix the helper/protocol mismatch before rerunning"
    return "fix the semantic probe failure in the bound workspace, then rerun"


def _human_unsupported_target_failure(payload: dict[str, JsonValue]) -> str:
    command = str(payload.get("command", "kan-prover"))
    declaration_name = str(payload.get("declaration_name", "target"))
    message = str(payload.get("message", "kan-prover target is unsupported"))
    detail = payload.get("detail")
    lines = [
        "failure kind: unsupported-target-kind\n"
        f"cause: `{command}` cannot use `{declaration_name}` as a proof-workflow target: {message}\n"
    ]
    if isinstance(detail, str) and detail:
        lines.append(f"detail: {detail}\n")
    lines.append(f"next step: {_unsupported_target_next_step(command=command, declaration_name=declaration_name)}")
    return "".join(lines)


def _unsupported_target_next_step(*, command: str, declaration_name: str) -> str:
    _ = command
    return (
        f"choose a proof declaration with an editable body, then rerun; "
        f"if `{declaration_name}` is only a catalog target, use declaration metadata "
        "surfaces instead of proof workflows"
    )


def _human_backend_failure(payload: dict[str, JsonValue]) -> str:
    backend = str(payload.get("backend", "backend"))
    backend_error_kind = str(payload.get("backend_error_kind", "failed"))
    message = str(payload.get("message", "backend failed"))
    next_step = _backend_next_step(backend=backend, backend_error_kind=backend_error_kind)
    return (
        "failure kind: backend-failed\n"
        f"cause: `{backend}` returned `{backend_error_kind}`: {message}\n"
        f"next step: {next_step}"
    )


def _backend_next_step(*, backend: str, backend_error_kind: str) -> str:
    if backend_error_kind == "unavailable":
        return (
            f"install or expose the `{backend}` CLI on PATH, or rerun with `--agent none` if you want "
            "Lean-only verification without a hosted backend"
        )
    if backend_error_kind == "execution-failed":
        return f"run `kan-prover doctor --agent {backend}` and fix the `{backend}` CLI startup failure, then rerun"
    if backend_error_kind == "protocol-failed":
        return f"fix the `{backend}` backend output so it matches the strict kan-prover schema, then rerun"
    return f"fix the `{backend}` backend failure and rerun"


def _human_summary(
    payload: InitResult
    | DoctorResult
    | CheckResult
    | InspectResult
    | RetrieveResult
    | PlanResult
    | ProveResult
    | ResumeResult
    | LoopResult
    | DecomposeResult
    | TraceResult
    | ReplayResult
    | ExportResult
    | ApplyResult
    | SessionActionResult
    | ShowRunResult
    | ShowSessionResult
    | BenchResult,
) -> str:
    if isinstance(payload, InitResult):
        return (
            f"kan-prover init {payload.status.value}: workspace={payload.workspace_root} "
            f"role={payload.workspace_role} "
            f"build_ran={str(payload.build_ran).lower()} wall={payload.wall_time_seconds:.3f}s"
        )
    if isinstance(payload, DoctorResult):
        failed_checks = [check.name for check in payload.checks if check.status.value == "fail"]
        warnings = [check.name for check in payload.checks if check.status.value == "warn"]
        failed_suffix = f" failed={','.join(failed_checks)}" if failed_checks else ""
        warning_suffix = f" warnings={','.join(warnings)}" if warnings else ""
        return (
            f"kan-prover doctor {payload.status.value}: workspace={payload.inspected_path} "
            f"role={payload.workspace_role} ready={str(payload.ready).lower()}{failed_suffix}{warning_suffix} "
            f"wall={payload.wall_time_seconds:.3f}s"
        )
    if isinstance(payload, InspectResult):
        return (
            f"kan-prover {payload.command} {payload.status.value}: file={payload.target_file} "
            f"theorem={payload.theorem} retrieval={len(payload.snapshot.retrieval_hits)} "
            f"helpers={len(payload.snapshot.active_helpers)} wall={payload.wall_time_seconds:.3f}s "
            f"file_state_check={payload.file_state_check.status.value} run_id={payload.run_id}"
        )
    if isinstance(payload, RetrieveResult):
        return (
            f"kan-prover retrieve {payload.status.value}: file={payload.target_file} "
            f"theorem={payload.theorem} retrieval={len(payload.snapshot.retrieval.hits)} "
            f"near_misses={len(payload.snapshot.retrieval.near_misses)} "
            f"helpers={len(payload.snapshot.retrieval.active_helpers)} wall={payload.wall_time_seconds:.3f}s "
            f"file_state_check={payload.file_state_check.status.value} run_id={payload.run_id}"
        )
    if isinstance(payload, PlanResult):
        return (
            f"kan-prover plan {payload.status.value}: file={payload.target_file} "
            f"theorem={payload.theorem} backend={payload.backend} "
            f"steps={0 if payload.plan is None else len(payload.plan.steps)} "
            f"wall={payload.wall_time_seconds:.3f}s run_id={payload.run_id}"
        )
    if isinstance(payload, ProveResult):
        verified_helpers = sum(1 for helper in payload.helpers if helper.status.value == "verified")
        return (
            f"kan-prover prove {payload.status.value}: file={payload.target_file} "
            f"theorem={payload.theorem} backend={payload.backend} "
            f"nodes={payload.graph.node_count} verified_node={payload.graph.verified_node_id} "
            f"helpers={len(payload.helpers)} verified_helpers={verified_helpers} "
            f"frontier={len(payload.graph.frontier_node_ids)} wall={payload.wall_time_seconds:.3f}s "
            f"run_id={payload.run_id}"
        )
    if isinstance(payload, ResumeResult):
        verified_helpers = sum(1 for helper in payload.helpers if helper.status.value == "verified")
        return (
            f"kan-prover resume {payload.status.value}: source_run={payload.source_run_id} "
            f"file={payload.target_file} theorem={payload.theorem} backend={payload.backend} "
            f"nodes={payload.graph.node_count} verified_node={payload.graph.verified_node_id} "
            f"helpers={len(payload.helpers)} verified_helpers={verified_helpers} "
            f"frontier={len(payload.graph.frontier_node_ids)} wall={payload.wall_time_seconds:.3f}s "
            f"run_id={payload.run_id}"
        )
    if isinstance(payload, LoopResult):
        return (
            f"kan-prover {payload.command} {payload.status.value}: file={payload.target_file} "
            f"theorem={payload.theorem} attempts={payload.attempt_count} "
            f"verified_attempt={payload.verified_attempt} applied={str(payload.applied).lower()} "
            f"wall={payload.wall_time_seconds:.3f}s run_id={payload.run_id}"
        )
    if isinstance(payload, DecomposeResult):
        return (
            f"kan-prover decompose {payload.status.value}: file={payload.target_file} "
            f"theorem={payload.theorem} steps={len(payload.steps)} retrieval={len(payload.retrieval_hits)} "
            f"wall={payload.wall_time_seconds:.3f}s run_id={payload.run_id}"
        )
    if isinstance(payload, TraceResult):
        return (
            f"kan-prover trace {payload.status.value}: source_run={payload.source_run_id} "
            f"file={payload.target_file} theorem={payload.theorem} attempts={payload.attempt_count} "
            f"backend={payload.backend} run_id={payload.run_id}"
        )
    if isinstance(payload, ReplayResult):
        return (
            f"kan-prover replay {payload.status.value}: source_run={payload.source_run_id} "
            f"mode={payload.mode.value} replayed_run={payload.replayed_run_id} "
            f"status={payload.replayed_status} run_id={payload.run_id}"
        )
    if isinstance(payload, ExportResult):
        return (
            f"kan-prover export {payload.status.value}: source_run={payload.source_run_id} "
            f"file={payload.target_file} theorem={payload.theorem} bundle={payload.bundle_path} "
            f"run_id={payload.run_id}"
        )
    if isinstance(payload, ApplyResult):
        suffix = (
            f" error_code={payload.error_code.value} error={payload.error_message}"
            if payload.error_code is not None
            else ""
        )
        return (
            f"kan-prover apply {payload.status.value}: source_run={payload.source_run_id} "
            f"node={payload.source_node_id} file={payload.target_file} theorem={payload.theorem} "
            f"helpers={len(payload.applied_helper_modules)} run_id={payload.run_id}{suffix}"
        )
    if isinstance(payload, SessionActionResult):
        return (
            f"kan-prover session {payload.action.value} {payload.status.value}: session={payload.session_id} "
            f"file={payload.target_file} theorem={payload.theorem} node={payload.current_node_id} "
            f"diagnostics={payload.diagnostic_count} run_id={payload.run_id}"
        )
    if isinstance(payload, ShowRunResult):
        return (
            f"kan-prover show run {payload.status.value}: source_run={payload.source_run_id} "
            f"command={payload.stored_command} theorem={payload.theorem} "
            f"retrieval={len(payload.retrieval_hits)} run_id={payload.run_id}"
        )
    if isinstance(payload, ShowSessionResult):
        return (
            f"kan-prover show session {payload.status.value}: source_session={payload.source_session_id} "
            f"theorem={payload.theorem} node={payload.selected.node_id} run_id={payload.run_id}"
        )
    if isinstance(payload, BenchResult):
        return (
            f"kan-prover bench {payload.status.value}: spec={payload.spec_path} "
            f"targets={payload.target_count} success={payload.success_count} failure={payload.failure_count} "
            f"run_id={payload.run_id}"
        )
    classes = ", ".join(diagnostic.class_name.value for diagnostic in payload.diagnostics[:3])
    if payload.diagnostic_count > 3:
        classes = f"{classes}, ..."
    suffix = f" classes=[{classes}]" if classes else ""
    return (
        f"kan-prover check {payload.status.value}: file={payload.target_file} "
        f"diagnostics={payload.diagnostic_count} wall={payload.wall_time_seconds:.3f}s"
        f"{suffix} run_id={payload.run_id}"
    )


def _render_retrieve_report(payload: RetrieveResult) -> str:
    lines = [
        f"theorem: {payload.theorem}",
        f"file: {payload.target_file}",
        f"namespace: {payload.snapshot.namespace or '-'}",
        f"goal count: {len(payload.snapshot.goal_snapshot)}",
        f"file-state check: {payload.file_state_check.status.value}",
        f"file-state diagnostics: {payload.file_state_check.diagnostic_count}",
        "",
        "Ranked hits:",
    ]
    if payload.snapshot.retrieval.hits:
        for index, hit in enumerate(payload.snapshot.retrieval.hits, start=1):
            lines.append(f"{index}. [{hit.score}] {hit.source_kind.value} {hit.title}")
            for component in hit.score_breakdown:
                detail = f" ({component.detail})" if component.detail else ""
                lines.append(f"   +{component.points} {component.label}{detail}")
    else:
        lines.append("none")
    lines.append("")
    lines.append("Near misses:")
    if payload.snapshot.retrieval.near_misses:
        for miss in payload.snapshot.retrieval.near_misses:
            lines.append(f"- [{miss.score}] {miss.source_kind.value} {miss.title}: {miss.omission_reason}")
    else:
        lines.append("none")
    return "\n".join(lines)


def _render_show_run_report(payload: ShowRunResult) -> str:
    lines = [
        f"run: {payload.source_run_id}",
        f"command: {payload.stored_command}",
        f"status: {payload.stored_status}",
        f"file: {payload.target_file}",
        f"theorem: {payload.theorem or '-'}",
        f"backend: {payload.backend or '-'}",
        f"artifact dir: {payload.source_artifact_dir}",
        "",
        f"Retrieval hits ({len(payload.retrieval_hits)}):",
    ]
    if payload.retrieval_hits:
        for hit in payload.retrieval_hits:
            lines.append(f"- [{hit.score}] {hit.source_kind} {hit.title}")
    else:
        lines.append("none")
    lines.extend(["", "Proof target:"])
    if payload.proof_target:
        safe_edit = cast(dict[str, JsonValue] | None, payload.proof_target.get("safe_edit"))
        lines.append(f"- declaration: {payload.proof_target.get('declaration_name') or '-'}")
        lines.append(f"- keyword: {payload.proof_target.get('keyword') or '-'}")
        lines.append(f"- safe edit: {safe_edit.get('kind') if isinstance(safe_edit, dict) else '-'}")
        lines.append(f"- proof state available: {str(bool(payload.proof_target.get('proof_state_available'))).lower()}")
    else:
        lines.append("none")
    lines.extend(["", "Semantic probes:"])
    if payload.semantic_probes:
        for probe in payload.semantic_probes:
            detail = f" ({probe.summary})" if probe.summary else ""
            lines.append(f"- {probe.probe}: {probe.status}{detail}")
    else:
        lines.append("none")
    if payload.graph is not None:
        lines.extend(
            [
                "",
                "Graph:",
                f"- nodes: {payload.graph.node_count}",
                f"- edges: {payload.graph.edge_count}",
                f"- verified node: {payload.graph.verified_node_id or '-'}",
                "- frontier: "
                + (", ".join(payload.graph.frontier_node_ids) if payload.graph.frontier_node_ids else "none"),
                "- branch: " + (", ".join(payload.graph.branch_node_ids) if payload.graph.branch_node_ids else "none"),
            ]
        )
    if payload.selected is not None:
        lines.extend(
            [
                "",
                f"Selected {payload.selected.selection_kind}:",
                f"- label: {payload.selected.label}",
                f"- status: {payload.selected.status or '-'}",
                f"- verified: {str(payload.selected.verified).lower()}",
                f"- diagnostics: {payload.selected.diagnostic_count}",
            ]
        )
        if payload.selected.goal_count is not None:
            lines.append(f"- goals: {payload.selected.goal_count}")
        if payload.selected.candidate_id is not None:
            lines.append(f"- candidate: {payload.selected.candidate_id}")
        if payload.selected.action_kind is not None:
            lines.append(f"- action: {payload.selected.action_kind}")
        if payload.selected.response_class is not None:
            lines.append(f"- response class: {payload.selected.response_class}")
        if payload.selected.session_id is not None:
            lines.append(f"- session: {payload.selected.session_id}")
        if payload.selected.session_node_id is not None:
            lines.append(f"- session node: {payload.selected.session_node_id}")
        if payload.selected.semantic_session is not None:
            lines.append(
                "- semantic session: "
                f"{payload.selected.semantic_session.get('mode', '-')}"
                + (
                    f" ({payload.selected.semantic_session.get('reuse')})"
                    if payload.selected.semantic_session.get("reuse")
                    else ""
                )
            )
        if payload.selected.semantic_probes:
            lines.append("- selected semantic probes:")
            for probe in payload.selected.semantic_probes:
                detail = f" ({probe.summary})" if probe.summary else ""
                lines.append(f"  {probe.probe}: {probe.status}{detail}")
        if payload.selected.pretty_proof_state:
            lines.extend(["", "Proof state:", payload.selected.pretty_proof_state])
    return "\n".join(lines)


def _render_show_session_report(payload: ShowSessionResult) -> str:
    lines = [
        f"session: {payload.source_session_id}",
        f"status: {payload.session_status}",
        f"file: {payload.target_file}",
        f"theorem: {payload.theorem}",
        f"artifact dir: {payload.session_artifact_dir}",
        f"current node: {payload.current_node_id or '-'}",
        f"checkpoints: {', '.join(payload.checkpoints) if payload.checkpoints else 'none'}",
        "",
        "Nodes:",
    ]
    for node in payload.nodes:
        lines.append(
            f"- #{node.index} {node.node_id} action={node.action} status={node.status or '-'} "
            f"diagnostics={node.diagnostic_count} goals={node.goal_count}"
        )
    lines.extend(
        [
            "",
            "Selected node:",
            f"- #{payload.selected.index} {payload.selected.node_id}",
            f"- action: {payload.selected.action}",
            f"- parent: {payload.selected.parent_node_id or '-'}",
            f"- status: {payload.selected.status or '-'}",
            f"- diagnostics: {payload.selected.diagnostic_count}",
            f"- goals: {payload.selected.goal_count}",
        ]
    )
    if payload.selected.proof_target:
        safe_edit = cast(dict[str, JsonValue] | None, payload.selected.proof_target.get("safe_edit"))
        lines.extend(
            [
                f"- declaration: {payload.selected.proof_target.get('declaration_name') or '-'}",
                f"- safe edit: {safe_edit.get('kind') if isinstance(safe_edit, dict) else '-'}",
                "- proof state available: "
                f"{str(bool(payload.selected.proof_target.get('proof_state_available'))).lower()}",
            ]
        )
    if payload.selected.semantic_session is not None:
        lines.append(
            "- semantic session: "
            f"{payload.selected.semantic_session.get('mode', '-')}"
            + (
                f" ({payload.selected.semantic_session.get('reuse')})"
                if payload.selected.semantic_session.get("reuse")
                else ""
            )
        )
    if payload.selected.semantic_probes:
        lines.append("- semantic probes:")
        for probe in payload.selected.semantic_probes:
            detail = f" ({probe.summary})" if probe.summary else ""
            lines.append(f"  {probe.probe}: {probe.status}{detail}")
    if payload.selected.pretty_proof_state:
        lines.extend(["", "Proof state:", payload.selected.pretty_proof_state])
    return "\n".join(lines)


def _emit_stderr(message: str) -> None:
    print(message, file=sys.stderr)
