import Lean
import Lean.Elab
import Lean.Server.InfoUtils
import Tools.SemanticProbe.DeclarationCatalog
import Tools.SemanticProbe.LoadedFile
import Tools.SemanticProbe.Protocol

namespace Tools.SemanticProbe

open Lean
open Lean.Elab

def declarationInventoryEntries (catalog : DeclarationCatalog) : Array Json :=
  catalog.entries.foldl
    (init := #[])
    fun acc entry =>
      match declarationMetadataResult? catalog entry with
      | some payload => acc.push payload
      | none => acc

private def declarationInventoryResult (catalog : DeclarationCatalog) : Json :=
  Json.mkObj [("entries", Json.arr (declarationInventoryEntries catalog))]

def declarationInventoryFromLoaded (loaded : LoadedFile) (targetName : String) : Json :=
  let catalog := buildDeclarationCatalog loaded
  jsonOk "declaration-inventory" (toString loaded.filePath) targetName (declarationInventoryResult catalog)

def declarationInventoryCommand (workspaceRoot filePath targetName : String) : IO Json := do
  match ← loadFile workspaceRoot filePath with
  | .error failure => pure <| jsonFromFailure "declaration-inventory" filePath targetName failure
  | .ok loaded => pure <| declarationInventoryFromLoaded loaded targetName

private def parseFileManifest (manifestPath : System.FilePath) : IO (Except String (Array String)) := do
  let manifestText ← IO.FS.readFile manifestPath
  match Json.parse manifestText with
  | Except.error err => pure <| Except.error s!"could not parse frontier manifest: {err}"
  | Except.ok json =>
    match json with
    | Json.arr rows =>
      let mut filePaths := #[]
      for row in rows do
        match row with
        | Json.str filePath => filePaths := filePaths.push filePath
        | _ => return Except.error "frontier manifest must be one JSON array of file paths"
      pure <| Except.ok filePaths
    | _ => pure <| Except.error "frontier manifest must be one JSON array of file paths"

def batchDeclarationInventoryCommand
    (workspaceRoot filePath targetName manifestPath : String) : IO Json := do
  match ← parseFileManifest manifestPath with
  | Except.error message =>
    pure <| jsonError "batch-declaration-inventory" filePath targetName message
  | Except.ok frontierFiles =>
    let mut files := #[]
    for frontierFile in frontierFiles do
      let loaded ←
        match ← loadFile workspaceRoot frontierFile with
        | .error failure =>
          return jsonFromFailure "batch-declaration-inventory" frontierFile targetName failure
        | .ok loaded => pure loaded
      files := files.push <|
        Json.mkObj
          [ ("file_path", Json.str frontierFile)
          , ("entries", Json.arr (declarationInventoryEntries (buildDeclarationCatalog loaded)))
          ]
    pure <|
      jsonOk "batch-declaration-inventory" filePath targetName (Json.mkObj [("files", Json.arr files)])

structure WorkspaceModuleManifestEntry where
  filePath : String
  modulePath : String

private def parseWorkspaceModuleManifest (manifestPath : System.FilePath) :
    IO (Except String (Array WorkspaceModuleManifestEntry)) := do
  let manifestText ← IO.FS.readFile manifestPath
  match Json.parse manifestText with
  | Except.error err => pure <| Except.error s!"could not parse workspace module manifest: {err}"
  | Except.ok json =>
    match json with
    | Json.arr rows =>
      let mut entries := #[]
      for row in rows do
        match row.getObjValAs? String "file_path", row.getObjValAs? String "module_path" with
        | Except.ok filePath, Except.ok modulePath =>
          entries := entries.push { filePath, modulePath }
        | _, _ =>
          return Except.error "workspace module manifest rows must contain string `file_path` and `module_path`"
      pure <| Except.ok entries
    | _ => pure <| Except.error "workspace module manifest must be one JSON array of objects"

private def dottedName (text : String) : Name :=
  (text.splitOn ".").foldl
    (init := Name.anonymous)
    (fun current segment =>
      if segment.isEmpty then current else Name.str current segment)

private def declarationRangeJson (range : DeclarationRange) : Json :=
  Json.mkObj
    [ ("start", Json.mkObj [("line", Json.num range.pos.line), ("column", Json.num range.pos.column)])
    , ("end", Json.mkObj [("line", Json.num range.endPos.line), ("column", Json.num range.endPos.column)])
    ]

private def workspaceIndexEntryJson
    (filePath modulePath : String)
    (declName : Name)
    (declarationKind : String)
    (typeText : String)
    (range : DeclarationRange) : Json :=
  let namespaceName := declName.getPrefix
  Json.mkObj
    [ ("file_path", Json.str filePath)
    , ("module_path", Json.str modulePath)
    , ("declaration_name", Json.str declName.toString)
    , ("short_name", Json.str declName.getString!)
    , ("namespace", namespaceJson namespaceName)
    , ("declaration_kind", Json.str declarationKind)
    , ("declaration_span", declarationRangeJson range)
    , ("type_text", Json.str typeText)
    ]

private def importWorkspaceModules (modules : Array WorkspaceModuleManifestEntry) : IO Environment := do
  let imports := modules.map (fun entry => Import.mk (dottedName entry.modulePath) false true false)
  importModules imports Options.empty (leakEnv := true) (loadExts := true)

private def collectWorkspaceDeclarationIndexEntries
    (modules : Array WorkspaceModuleManifestEntry) : MetaM (Array Json) := do
  let env ← getEnv
  let mut entries := #[]
  for moduleEntry in modules do
    let moduleName := dottedName moduleEntry.modulePath
    let some modIdx := env.header.moduleNames.idxOf? moduleName | continue
    let moduleData := env.header.moduleData[modIdx]!
    for h : idx in *...moduleData.constNames.size do
      let declName := moduleData.constNames[idx]
      let constInfo := moduleData.constants[idx]!
      let some range ← findDeclarationRanges? declName | continue
      match constInfo with
      | .thmInfo theoremInfo =>
        let typeText := (← Meta.ppExpr theoremInfo.type).pretty
        entries := entries.push <|
          workspaceIndexEntryJson
            moduleEntry.filePath
            moduleEntry.modulePath
            declName
            "theorem"
            typeText
            range.range
      | .defnInfo defnInfo =>
        let typeText := (← Meta.ppExpr defnInfo.type).pretty
        let declarationKind := if defnInfo.hints.isAbbrev then "abbrev" else "def"
        entries := entries.push <|
          workspaceIndexEntryJson
            moduleEntry.filePath
            moduleEntry.modulePath
            declName
            declarationKind
            typeText
            range.range
      | .opaqueInfo opaqueInfo =>
        let typeText := (← Meta.ppExpr opaqueInfo.type).pretty
        entries := entries.push <|
          workspaceIndexEntryJson
            moduleEntry.filePath
            moduleEntry.modulePath
            declName
            "opaque"
            typeText
            range.range
      | _ => pure ()
  pure entries

def workspaceDeclarationIndexCommand
    (workspaceRoot filePath targetName manifestPath : String) : IO Json := do
  let _ := workspaceRoot
  match ← parseWorkspaceModuleManifest manifestPath with
  | Except.error message =>
    pure <| jsonError "workspace-declaration-index" filePath targetName message
  | Except.ok modules =>
    let env ← importWorkspaceModules modules
    let coreCtx : Core.Context := {
      fileName := "<workspace-declaration-index>"
      fileMap := default
      options := Options.empty
    }
    let (entries, _, _) ← Meta.MetaM.toIO (collectWorkspaceDeclarationIndexEntries modules) coreCtx { env := env } {} {}
    pure <| jsonOk "workspace-declaration-index" filePath targetName (Json.mkObj [("entries", Json.arr entries)])

end Tools.SemanticProbe
