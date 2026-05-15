import Lean
import Lean.Elab
import Lean.Language.Lean
import Tools.SemanticProbe.Protocol

namespace Tools.SemanticProbe

open Lean
open Lean.Elab
open Lean.Language
open Lean.Language.Lean

/-- Parsed file state that later probes reuse instead of reparsing ad hoc.

`commands` is the full parsed command snapshot chain for the file. It may contain
snapshots that are not declaration boundaries, so later code must still filter
for the boundary kinds it cares about.
-/
structure LoadedFile where
  workspaceRoot : System.FilePath
  filePath : System.FilePath
  modulePath : String
  imports : Array String
  inputCtx : Parser.InputContext
  initialSnap : InitialSnapshot
  commands : Array CommandParsedSnapshot

private partial def collectSnapshots (snap : CommandParsedSnapshot) : Array CommandParsedSnapshot :=
  let rest :=
    match snap.nextCmdSnap? with
    | some next => collectSnapshots next.get
    | none => #[]
  #[snap] ++ rest

private def headerImports (header : TSyntax `Lean.Parser.Module.header) : Array String :=
  header.raw[2].getArgs.foldl
    (init := #[])
    fun imports stx =>
      if stx.getKind == ``Lean.Parser.Module.import && stx.getNumArgs > 4 && stx[4].isIdent then
        imports.push stx[4].getId.toString
      else
        imports

private def modulePathFromWorkspace (workspaceRoot filePath : System.FilePath) : String :=
  let rootDepth := workspaceRoot.components.length
  let relativeParts := (filePath.withExtension "").components.drop rootDepth
  String.intercalate "." relativeParts

private def moduleNameFromPath (modulePath : String) : Name :=
  (modulePath.splitOn ".").foldl
    (init := Name.anonymous)
    fun current segment =>
      if segment.isEmpty then
        current
      else
        Name.str current segment

private def setupImportsForModule (modulePath : String) :
    HeaderSyntax → Language.ProcessingT IO (Except Lean.Language.Lean.HeaderProcessedSnapshot SetupImportsResult)
  | header =>
      pure <|
        Except.ok
          { mainModuleName := moduleNameFromPath modulePath
          , isModule := header.isModule
          , imports := header.imports
          , opts := Options.empty
          }

private def commandsFromProcessedState (processed : HeaderProcessedState) : Array CommandParsedSnapshot :=
  collectSnapshots processed.firstCmdSnap.get

private def renderMessageLogs (logs : Array MessageLog) : IO (Array String) := do
  let mut lines := #[]
  for log in logs do
    for msg in MessageLog.toList log do
      lines := lines.push (← msg.toString)
  pure lines

private def processingFailure
    (filePath : System.FilePath)
    (logs : Array MessageLog)
    (fallback : String) : IO ProbeFailure := do
  let rendered ← renderMessageLogs logs
  if rendered.isEmpty then
    pure
      { kind := .error
      , message := s!"{fallback} (`{filePath}`)"
      }
  else
    pure
      { kind := .unavailable
      , message := s!"Lean could not load `{filePath}` into a processed command chain.\n\n{String.intercalate "\n\n" rendered.toList}"
      }

private def processedStateOrFailure
    (filePath : System.FilePath)
    (snap : InitialSnapshot) : IO (Except ProbeFailure HeaderProcessedState) := do
  match snap.result? with
  | none =>
    return Except.error <|
      ← processingFailure
          filePath
          #[snap.diagnostics.msgLog, snap.metaSnap.get.diagnostics.msgLog]
          "SemanticProbe could not parse the file header"
  | some parsed =>
    let processedSnap := parsed.processedSnap.get
    match processedSnap.result? with
    | some processed => pure <| Except.ok processed
    | none =>
      return Except.error <|
        ← processingFailure
            filePath
            #[processedSnap.diagnostics.msgLog, processedSnap.metaSnap.get.diagnostics.msgLog]
            "SemanticProbe did not receive one processed header state from Lean"

/-- Parse one file in the target workspace and keep the command snapshot chain.

The helper processes the real file inside the target workspace rather than the
bundled runtime because import resolution must match the workspace that Lean will
later verify. Failures here are real parse or elaboration failures, not fallback
signals.
-/
def loadFileFromSource
    (workspaceRoot filePath : System.FilePath)
    (source : String)
    (old? : Option InitialSnapshot := none) : IO (Except ProbeFailure LoadedFile) := do
  let inputCtx := Parser.mkInputContext source (toString filePath)
  let (header, _, _) ← Parser.parseHeader inputCtx
  let modulePath := modulePathFromWorkspace workspaceRoot filePath
  let initialSnap ← Lean.Language.Lean.process (setupImportsForModule modulePath) old? { inputCtx with }
  match ← processedStateOrFailure filePath initialSnap with
  | .error failure => pure <| Except.error failure
  | .ok processed =>
    pure <|
      Except.ok
        { workspaceRoot
        , filePath
        , modulePath
        , imports := headerImports header
        , inputCtx
        , initialSnap
        , commands := commandsFromProcessedState processed
        }

def loadFile (workspaceRoot filePath : System.FilePath) : IO (Except ProbeFailure LoadedFile) := do
  let source ← IO.FS.readFile filePath
  loadFileFromSource workspaceRoot filePath source

end Tools.SemanticProbe
