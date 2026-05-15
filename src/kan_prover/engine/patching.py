"""Theorem-body replacement and sandbox patch helpers."""

from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path

from kan_prover.engine.models import DecompositionStep, HelperRef
from kan_prover.engine.sandbox import MANAGED_HELPER_MODULE_ROOT, MANAGED_HELPER_ROOT, SandboxWorkspace
from kan_prover.engine.theorem_locator import TheoremSpan, locate_theorem_span_in_file


@dataclass(frozen=True)
class SandboxPatch:
    """Patched sandbox file paths and rendered content."""

    sandbox_root: Path
    sandbox_workspace_root: Path
    sandbox_file: Path
    verification_file: Path
    patched_text: str
    diff_text: str
    helper_modules: list[str]


def replace_theorem_body(source: str, span: TheoremSpan, body: str) -> str:
    """Replace only the theorem body text between `:=` and the next declaration."""
    normalized_body = _normalize_body(body)
    return f"{source[: span.body_start]}{normalized_body}{source[span.body_end :]}"


def create_sandbox_patch(
    *,
    sandbox: SandboxWorkspace,
    sandbox_root: Path,
    target_file: Path,
    source_text: str,
    span: TheoremSpan,
    candidate_body: str,
    active_helpers: list[HelperRef],
) -> SandboxPatch:
    """Patch only the theorem body inside an existing sandbox workspace."""
    sandbox_file = sandbox.target_file(target_file)
    helper_modules = [helper.module_name for helper in active_helpers]
    patched_text = apply_helper_imports(
        replace_theorem_body(source_text, span, candidate_body),
        helper_modules=helper_modules,
    )
    sandbox_file.write_text(patched_text, encoding="utf-8")
    diff_text = "".join(
        unified_diff(
            source_text.splitlines(keepends=True),
            patched_text.splitlines(keepends=True),
            fromfile=str(target_file),
            tofile=str(sandbox_file),
        )
    )
    return SandboxPatch(
        sandbox_root=sandbox_root,
        sandbox_workspace_root=sandbox.sandbox_workspace.root,
        sandbox_file=sandbox_file,
        verification_file=sandbox_file,
        patched_text=patched_text,
        diff_text=diff_text,
        helper_modules=helper_modules,
    )


def apply_helper_imports(source_text: str, *, helper_modules: list[str]) -> str:
    """Insert any missing helper imports at the top of the target file."""
    unique_modules = [module for module in dict.fromkeys(helper_modules) if f"import {module}" not in source_text]
    if not unique_modules:
        return source_text
    lines = source_text.splitlines()
    insert_at = 0
    while insert_at < len(lines) and lines[insert_at].startswith("import "):
        insert_at += 1
    import_lines = [f"import {module}" for module in unique_modules]
    updated_lines = lines[:insert_at] + import_lines + ([""] if lines else []) + lines[insert_at:]
    return "\n".join(updated_lines) + ("\n" if source_text.endswith("\n") or not source_text else "")


def helper_module_name(*, target_file: Path, theorem: str, helper_name: str, project_root: Path) -> str:
    """Build a stable helper module name under `KanProverGenerated.Helpers`."""
    relative = target_file.resolve().relative_to(project_root)
    parts = list(relative.with_suffix("").parts)
    segments = [
        *MANAGED_HELPER_MODULE_ROOT.split("."),
        *[_module_segment(part) for part in parts],
        _module_segment(theorem),
        _module_segment(helper_name),
    ]
    return ".".join(segment for segment in segments if segment)


def render_helper_file(step: DecompositionStep) -> str:
    """Render one helper file from a structured decomposition step."""
    if step.helper_name is None or step.helper_statement is None or step.helper_proof is None:
        msg = f"Decomposition step `{step.step_id}` is missing helper fields."
        raise ValueError(msg)
    imports = [
        import_line if import_line.startswith("import ") else f"import {import_line}"
        for import_line in dict.fromkeys(step.helper_imports)
    ]
    import_block = f"{'\n'.join(imports)}\n\n" if imports else ""
    return f"{import_block}{step.helper_statement}{_normalize_body(step.helper_proof)}\n"


def apply_verified_transplant(
    *,
    sandbox: SandboxWorkspace,
    target_file: Path,
    theorem: str,
    candidate_body: str,
    helper_modules: list[str],
) -> None:
    """Apply one verified theorem draft plus managed helpers to the real workspace."""
    sandbox_target_file = sandbox.target_file(target_file)
    sandbox_text = sandbox_target_file.read_text(encoding="utf-8")
    span = locate_theorem_span_in_file(
        target_file=sandbox_target_file,
        source=sandbox_text,
        theorem_name=theorem,
    )
    current_text = target_file.read_text(encoding="utf-8")
    patched_text = apply_helper_imports(
        replace_theorem_body(current_text, span, candidate_body),
        helper_modules=helper_modules,
    )
    apply_verified_helper_files(sandbox=sandbox, helper_modules=helper_modules)
    target_file.write_text(patched_text, encoding="utf-8")


def apply_verified_helper_files(*, sandbox: SandboxWorkspace, helper_modules: list[str]) -> None:
    """Copy verified helper files from a sandbox into the managed helper root only."""
    managed_root = sandbox.real_workspace.root / MANAGED_HELPER_ROOT
    for module_name in helper_modules:
        source = sandbox.helper_file(module_name)
        destination = sandbox.real_helper_file(module_name)
        if not source.is_file():
            msg = f"Verified helper source is missing from sandbox: {source}"
            raise FileNotFoundError(msg)
        if destination.is_file():
            if destination.read_text(encoding="utf-8") != source.read_text(encoding="utf-8"):
                msg = (
                    f"Managed helper `{module_name}` already exists in the real workspace with different contents. "
                    "Verified apply only supports helper additions under `KanProverGenerated/Helpers`."
                )
                raise ValueError(msg)
            continue
        if managed_root not in destination.parents:
            msg = f"Refusing to apply helper outside managed root: {destination}"
            raise ValueError(msg)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _normalize_body(body: str) -> str:
    if not body:
        return " "
    return body if body[:1].isspace() else f" {body}"


def _module_segment(text: str) -> str:
    words = [word for word in "".join(char if char.isalnum() else " " for char in text).split() if word]
    if not words:
        return "Generated"
    segment = "".join(word[:1].upper() + word[1:] for word in words)
    if segment[:1].isdigit():
        return f"N{segment}"
    return segment
