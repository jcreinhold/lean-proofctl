import Lean

namespace Tools.SemanticProbe

open Lean

def liveSchemaVersion : Json := Json.str "probe.live.v1"

inductive LiveReuseKind where
  | created
  | reused
  | rebuilt

def LiveReuseKind.asString : LiveReuseKind → String
  | .created => "created"
  | .reused => "reused"
  | .rebuilt => "rebuilt"

structure LiveProbeRequest where
  requestId : String
  command : String
  filePath : String
  theoremName : String
  contentDigest : String
  content : String

private def optionalRequestId (json : Json) : Option String :=
  match json.getObjValAs? String "request_id" with
  | Except.ok requestId => some requestId
  | Except.error _ => none

def parseLiveProbeRequest (json : Json) : Except String LiveProbeRequest := do
  let schemaVersion ← json.getObjValAs? String "schema_version"
  if schemaVersion != "probe.live.v1" then
    throw s!"unsupported live schema version `{schemaVersion}`"
  pure
    { requestId := ← json.getObjValAs? String "request_id"
    , command := ← json.getObjValAs? String "command"
    , filePath := ← json.getObjValAs? String "file_path"
    , theoremName := ← json.getObjValAs? String "theorem"
    , contentDigest := ← json.getObjValAs? String "content_digest"
    , content := ← json.getObjValAs? String "content"
    }

private def liveStatus
    (status : String)
    (requestId? : Option String)
    (fields : Array (String × Json) := #[]) : Json :=
  Json.mkObj <|
    [ ("schema_version", liveSchemaVersion)
    , ("status", Json.str status)
    ] ++
      (match requestId? with
      | some requestId => [("request_id", Json.str requestId)]
      | none => []) ++
      fields.toList

def liveErrorResponse (requestId? : Option String) (message : String) : Json :=
  liveStatus "error" requestId? #[("message", Json.str message)]

private def liveSessionJson (request : LiveProbeRequest) (reuseKind : LiveReuseKind) : Json :=
  Json.mkObj
    [ ("file_path", Json.str request.filePath)
    , ("content_digest", Json.str request.contentDigest)
    , ("reuse", Json.str reuseKind.asString)
    ]

def liveOkResponse (request : LiveProbeRequest) (reuseKind : LiveReuseKind) (probe : Json) : Json :=
  liveStatus "ok" (some request.requestId)
    #[ ("command", Json.str request.command)
     , ("file_path", Json.str request.filePath)
     , ("theorem", Json.str request.theoremName)
     , ("session", liveSessionJson request reuseKind)
     , ("probe", probe)
     ]

def liveRequestIdFromJson? (json : Json) : Option String :=
  optionalRequestId json

end Tools.SemanticProbe
