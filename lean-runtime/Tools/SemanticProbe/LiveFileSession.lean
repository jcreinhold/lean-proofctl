import Lean
import Tools.SemanticProbe.DeclarationInventory
import Tools.SemanticProbe.LiveProtocol
import Tools.SemanticProbe.LoadedFile
import Tools.SemanticProbe.ProofStateQueries
import Tools.SemanticProbe.ProofTargetResolver

namespace Tools.SemanticProbe

open Lean

/--
One reusable live semantic session for one file version.

This mirrors Lean's file-worker shape in a smaller form: keep one file-scoped
snapshot root alive, answer repeated theorem-local requests from that state,
and rebuild from the old snapshot chain when the file content digest changes.
-/
structure LiveFileSession where
  filePath : System.FilePath
  contentDigest : String
  loadedFile : LoadedFile

abbrev LiveFileSessionTable := Std.HashMap String LiveFileSession

private def liveFileKey (filePath : String) : String :=
  filePath

private def dispatchLoadedCommand (command theoremName : String) (loaded : LoadedFile) : IO Json := do
  match command with
  | "context-bundle" => contextBundleFromLoaded loaded theoremName
  | "declaration-target" => declarationTargetFromLoaded loaded theoremName
  | "declaration-inventory" => pure <| declarationInventoryFromLoaded loaded theoremName
  | "proof-state" => proofStateFromLoaded loaded theoremName
  | "proof-state-after-last-tactic" => proofStateAfterLastTacticFromLoaded loaded theoremName
  | "local-context" => localContextFromLoaded loaded theoremName
  | "goal-snapshot" => goalSnapshotFromLoaded loaded theoremName
  | "insertion-point" => insertionPointFromLoaded loaded theoremName
  | _ =>
    pure <| jsonError command (toString loaded.filePath) theoremName s!"unknown live subcommand `{command}`"

private def loadLiveFileSession
    (workspaceRoot : System.FilePath)
    (sessions : LiveFileSessionTable)
    (request : LiveProbeRequest) : IO (Except ProbeFailure (LiveFileSessionTable × LiveReuseKind × LoadedFile)) := do
  let key := liveFileKey request.filePath
  let filePath := System.FilePath.mk request.filePath
  match sessions.get? key with
  | some session =>
    if session.contentDigest == request.contentDigest then
      pure <| Except.ok (sessions, .reused, session.loadedFile)
    else
      match ←
        loadFileFromSource
          workspaceRoot
          filePath
          request.content
          (old? := some session.loadedFile.initialSnap)
      with
      | .error failure => pure <| Except.error failure
      | .ok loadedFile =>
        let nextSession : LiveFileSession := { filePath, contentDigest := request.contentDigest, loadedFile }
        pure <| Except.ok (sessions.insert key nextSession, .rebuilt, loadedFile)
  | none =>
    match ← loadFileFromSource workspaceRoot filePath request.content with
    | .error failure => pure <| Except.error failure
    | .ok loadedFile =>
      let nextSession : LiveFileSession := { filePath, contentDigest := request.contentDigest, loadedFile }
      pure <| Except.ok (sessions.insert key nextSession, .created, loadedFile)

private def handleLiveRequest
    (workspaceRoot : System.FilePath)
    (sessions : LiveFileSessionTable)
    (request : LiveProbeRequest) : IO (LiveFileSessionTable × Json) := do
  match ← loadLiveFileSession workspaceRoot sessions request with
  | .error failure =>
    pure (sessions, liveErrorResponse (some request.requestId) failure.message)
  | .ok (sessions, reuseKind, loaded) =>
    let probe ← dispatchLoadedCommand request.command request.theoremName loaded
    pure (sessions, liveOkResponse request reuseKind probe)

private def handleRequestLine
    (workspaceRoot : System.FilePath)
    (sessions : LiveFileSessionTable)
    (line : String) : IO (LiveFileSessionTable × Json) := do
  match Json.parse line with
  | Except.error err =>
    pure (sessions, liveErrorResponse none s!"could not parse live request JSON: {err}")
  | Except.ok json =>
    match parseLiveProbeRequest json with
    | Except.error message =>
      pure (sessions, liveErrorResponse (liveRequestIdFromJson? json) message)
    | Except.ok request =>
      try
        handleLiveRequest workspaceRoot sessions request
      catch error =>
        pure (sessions, liveErrorResponse (some request.requestId) error.toString)

/--
Serve one long-lived live semantic process on stdin/stdout.

The session table is keyed by file identity. Each file entry carries the last
accepted content digest plus the reusable Lean snapshot state for that version.
When the digest changes, the runtime rebuilds that file from the previous
snapshot root so Lean can reuse unchanged command snapshots after the edit.
-/
partial def runLiveFileSessionServer (workspaceRoot : System.FilePath) : IO UInt32 := do
  let stdin ← IO.getStdin
  let stdout ← IO.getStdout
  loop stdin stdout ({ } : LiveFileSessionTable)
where
  loop
      (stdin : IO.FS.Stream)
      (stdout : IO.FS.Stream)
      (sessions : LiveFileSessionTable) : IO UInt32 := do
    let line ← stdin.getLine
    if line.isEmpty then
      pure 0
    else if line.trimAscii.isEmpty then
      loop stdin stdout sessions
    else
      let (sessions, response) ← handleRequestLine workspaceRoot sessions line
      stdout.putStr (response.compress ++ "\n")
      stdout.flush
      loop stdin stdout sessions

end Tools.SemanticProbe
