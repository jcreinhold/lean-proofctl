import Lean
import Lean.Elab
import Lean.Server.InfoUtils
import Tools.SemanticProbe.DeclarationCatalog
import Tools.SemanticProbe.ProofTargetResolver
import Tools.SemanticProbe.Protocol

namespace Tools.SemanticProbe

open Lean
open Lean.Elab

private def infoInRange (info : Elab.Info) (startPos endPos : String.Pos.Raw) : Bool :=
  match info.pos?, info.tailPos? with
  | some infoStart, some infoEnd => startPos <= infoStart && infoEnd <= endPos
  | _, _ => false

private def firstTacticInfo? (tree : Elab.InfoTree) (startPos endPos : String.Pos.Raw) :
    Option (Elab.ContextInfo × Elab.TacticInfo) :=
  let candidates := tree.foldInfo (init := #[]) fun ctx info acc =>
    match info with
    | .ofTacticInfo tacticInfo =>
      if infoInRange (.ofTacticInfo tacticInfo) startPos endPos then
        acc.push (ctx, tacticInfo)
      else
        acc
    | _ => acc
  candidates.foldl
    (init := none)
    fun best? candidate =>
      match best? with
      | none => some candidate
      | some best =>
        let candidatePos := candidate.2.stx.getPos? (canonicalOnly := true) |>.getD 0
        let bestPos := best.2.stx.getPos? (canonicalOnly := true) |>.getD 0
        if candidatePos < bestPos then some candidate else best?

private def lastTacticInfo? (tree : Elab.InfoTree) (startPos endPos : String.Pos.Raw) :
    Option (Elab.ContextInfo × Elab.TacticInfo) :=
  let candidates := tree.foldInfo (init := #[]) fun ctx info acc =>
    match info with
    | .ofTacticInfo tacticInfo =>
      if infoInRange (.ofTacticInfo tacticInfo) startPos endPos then
        acc.push (ctx, tacticInfo)
      else
        acc
    | _ => acc
  candidates.foldl
    (init := none)
    fun best? candidate =>
      match best? with
      | none => some candidate
      | some best =>
        let candidatePos := candidate.2.stx.getTailPos? (canonicalOnly := true) |>.getD 0
        let bestPos := best.2.stx.getTailPos? (canonicalOnly := true) |>.getD 0
        if candidatePos > bestPos then some candidate else best?

private def firstTermInfo? (tree : Elab.InfoTree) (startPos endPos : String.Pos.Raw) :
    Option (Elab.ContextInfo × Elab.TermInfo) :=
  let candidates := tree.foldInfo (init := #[]) fun ctx info acc =>
    match info with
    | .ofTermInfo termInfo =>
      if infoInRange (.ofTermInfo termInfo) startPos endPos then
        acc.push (ctx, termInfo)
      else
        acc
    | _ => acc
  candidates.foldl
    (init := none)
    fun best? candidate =>
      match best? with
      | none => some candidate
      | some best =>
        let candidatePos := candidate.2.stx.getPos? (canonicalOnly := true) |>.getD 0
        let bestPos := best.2.stx.getPos? (canonicalOnly := true) |>.getD 0
        if candidatePos < bestPos then some candidate else best?

private def lastTermInfo? (tree : Elab.InfoTree) (startPos endPos : String.Pos.Raw) :
    Option (Elab.ContextInfo × Elab.TermInfo) :=
  let candidates := tree.foldInfo (init := #[]) fun ctx info acc =>
    match info with
    | .ofTermInfo termInfo =>
      if infoInRange (.ofTermInfo termInfo) startPos endPos then
        acc.push (ctx, termInfo)
      else
        acc
    | _ => acc
  candidates.foldl
    (init := none)
    fun best? candidate =>
      match best? with
      | none => some candidate
      | some best =>
        let candidatePos := candidate.2.stx.getTailPos? (canonicalOnly := true) |>.getD 0
        let bestPos := best.2.stx.getTailPos? (canonicalOnly := true) |>.getD 0
        if candidatePos > bestPos then some candidate else best?

private def prettyToString (fmt : Format) : String :=
  fmt.pretty

private def localDeclJson (userName : Name) (typeText : String) (binderInfo : BinderInfo) (valueText? : Option String) : Json :=
  Json.mkObj <|
    [ ("name", Json.str userName.toString)
    , ("binder_info", Json.str (repr binderInfo |>.pretty))
    , ("type", Json.str typeText)
    ] ++
      (match valueText? with
      | some valueText => [("value", Json.str valueText)]
      | none => [])

private def collectLocalContextMeta (lctx : LocalContext) : MetaM (Array Json) :=
  lctx.foldlM (init := #[]) fun acc localDecl => do
    if localDecl.isImplementationDetail then
      pure acc
    else
      let typeText := prettyToString (← Meta.ppExpr localDecl.type)
      let valueText? ←
        match localDecl.value? with
        | some value => pure <| some <| prettyToString (← Meta.ppExpr value)
        | none => pure none
      pure <| acc.push (localDeclJson localDecl.userName typeText localDecl.binderInfo valueText?)

private def collectLocalContextJson (ctx : Elab.ContextInfo) (lctx : LocalContext) : IO (Array Json) :=
  ctx.runMetaM lctx (collectLocalContextMeta lctx)

private def tacticLocalContextResult (target : ProofTarget) (ctx : Elab.ContextInfo) (tacticInfo : Elab.TacticInfo) : IO Json := do
  let entry := target.entry
  let ctxBefore := { ctx with mctx := tacticInfo.mctxBefore }
  let locals ←
    ctxBefore.runMetaM {} do
      match tacticInfo.goalsBefore with
      | goal :: _ =>
        goal.withContext do
          let decl ← goal.getDecl
          collectLocalContextMeta decl.lctx
      | [] => pure #[]
  pure <|
    Json.mkObj
      [ ("source", Json.str "tactic-goals-before")
      , ("declaration_name", Json.str entry.fullName.toString)
      , ("namespace", namespaceJson entry.namespaceName)
      , ("locals", Json.arr locals)
      ]

private def termLocalContextResult (target : ProofTarget) (ctx : Elab.ContextInfo) (termInfo : Elab.TermInfo) : IO Json := do
  let entry := target.entry
  let locals ← collectLocalContextJson ctx termInfo.lctx
  pure <|
    Json.mkObj
      [ ("source", Json.str "term-info")
      , ("declaration_name", Json.str entry.fullName.toString)
      , ("namespace", namespaceJson entry.namespaceName)
      , ("locals", Json.arr locals)
      ]

private def tacticGoalSnapshotResult (target : ProofTarget) (ctx : Elab.ContextInfo) (tacticInfo : Elab.TacticInfo) : IO Json := do
  let entry := target.entry
  let ctxBefore := { ctx with mctx := tacticInfo.mctxBefore }
  let prettyGoals ← ctxBefore.ppGoals tacticInfo.goalsBefore
  pure <|
    Json.mkObj
      [ ("source", Json.str "tactic-goals-before")
      , ("declaration_name", Json.str entry.fullName.toString)
      , ("namespace", namespaceJson entry.namespaceName)
      , ("goals", Json.arr #[Json.str (prettyToString prettyGoals)])
      ]

private def termGoalSnapshotResult (target : ProofTarget) (ctx : Elab.ContextInfo) (termInfo : Elab.TermInfo) : IO Json := do
  let entry := target.entry
  let goalText ←
    termInfo.runMetaM ctx do
      match termInfo.expectedType? with
      | some expectedType => return prettyToString (← Meta.ppExpr expectedType)
      | none => return prettyToString (← Meta.ppExpr (← Meta.inferType termInfo.expr))
  pure <|
    Json.mkObj
      [ ("source", Json.str "term-info")
      , ("declaration_name", Json.str entry.fullName.toString)
      , ("namespace", namespaceJson entry.namespaceName)
      , ("goals", Json.arr #[Json.str goalText])
      ]

private def insertionPointResult (catalog : DeclarationCatalog) (target : ProofTarget) : Json :=
  let loaded := catalog.loaded
  let entry := target.entry
  let fileMap := loaded.inputCtx.fileMap
  let insertPos := entry.declarationStx.getPos? (canonicalOnly := true) |>.getD 0
  Json.mkObj
    [ ("kind", Json.str "before-theorem")
    , ("declaration_name", Json.str entry.fullName.toString)
    , ("namespace", namespaceJson entry.namespaceName)
    , ("insert_position", pointJson fileMap insertPos)
    ]

private def tacticProofStateResult
    (catalog : DeclarationCatalog)
    (target : ProofTarget)
    (ctx : Elab.ContextInfo)
    (tacticInfo : Elab.TacticInfo) : IO Json := do
  let entry := target.entry
  let ctxBefore := { ctx with mctx := tacticInfo.mctxBefore }
  let locals ←
    ctxBefore.runMetaM {} do
      match tacticInfo.goalsBefore with
      | goal :: _ =>
        goal.withContext do
          let decl ← goal.getDecl
          collectLocalContextMeta decl.lctx
      | [] => pure #[]
  let prettyGoals ← ctxBefore.ppGoals tacticInfo.goalsBefore
  pure <|
    Json.mkObj
      [ ("source", Json.str "tactic-goals-before")
      , ("declaration_name", Json.str entry.fullName.toString)
      , ("namespace", namespaceJson entry.namespaceName)
      , ("locals", Json.arr locals)
      , ("goals", Json.arr #[Json.str (prettyToString prettyGoals)])
      , ("insertion_point", insertionPointResult catalog target)
      ]

private def tacticProofStateAfterResult
    (catalog : DeclarationCatalog)
    (target : ProofTarget)
    (ctx : Elab.ContextInfo)
    (tacticInfo : Elab.TacticInfo) : IO Json := do
  let entry := target.entry
  let ctxAfter := { ctx with mctx := tacticInfo.mctxAfter }
  let locals ←
    ctxAfter.runMetaM {} do
      match tacticInfo.goalsAfter with
      | goal :: _ =>
        goal.withContext do
          let decl ← goal.getDecl
          collectLocalContextMeta decl.lctx
      | [] => pure #[]
  let goalsText ←
    if tacticInfo.goalsAfter.isEmpty then
      pure #[]
    else
      let prettyGoals ← ctxAfter.ppGoals tacticInfo.goalsAfter
      pure #[Json.str (prettyToString prettyGoals)]
  pure <|
    Json.mkObj
      [ ("source", Json.str "tactic-goals-after-last")
      , ("declaration_name", Json.str entry.fullName.toString)
      , ("namespace", namespaceJson entry.namespaceName)
      , ("locals", Json.arr locals)
      , ("goals", Json.arr goalsText)
      , ("insertion_point", insertionPointResult catalog target)
      ]

private def termProofStateResult
    (catalog : DeclarationCatalog)
    (target : ProofTarget)
    (ctx : Elab.ContextInfo)
    (termInfo : Elab.TermInfo) : IO Json := do
  let entry := target.entry
  let locals ← collectLocalContextJson ctx termInfo.lctx
  let goalText ←
    termInfo.runMetaM ctx do
      match termInfo.expectedType? with
      | some expectedType => return prettyToString (← Meta.ppExpr expectedType)
      | none => return prettyToString (← Meta.ppExpr (← Meta.inferType termInfo.expr))
  pure <|
    Json.mkObj
      [ ("source", Json.str "term-info")
      , ("declaration_name", Json.str entry.fullName.toString)
      , ("namespace", namespaceJson entry.namespaceName)
      , ("locals", Json.arr locals)
      , ("goals", Json.arr #[Json.str goalText])
      , ("insertion_point", insertionPointResult catalog target)
      ]

private def probeFromInfoTree
    (catalog : DeclarationCatalog)
    (target : ProofTarget)
    (onTactic : Elab.ContextInfo → Elab.TacticInfo → IO Json)
    (onTerm : Elab.ContextInfo → Elab.TermInfo → IO Json) : IO (Except ProbeFailure Json) := do
  let loaded := catalog.loaded
  let entry := target.entry
  let fileMap := loaded.inputCtx.fileMap
  let some bodyStart := entry.bodyStx.getPos? (canonicalOnly := true)
    | return .error { kind := .unavailable, message := "target theorem body is missing canonical position data" }
  let some bodyEnd := entry.bodyStx.getTailPos? (canonicalOnly := true)
    | return .error { kind := .unavailable, message := "target theorem body is missing canonical end position data" }
  let some infoTree := entry.snapshot.elabSnap.infoTreeSnap.get.infoTree?
    | return .error { kind := .unavailable, message := "no info tree is available for the target theorem" }
  if let some (ctx, tacticInfo) := firstTacticInfo? infoTree bodyStart bodyEnd then
    return .ok (← onTactic ctx tacticInfo)
  if let some (ctx, termInfo) := firstTermInfo? infoTree bodyStart bodyEnd then
    return .ok (← onTerm ctx termInfo)
  let anchor := fileMap.toPosition bodyStart
  return .error
    { kind := .unavailable
    , message := s!"no tactic or term info was found near {anchor.line}:{anchor.column}"
    }

private def probeFromLastInfoTree
    (catalog : DeclarationCatalog)
    (target : ProofTarget)
    (onTactic : Elab.ContextInfo → Elab.TacticInfo → IO Json)
    (onTerm : Elab.ContextInfo → Elab.TermInfo → IO Json) : IO (Except ProbeFailure Json) := do
  let loaded := catalog.loaded
  let entry := target.entry
  let fileMap := loaded.inputCtx.fileMap
  let some bodyStart := entry.bodyStx.getPos? (canonicalOnly := true)
    | return .error { kind := .unavailable, message := "target theorem body is missing canonical position data" }
  let some bodyEnd := entry.bodyStx.getTailPos? (canonicalOnly := true)
    | return .error { kind := .unavailable, message := "target theorem body is missing canonical end position data" }
  let some infoTree := entry.snapshot.elabSnap.infoTreeSnap.get.infoTree?
    | return .error { kind := .unavailable, message := "no info tree is available for the target theorem" }
  if let some (ctx, tacticInfo) := lastTacticInfo? infoTree bodyStart bodyEnd then
    return .ok (← onTactic ctx tacticInfo)
  if let some (ctx, termInfo) := lastTermInfo? infoTree bodyStart bodyEnd then
    return .ok (← onTerm ctx termInfo)
  let anchor := fileMap.toPosition bodyStart
  return .error
    { kind := .unavailable
    , message := s!"no tactic or term info was found near {anchor.line}:{anchor.column}"
    }

def contextBundleResult (catalog : DeclarationCatalog) (target : ProofTarget) : IO (Except ProbeFailure Json) := do
  let some declarationTarget := declarationTargetResult? catalog target
    | return Except.error
        { kind := .unavailable
        , message := "target declaration is missing canonical span data"
        }
  match ← probeFromInfoTree catalog target
      (fun ctx tacticInfo => tacticProofStateResult catalog target ctx tacticInfo)
      (fun ctx termInfo => termProofStateResult catalog target ctx termInfo) with
  | .ok proofState =>
    return Except.ok <|
      Json.mkObj
        [ ("declaration_target", declarationTarget)
        , ("proof_state", proofState)
        ]
  | .error failure => return Except.error failure

private def withLoadedResolvedProofQuery
    (loaded : LoadedFile)
    (theoremName commandName : String)
    (onTarget : DeclarationCatalog → ProofTarget → IO (Except ProbeFailure Json)) : IO Json := do
  withLoadedResolvedProofTarget loaded theoremName commandName fun catalog target => do
    let filePath := toString loaded.filePath
    match ← onTarget catalog target with
    | .ok payload => pure <| jsonOk commandName filePath theoremName payload
    | .error failure => pure <| jsonFromFailure commandName filePath theoremName failure

private def withResolvedProofQuery
    (workspaceRoot filePath theoremName commandName : String)
    (onTarget : DeclarationCatalog → ProofTarget → IO (Except ProbeFailure Json)) : IO Json := do
  withResolvedProofTarget workspaceRoot filePath theoremName commandName fun catalog target => do
    match ← onTarget catalog target with
    | .ok payload => pure <| jsonOk commandName filePath theoremName payload
    | .error failure => pure <| jsonFromFailure commandName filePath theoremName failure

def localContextCommand (workspaceRoot filePath theoremName : String) : IO Json :=
  withResolvedProofQuery workspaceRoot filePath theoremName "local-context" fun catalog target =>
    probeFromInfoTree catalog target
      (fun ctx tacticInfo => tacticLocalContextResult target ctx tacticInfo)
      (fun ctx termInfo => termLocalContextResult target ctx termInfo)

def localContextFromLoaded (loaded : LoadedFile) (theoremName : String) : IO Json :=
  withLoadedResolvedProofQuery loaded theoremName "local-context" fun catalog target =>
    probeFromInfoTree catalog target
      (fun ctx tacticInfo => tacticLocalContextResult target ctx tacticInfo)
      (fun ctx termInfo => termLocalContextResult target ctx termInfo)

def goalSnapshotCommand (workspaceRoot filePath theoremName : String) : IO Json :=
  withResolvedProofQuery workspaceRoot filePath theoremName "goal-snapshot" fun catalog target =>
    probeFromInfoTree catalog target
      (fun ctx tacticInfo => tacticGoalSnapshotResult target ctx tacticInfo)
      (fun ctx termInfo => termGoalSnapshotResult target ctx termInfo)

def goalSnapshotFromLoaded (loaded : LoadedFile) (theoremName : String) : IO Json :=
  withLoadedResolvedProofQuery loaded theoremName "goal-snapshot" fun catalog target =>
    probeFromInfoTree catalog target
      (fun ctx tacticInfo => tacticGoalSnapshotResult target ctx tacticInfo)
      (fun ctx termInfo => termGoalSnapshotResult target ctx termInfo)

def proofStateCommand (workspaceRoot filePath theoremName : String) : IO Json :=
  withResolvedProofQuery workspaceRoot filePath theoremName "proof-state" fun catalog target =>
    probeFromInfoTree catalog target
      (fun ctx tacticInfo => tacticProofStateResult catalog target ctx tacticInfo)
      (fun ctx termInfo => termProofStateResult catalog target ctx termInfo)

def proofStateFromLoaded (loaded : LoadedFile) (theoremName : String) : IO Json :=
  withLoadedResolvedProofQuery loaded theoremName "proof-state" fun catalog target =>
    probeFromInfoTree catalog target
      (fun ctx tacticInfo => tacticProofStateResult catalog target ctx tacticInfo)
      (fun ctx termInfo => termProofStateResult catalog target ctx termInfo)

def contextBundleCommand (workspaceRoot filePath theoremName : String) : IO Json :=
  withResolvedProofQuery workspaceRoot filePath theoremName "context-bundle" fun catalog target =>
    contextBundleResult catalog target

def contextBundleFromLoaded (loaded : LoadedFile) (theoremName : String) : IO Json :=
  withLoadedResolvedProofQuery loaded theoremName "context-bundle" fun catalog target =>
    contextBundleResult catalog target

def proofStateAfterLastTacticCommand (workspaceRoot filePath theoremName : String) : IO Json :=
  withResolvedProofQuery workspaceRoot filePath theoremName "proof-state-after-last-tactic" fun catalog target =>
    probeFromLastInfoTree catalog target
      (fun ctx tacticInfo => tacticProofStateAfterResult catalog target ctx tacticInfo)
      (fun ctx termInfo => termProofStateResult catalog target ctx termInfo)

def proofStateAfterLastTacticFromLoaded (loaded : LoadedFile) (theoremName : String) : IO Json :=
  withLoadedResolvedProofQuery loaded theoremName "proof-state-after-last-tactic" fun catalog target =>
    probeFromLastInfoTree catalog target
      (fun ctx tacticInfo => tacticProofStateAfterResult catalog target ctx tacticInfo)
      (fun ctx termInfo => termProofStateResult catalog target ctx termInfo)

def insertionPointCommand (workspaceRoot filePath theoremName : String) : IO Json :=
  withResolvedProofTarget workspaceRoot filePath theoremName "insertion-point" fun catalog target =>
    pure <| jsonOk "insertion-point" filePath theoremName (insertionPointResult catalog target)

def insertionPointFromLoaded (loaded : LoadedFile) (theoremName : String) : IO Json := do
  let filePath := toString loaded.filePath
  withLoadedResolvedProofTarget loaded theoremName "insertion-point" fun catalog target =>
    pure <| jsonOk "insertion-point" filePath theoremName (insertionPointResult catalog target)

end Tools.SemanticProbe
