import Lean

namespace Tools.SemanticProbe

open Lean

inductive ProbeFailureKind where
  | unavailable
  | error

structure ProbeFailure where
  kind : ProbeFailureKind
  message : String

def schemaVersion : Json := Json.str "probe.v2"

def pointJson (fileMap : FileMap) (pos : String.Pos.Raw) : Json :=
  let p := fileMap.toPosition pos
  Json.mkObj [("line", Json.num p.line), ("column", Json.num p.column)]

def spanJson (fileMap : FileMap) (startPos endPos : String.Pos.Raw) : Json :=
  Json.mkObj [("start", pointJson fileMap startPos), ("end", pointJson fileMap endPos)]

def namespaceJson (name : Name) : Json :=
  if name.isAnonymous then Json.null else Json.str name.toString

def jsonStatus (command status filePath theoremName : String) (fields : Array (String × Json) := #[]) : Json :=
  Json.mkObj <|
    [ ("schema_version", schemaVersion)
    , ("layer", Json.str "semantic")
    , ("command", Json.str command)
    , ("status", Json.str status)
    , ("file", Json.str filePath)
    , ("theorem", Json.str theoremName)
    ] ++ fields.toList

def jsonOk (command filePath theoremName : String) (result : Json) : Json :=
  jsonStatus command "ok" filePath theoremName #[("result", result)]

def jsonError (command filePath theoremName message : String) : Json :=
  jsonStatus command "error" filePath theoremName #[("message", Json.str message)]

def jsonUnavailable (command filePath theoremName message : String) : Json :=
  jsonStatus command "unavailable" filePath theoremName #[("message", Json.str message)]

def jsonFromFailure (command filePath theoremName : String) (failure : ProbeFailure) : Json :=
  match failure.kind with
  | .unavailable => jsonUnavailable command filePath theoremName failure.message
  | .error => jsonError command filePath theoremName failure.message

def exitCodeForPayload (payload : Json) : UInt32 :=
  let status := payload.getObjValAs? String "status" |>.toOption |>.getD "error"
  if status == "error" then 4 else 0

end Tools.SemanticProbe
