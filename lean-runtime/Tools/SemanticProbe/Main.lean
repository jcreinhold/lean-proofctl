import Lean
import Tools

namespace Tools.SemanticProbe.Main

open Lean
open Tools.SemanticProbe

def usage : String :=
  String.intercalate "\n"
    [ "usage: SemanticProbe <command> <workspace-root> [file] [target]"
    , ""
    , "commands:"
    , "  batch-declaration-inventory <frontier-manifest>"
    , "  context-bundle"
    , "  declaration-target"
    , "  declaration-inventory"
    , "  serve"
    , "  workspace-declaration-index <workspace-module-manifest>"
    , "  proof-state"
    , "  proof-state-after-last-tactic"
    , "  local-context"
    , "  goal-snapshot"
    , "  insertion-point"
    ]

def dispatch (args : List String) : IO (Prod UInt32 Json) := do
  match args with
  | [command, workspaceRoot] =>
    pure <| (2, jsonError command workspaceRoot "" "this command requires a file path and target name")
  | [command, workspaceRoot, filePath, theoremName, manifestPath] =>
    let payload ←
      match command with
      | "batch-declaration-inventory" =>
        batchDeclarationInventoryCommand workspaceRoot filePath theoremName manifestPath
      | "workspace-declaration-index" =>
        workspaceDeclarationIndexCommand workspaceRoot filePath theoremName manifestPath
      | _ => pure <| jsonError command filePath theoremName s!"unknown subcommand `{command}`"
    pure (exitCodeForPayload payload, payload)
  | [command, workspaceRoot, filePath, theoremName] =>
    let payload ←
      match command with
      | "batch-declaration-inventory" =>
        pure <| jsonError command filePath theoremName "batch-declaration-inventory requires a frontier manifest path"
      | "workspace-declaration-index" =>
        pure <| jsonError command filePath theoremName "workspace-declaration-index requires a workspace module manifest path"
      | "context-bundle" => contextBundleCommand workspaceRoot filePath theoremName
      | "declaration-target" => declarationTargetCommand workspaceRoot filePath theoremName
      | "declaration-inventory" => declarationInventoryCommand workspaceRoot filePath theoremName
      | "proof-state" => proofStateCommand workspaceRoot filePath theoremName
      | "proof-state-after-last-tactic" => proofStateAfterLastTacticCommand workspaceRoot filePath theoremName
      | "local-context" => localContextCommand workspaceRoot filePath theoremName
      | "goal-snapshot" => goalSnapshotCommand workspaceRoot filePath theoremName
      | "insertion-point" => insertionPointCommand workspaceRoot filePath theoremName
      | _ => pure <| jsonError command filePath theoremName s!"unknown subcommand `{command}`"
    pure (exitCodeForPayload payload, payload)
  | _ =>
    pure
      (2, Json.mkObj [("schema_version", schemaVersion), ("status", Json.str "error"), ("message", Json.str usage)])

private def workspaceSearchPath (workspaceRoot : System.FilePath) : IO SearchPath := do
  let buildLib := workspaceRoot / ".lake" / "build" / "lib" / "lean"
  let mut entries : SearchPath := if ← buildLib.isDir then [buildLib] else []
  let packagesDir := workspaceRoot / ".lake" / "packages"
  if ← packagesDir.isDir then
    for packageEntry in (← packagesDir.readDir) do
      if ← packageEntry.path.isDir then
        let packageLib := packageEntry.path / ".lake" / "build" / "lib" / "lean"
        if ← packageLib.isDir then
          entries := entries ++ [packageLib]
  let helperSearchRoot := workspaceRoot / ".proofctl-helper-root"
  if ← helperSearchRoot.isDir then
    entries := entries ++ [helperSearchRoot]
  pure entries

private def workspaceRootFromArgs? : List String → Option System.FilePath
  | ["serve", workspaceRoot] => some <| System.FilePath.mk workspaceRoot
  | [_command, workspaceRoot] => some <| System.FilePath.mk workspaceRoot
  | [_command, workspaceRoot, _filePath, _theoremName] => some <| System.FilePath.mk workspaceRoot
  | [_command, workspaceRoot, _filePath, _theoremName, _manifestPath] =>
      some <| System.FilePath.mk workspaceRoot
  | _ => none

def run (args : List String) : IO UInt32 := do
  try
    let leanSysroot ← findSysroot
    match workspaceRootFromArgs? args with
    | some workspaceRoot =>
      initSearchPath leanSysroot (← workspaceSearchPath workspaceRoot)
    | none =>
      initSearchPath leanSysroot
    match args with
    | ["serve", workspaceRoot] =>
      runLiveFileSessionServer workspaceRoot
    | _ =>
      let (exitCode, payload) ← dispatch args
      IO.println payload.compress
      pure exitCode
  catch error =>
    let payload :=
      jsonError
        "semantic-probe"
        ""
        ""
        error.toString
    IO.println payload.compress
    pure 4

end Tools.SemanticProbe.Main

def main (args : List String) : IO UInt32 :=
  Tools.SemanticProbe.Main.run args
