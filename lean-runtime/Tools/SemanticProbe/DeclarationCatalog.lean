import Lean
import Lean.Elab
import Lean.Language.Lean
import Tools.SemanticProbe.LoadedFile
import Tools.SemanticProbe.Protocol

namespace Tools.SemanticProbe

open Lean
open Lean.Elab
open Lean.Language
open Lean.Language.Lean

private inductive ScopeKind where
  | section
  | namespace
  | mutual

private structure ScopeFrame where
  kind : ScopeKind
  namespaceBefore : Name

/-- State for the currently open `mutual` block.

`nextMemberIndex` is 1-based because the JSON contract is operator-facing and we
want "member 1/2/3" semantics rather than array indexing semantics.
-/
private structure ActiveMutualGroup where
  groupId : String
  nextMemberIndex : Nat

/-- One declaration entry from the source catalog. -/
structure DeclarationCatalogEntry where
  commandIndex : Nat
  shortName : Name
  fullName : Name
  modulePath : String
  namespaceName : Name
  keyword : String
  declarationKind : String
  modifiers : Array String
  groupKind : String
  groupId : Option String
  groupMemberIndex : Option Nat
  declarationStx : Syntax
  nameStx : Syntax
  commandStx : Syntax
  bodyStx : Syntax
  snapshot : CommandParsedSnapshot

/-- Source declaration catalog for one loaded file. -/
structure DeclarationCatalog where
  loaded : LoadedFile
  entries : Array DeclarationCatalogEntry

/-- The source spans that later catalog and target payloads reuse. -/
structure DeclarationCatalogSpans where
  declarationSpan : Json
  nameSpan : Json
  bodySpan : Json

private partial def firstIdent? (stx : Syntax) : Option Syntax :=
  if stx.isIdent then
    some stx
  else
    stx.getArgs.findSome? firstIdent?

private def declIdNameStx (stx : Syntax) : Syntax :=
  if stx.isOfKind ``Lean.Parser.Command.instance then
    if let some identStx := firstIdent? stx[3] then
      identStx
    else
      stx[1]
  else if let some identStx := firstIdent? stx[1] then
    identStx
  else
    stx[0]

private def lemmaSyntaxKind : SyntaxNodeKind :=
  Name.mkSimple "lemma"

/-- Return one source declaration node that belongs in the source catalog.

The catalog is broader than proof workflows. It should keep ordinary parser
declarations whose source facts and spans are available from the loaded command
snapshot chain, then let `ProofTargetResolver` decide which of those entries are
editable proof targets.
-/
private def catalogDeclarationSyntax? (stx : Syntax) : Option Syntax :=
  if stx.getKind == ``Lean.Parser.Command.declaration then
    let inner := stx[1]
    if inner.getKind == ``Lean.Parser.Command.theorem ||
        inner.getKind == ``Lean.Parser.Command.definition ||
        inner.getKind == ``Lean.Parser.Command.abbrev ||
        inner.getKind == ``Lean.Parser.Command.opaque ||
        inner.getKind == ``Lean.Parser.Command.instance then
      some inner
    else
      none
  else if stx.getKind == lemmaSyntaxKind then
    some stx
  else
    none

private partial def declarationBodySyntax? (stx : Syntax) : Option Syntax :=
  if stx.getKind == ``Lean.Parser.Command.instance && stx.getNumArgs >= 6 then
    some stx[5]
  else if stx.getKind == ``Lean.Parser.Command.declValSimple && stx.getNumArgs >= 2 then
    some stx[1]
  else
    stx.getArgs.findSome? declarationBodySyntax?

/-- Extract declaration nodes from one command snapshot.

