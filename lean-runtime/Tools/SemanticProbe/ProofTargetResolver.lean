import Lean
import Tools.SemanticProbe.DeclarationCatalog
import Tools.SemanticProbe.LoadedFile
import Tools.SemanticProbe.Protocol

namespace Tools.SemanticProbe

open Lean

/-- One proof-workflow target resolved from the source catalog. -/
structure ProofTarget where
  entry : DeclarationCatalogEntry

private def loadedEnvironment? (loaded : LoadedFile) : Option Environment := do
  let lastSnap ← loaded.commands.back?
  some lastSnap.elabSnap.resultSnap.get.cmdState.env

private def proofDeclarationInEnv (env : Environment) (fullName : Name) : Bool :=
  match env.find? fullName with
  | some (.thmInfo _) => true
  | _ => false

private def proofDeclaration? (loaded : LoadedFile) (entry : DeclarationCatalogEntry) : Bool :=
  match loadedEnvironment? loaded with
  | some env => proofDeclarationInEnv env entry.fullName
  | none => false

private def unsupportedProofTargetMessage (declarationName : Name) (declarationKind : String) : String :=
  s!"`{declarationName}` is a `{declarationKind}` declaration. `declaration-inventory` and `workspace-declaration-index` expose declaration metadata, but proof-target workflows only support proof declarations with editable proof bodies"

/-- Render the declaration-target JSON contract for one resolved proof target.

The payload intentionally duplicates `body_span` under `safe_edit`. The top-level
field is convenient for callers that only need the body. The nested field
preserves the stronger statement that the body span is part of an explicit
"replace-body" edit contract, not just an arbitrary range.
-/
def declarationTargetResult? (catalog : DeclarationCatalog) (target : ProofTarget) : Option Json := do
  let entry := target.entry
  let spans ← declarationSpans? catalog entry
  let safeEditJson :=
    Json.mkObj
      [ ("kind", Json.str "replace-body")
      , ("declaration_span", spans.declarationSpan)
      , ("body_span", spans.bodySpan)
      ]
  pure <|
    Json.mkObj
      [ ("module_path", Json.str entry.modulePath)
      , ("short_name", Json.str entry.shortName.toString)
      , ("declaration_name", Json.str entry.fullName.toString)
      , ("namespace", namespaceJson entry.namespaceName)
      , ("namespace_stack", Json.arr <| entry.namespaceName.components.toArray.map (Json.str ·.toString))
      , ("imports", Json.arr <| catalog.loaded.imports.map Json.str)
      , ("keyword", Json.str entry.keyword)
      , ("modifiers", Json.arr <| entry.modifiers.map Json.str)
      , ("group", declarationGroupJson entry)
      , ("declaration_span", spans.declarationSpan)
      , ("name_span", spans.nameSpan)
      , ("body_span", spans.bodySpan)
      , ("safe_edit", safeEditJson)
      , ("proof_state_available", Json.bool true)
      ]

def resolveProofTarget
    (catalog : DeclarationCatalog)
    (commandName targetName : String) : Prod Json (Option ProofTarget) :=
  let (result, entry?) := resolveDeclarationEntry catalog commandName targetName
  match entry? with
  | none => (result, none)
  | some entry =>
    if proofDeclaration? catalog.loaded entry then
      (result, some { entry })
    else
      ( jsonUnavailable
          commandName
          (toString catalog.loaded.filePath)
          targetName
          (unsupportedProofTargetMessage entry.fullName entry.declarationKind)
      , none
      )

def withLoadedResolvedProofTarget
    (loaded : LoadedFile)
    (theoremName commandName : String)
    (onTarget : DeclarationCatalog → ProofTarget → IO Json) : IO Json := do
  let catalog := buildDeclarationCatalog loaded
  let (result, target?) := resolveProofTarget catalog commandName theoremName
  match target? with
  | none => pure result
  | some target => onTarget catalog target

def withResolvedProofTarget
    (workspaceRoot filePath theoremName commandName : String)
    (onTarget : DeclarationCatalog → ProofTarget → IO Json) : IO Json := do
  match ← loadFile workspaceRoot filePath with
  | .error failure => pure <| jsonFromFailure commandName filePath theoremName failure
  | .ok loaded => withLoadedResolvedProofTarget loaded theoremName commandName onTarget

def declarationTargetCommand (workspaceRoot filePath theoremName : String) : IO Json :=
  withResolvedProofTarget workspaceRoot filePath theoremName "declaration-target" fun catalog target =>
    match declarationTargetResult? catalog target with
    | some payload => pure <| jsonOk "declaration-target" filePath theoremName payload
    | none => pure <| jsonUnavailable "declaration-target" filePath theoremName "target declaration is missing canonical span data"

def declarationTargetFromLoaded (loaded : LoadedFile) (theoremName : String) : IO Json := do
  let filePath := toString loaded.filePath
  withLoadedResolvedProofTarget loaded theoremName "declaration-target" fun catalog target =>
    match declarationTargetResult? catalog target with
    | some payload => pure <| jsonOk "declaration-target" filePath theoremName payload
    | none =>
      pure <| jsonUnavailable "declaration-target" filePath theoremName "target declaration is missing canonical span data"

end Tools.SemanticProbe