Lean exposes the first member of a `mutual` block as a `Command.mutual` wrapper
and later members as ordinary `Command.declaration` nodes. The catalog flattens
that asymmetry here so later code can reason in terms of declaration members
instead of parser presentation details.
-/
private def declarationSyntaxes (stx : Syntax) : Array Syntax :=
  if catalogDeclarationSyntax? stx |>.isSome then
    #[stx]
  else if stx.getKind == ``Lean.Parser.Command.mutual && stx.getNumArgs > 1 then
    stx[1].getArgs.foldl
      (init := #[])
      fun declarations child =>
        if catalogDeclarationSyntax? child |>.isSome then
          declarations.push child
        else
          declarations
  else
    #[]

private partial def collectAtomValues (stx : Syntax) : Array String :=
  let children := stx.getArgs.foldl (init := #[]) fun values child => values ++ collectAtomValues child
  if stx.isAtom then
    children.push stx.getAtomVal
  else
    children

/-- Recover declaration modifiers from the parser subtree.

We collect atom values instead of matching parser node kinds because Lean's
modifier grammar is spread across several helper productions. The JSON contract
only needs the surface modifier words, not the exact parser node that produced
them.
-/
private def declarationModifiers (declStx : Syntax) : Array String :=
  let modifiersStx :=
    if declStx.getKind == ``Lean.Parser.Command.declaration && declStx.getNumArgs > 0 then
      declStx[0]
    else
      Syntax.missing
  let atoms := collectAtomValues modifiersStx
  let candidates : Array String := #["private", "protected", "scoped", "local", "unsafe", "partial", "noncomputable"]
  candidates.foldl
    (init := #[])
    fun modifiers name =>
      if atoms.contains name then modifiers.push name else modifiers

private def declarationKeyword (stx : Syntax) : String :=
  if stx.getKind == lemmaSyntaxKind then
    "lemma"
  else if stx.getKind == ``Lean.Parser.Command.instance then
    "instance"
  else if stx.getNumArgs > 0 && stx[0].isAtom then
    stx[0].getAtomVal
  else
    "theorem"

private def declarationKindOfSyntax (stx : Syntax) : String :=
  declarationKeyword stx

private def mkDeclarationCatalogEntry?
    (loaded : LoadedFile)
    (commandIndex : Nat)
    (currNamespace : Name)
    (snap : CommandParsedSnapshot)
    (declStx : Syntax)
    (group? : Option ActiveMutualGroup) : Option DeclarationCatalogEntry := do
  let stx ← catalogDeclarationSyntax? declStx
  let bodyStx ← declarationBodySyntax? stx
  let nameStx := declIdNameStx stx
  guard nameStx.isIdent
  let shortName := nameStx.getId
  let keyword := declarationKeyword stx
  let declarationKind := declarationKindOfSyntax stx
  let namespaceName := currNamespace
  let fullName := if currNamespace.isAnonymous then shortName else currNamespace ++ shortName
  pure
    { commandIndex
    , shortName
    , fullName
    , modulePath := loaded.modulePath
    , namespaceName
    , keyword
    , declarationKind
    , modifiers := declarationModifiers declStx
    , groupKind := if group?.isSome then "mutual" else "standalone"
    , groupId := group?.map (·.groupId)
    , groupMemberIndex := group?.map (·.nextMemberIndex)
    , declarationStx := declStx
    , nameStx
    , commandStx := declStx
    , bodyStx
    , snapshot := snap
    }

private def namespaceEntryName (stx : Syntax) : Name :=
  stx[1].getId

/-- Collect source declaration entries from the loaded command chain.

The catalog stays source-only. It preserves names, keywords, modifiers, spans,
and grouping facts from the current parsed file without deciding whether proof
workflows apply to those declarations.
-/
private def collectEntries (loaded : LoadedFile) : Array DeclarationCatalogEntry :=
  let (_, _, _, _, entries) :=
    loaded.commands.foldl
      (init := (0, Name.anonymous, ([] : List ScopeFrame), (none : Option ActiveMutualGroup), (#[] : Array DeclarationCatalogEntry)))
      fun (commandIndex, currNamespace, frames, activeMutual?, entries) snap =>
        if Parser.isTerminalCommand snap.stx then
          (commandIndex + 1, currNamespace, frames, activeMutual?, entries)
        else if snap.stx.getKind == ``Lean.Parser.Command.namespace then
          let nextNamespace :=
            if currNamespace.isAnonymous then
              namespaceEntryName snap.stx
            else
              currNamespace ++ namespaceEntryName snap.stx
          ( commandIndex + 1
          , nextNamespace
          , { kind := .namespace, namespaceBefore := currNamespace } :: frames
          , activeMutual?
          , entries
          )
        else if snap.stx.getKind == ``Lean.Parser.Command.section then
          ( commandIndex + 1
          , currNamespace
          , { kind := .section, namespaceBefore := currNamespace } :: frames
          , activeMutual?
          , entries
          )
        else if snap.stx.getKind == ``Lean.Parser.Command.mutual then
          let groupId :=
            match snap.stx.getPos? (canonicalOnly := true) with
            | some startPos =>
              let pos := loaded.inputCtx.fileMap.toPosition startPos
              s!"{loaded.modulePath}:mutual:{pos.line}:{pos.column}"
            | none => s!"{loaded.modulePath}:mutual:{commandIndex + 1}"
          let group : ActiveMutualGroup := { groupId, nextMemberIndex := 1 }
          let initialEntries : Array DeclarationCatalogEntry × Nat := (#[], group.nextMemberIndex)
          let mutualEntries :=
            (declarationSyntaxes snap.stx).foldl
              (init := initialEntries)
              fun (acc, nextMemberIndex) declStx =>
                let groupAtEntry : ActiveMutualGroup := { group with nextMemberIndex }
                match mkDeclarationCatalogEntry? loaded commandIndex currNamespace snap declStx (some groupAtEntry) with
                | some entry => (acc.push entry, nextMemberIndex + 1)
                | none => (acc, nextMemberIndex)
          ( commandIndex + 1
          , currNamespace
          , { kind := .mutual, namespaceBefore := currNamespace } :: frames
          , some { group with nextMemberIndex := mutualEntries.2 }
          , entries ++ mutualEntries.1
          )
        else if snap.stx.getKind == ``Lean.Parser.Command.end then
          match frames with
          | frame :: rest =>
            let nextMutual? :=
              match frame.kind with
              | .mutual => none
              | _ => activeMutual?
            (commandIndex + 1, frame.namespaceBefore, rest, nextMutual?, entries)
          | [] => (commandIndex + 1, currNamespace, frames, activeMutual?, entries)
        else
          let initialEntries : Array DeclarationCatalogEntry × Option Nat := (#[], activeMutual?.map (·.nextMemberIndex))
          let declarationEntries :=
            (declarationSyntaxes snap.stx).foldl
              (init := initialEntries)
              fun (acc, nextMemberIndex?) declStx =>
                let groupAtEntry? :=
                  match activeMutual?, nextMemberIndex? with
                  | some group, some nextMemberIndex => some { groupId := group.groupId, nextMemberIndex }
                  | _, _ => none
                match mkDeclarationCatalogEntry? loaded commandIndex currNamespace snap declStx groupAtEntry? with
                | some entry =>
                  let nextIndex? := nextMemberIndex?.map (· + 1)
                  (acc.push entry, nextIndex?)
                | none => (acc, nextMemberIndex?)
          let nextMutual? :=
            match activeMutual?, declarationEntries.2 with
            | some group, some nextMemberIndex => some { group with nextMemberIndex }
            | some group, none => some group
            | none, _ => none
          (commandIndex + 1, currNamespace, frames, nextMutual?, entries ++ declarationEntries.1)
  entries

def buildDeclarationCatalog (loaded : LoadedFile) : DeclarationCatalog :=
  { loaded, entries := collectEntries loaded }

/-- Compute the exclusive end position for one declaration body replacement.

We prefer the next catalog declaration start when one exists, because that is
the narrowest declaration boundary for grouped declarations. For the last member
of a `mutual` block we instead stop at the matching `end`, not at a later
declaration outside the group.
-/
private def declarationEndPos? (catalog : DeclarationCatalog) (entry : DeclarationCatalogEntry) : Option String.Pos.Raw :=
  let currentStart? := entry.declarationStx.getPos? (canonicalOnly := true)
  let nextEntry? :=
    catalog.entries.foldl
      (init := none)
      fun best? candidate =>
        let candidateStart? := candidate.declarationStx.getPos? (canonicalOnly := true)
        match currentStart?, candidateStart? with
        | some currentStart, some candidateStart =>
          if candidateStart <= currentStart then
            best?
          else
            match best? with
            | some best =>
              let bestStart := best.1
              if candidateStart < bestStart then
                some (candidateStart, candidate.groupId)
              else
                best?
            | none => some (candidateStart, candidate.groupId)
        | _, _ => best?
  let nextEnd? :=
    catalog.loaded.commands.foldl
      (init := none)
      fun best? snap =>
        let kind := snap.stx.getKind
        if kind != ``Lean.Parser.Command.end then
          best?
        else
          match currentStart?, snap.stx.getPos? (canonicalOnly := true) with
          | some currentStart, some candidateStart =>
            if candidateStart <= currentStart then
              best?
            else
              match best? with
              | some bestStart =>
                if candidateStart < bestStart then some candidateStart else best?
              | none => some candidateStart
          | _, _ => best?
  match entry.groupKind, nextEntry?, nextEnd? with
  | "mutual", some (nextEntryStart, some nextGroupId), _ =>
    if entry.groupId == some nextGroupId then
      some nextEntryStart
    else
      match nextEnd? with
      | some nextEnd => some nextEnd
      | none => some nextEntryStart
  | "mutual", some (nextEntryStart, none), _ =>
    match nextEnd? with
    | some nextEnd => some nextEnd
    | none => some nextEntryStart
  | "mutual", none, some nextEnd => some nextEnd
  | _, some (nextEntryStart, _), _ => some nextEntryStart
  | _, _, _ => entry.bodyStx.getTailPos? (canonicalOnly := true)

private def declarationBoundarySpanJson? (catalog : DeclarationCatalog) (entry : DeclarationCatalogEntry) : Option Json := do
  let startPos ← entry.declarationStx.getPos? (canonicalOnly := true)
  let endPos ← declarationEndPos? catalog entry
  pure <| spanJson catalog.loaded.inputCtx.fileMap startPos endPos

def declarationSpans? (catalog : DeclarationCatalog) (entry : DeclarationCatalogEntry) :
    Option DeclarationCatalogSpans := do
  let declarationSpan ← declarationBoundarySpanJson? catalog entry
  let nameStart ← entry.nameStx.getPos? (canonicalOnly := true)
  let nameEnd ← entry.nameStx.getTailPos? (canonicalOnly := true)
  let bodyStart ← entry.bodyStx.getPos? (canonicalOnly := true)
  let bodyEnd ← declarationEndPos? catalog entry
  pure
    { declarationSpan
    , nameSpan := spanJson catalog.loaded.inputCtx.fileMap nameStart nameEnd
    , bodySpan := spanJson catalog.loaded.inputCtx.fileMap bodyStart bodyEnd
    }

private def matchEntries (entries : Array DeclarationCatalogEntry) (targetName : String) : Array DeclarationCatalogEntry :=
  let fullMatches := entries.filter (fun entry => entry.fullName.toString == targetName)
  if !fullMatches.isEmpty then
    fullMatches
  else
    entries.filter (fun entry => entry.shortName.toString == targetName)

private def targetCandidateJson (catalog : DeclarationCatalog) (entry : DeclarationCatalogEntry) : Json := by
  let baseFields :=
    [ ("module_path", Json.str entry.modulePath)
    , ("short_name", Json.str entry.shortName.toString)
    , ("declaration_name", Json.str entry.fullName.toString)
    , ("namespace", namespaceJson entry.namespaceName)
    , ("namespace_stack", Json.arr <| entry.namespaceName.components.toArray.map (Json.str ·.toString))
    , ("declaration_kind", Json.str entry.declarationKind)
    ]
  let allFields :=
    match declarationBoundarySpanJson? catalog entry with
    | some span => baseFields ++ [("declaration_span", span)]
    | none => baseFields
  exact Json.mkObj allFields

/-- Resolve one operator-supplied name to zero, one, or many catalog entries.

Full-name matches win over short-name matches so Python can ask for an exact
declaration when it already knows the namespace, while CLI operators may still
use a short local name during ad hoc inspection.
-/
def resolveDeclarationEntry
    (catalog : DeclarationCatalog)
    (commandName targetName : String) : Prod Json (Option DeclarationCatalogEntry) :=
  match matchEntries catalog.entries targetName with
  | #[] =>
    (jsonStatus commandName "missing" (toString catalog.loaded.filePath) targetName #[], none)
  | #[entry] =>
    (jsonOk commandName (toString catalog.loaded.filePath) targetName (targetCandidateJson catalog entry), some entry)
  | manyEntries =>
    let candidates := Json.arr <| manyEntries.map (targetCandidateJson catalog)
    (jsonStatus commandName "ambiguous" (toString catalog.loaded.filePath) targetName #[("candidates", candidates)], none)

def declarationGroupJson (entry : DeclarationCatalogEntry) : Json :=
  Json.mkObj <|
    [ ("kind", Json.str entry.groupKind) ] ++
    (match entry.groupId with
    | some value => [("group_id", Json.str value)]
    | none => []) ++
    (match entry.groupMemberIndex with
    | some index => [("member_index", Json.num index)]
    | none => [])

def declarationMetadataResult? (catalog : DeclarationCatalog) (entry : DeclarationCatalogEntry) : Option Json := do
  let spans ← declarationSpans? catalog entry
  pure <|
    Json.mkObj
      [ ("module_path", Json.str entry.modulePath)
      , ("short_name", Json.str entry.shortName.toString)
      , ("declaration_name", Json.str entry.fullName.toString)
      , ("namespace", namespaceJson entry.namespaceName)
      , ("namespace_stack", Json.arr <| entry.namespaceName.components.toArray.map (Json.str ·.toString))
      , ("imports", Json.arr <| catalog.loaded.imports.map Json.str)
      , ("declaration_kind", Json.str entry.declarationKind)
      , ("modifiers", Json.arr <| entry.modifiers.map Json.str)
      , ("group", declarationGroupJson entry)
      , ("declaration_span", spans.declarationSpan)
      , ("name_span", spans.nameSpan)
      , ("body_span", spans.bodySpan)
      ]

end Tools.SemanticProbe
