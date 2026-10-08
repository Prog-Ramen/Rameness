"""JSON schemas for every model call whose reply must be JSON.

``Provider.complete_json(..., schema=...)`` sends them to the server for constrained decoding (``response_format``
on llama.cpp, vLLM, TabbyAPI and ninfer; a reply tool on Anthropic models), so the reply can only be JSON of this
shape once the model's thinking ends. Kept loose where the content is open-ended (an SOP's own input schema, a
test's input), strict where Rameness reads the fields.
"""

STR = {"type": "string"}
STRS = {"type": "array", "items": STR}
OBJ = {"type": "object"}
INTS = {"type": "array", "items": {"type": "integer"}}

TESTS = {"type": "array", "items": {"type": "object", "properties": {
    "input": OBJ, "expect": OBJ, "expect_keys": STRS, "expect_error": {"type": "boolean"},
    "files": {"type": "object", "additionalProperties": STR}, "setup": STR}, "required": ["input"]}}

# learning.Learner.review_runs: repeated multi-step tasks in the recent runs
REVIEW = {"type": "object", "required": ["procedures"], "properties": {"procedures": {
    "type": "array", "maxItems": 3, "items": {"type": "object", "required": ["name", "description", "occurrences"],
                                              "properties": {
        "name": STR, "description": STR, "params": STRS,
        "occurrences": {"type": "array", "items": {"type": "object", "required": ["run", "steps"],
                                                   "properties": {"run": STR, "steps": INTS}}}}}}}}

# learning.Learner._llm_spec: a new SOP
SOP_SPEC = {"type": "object", "required": ["id", "description", "script", "tests"], "properties": {
    "id": STR, "description": STR, "keywords": STRS, "inputs": OBJ, "outputs": OBJ, "permissions": STRS,
    "script": STR, "tests": TESTS}}

# learning.extend_sop: an existing SOP extended to cover a similar procedure
SOP_EXTENSION = {"type": "object", "required": ["script", "tests"], "properties": {
    "script": STR, "inputs": OBJ, "outputs": OBJ, "description": STR, "keywords": STRS, "tests": TESTS}}

# learning.simplify_sop: a standard-only rewrite, or keep the original
SOP_SIMPLIFIED = {"type": "object", "properties": {"script": STR, "keep": {"type": "boolean"}, "why": STR}}

# router.Router.resolve_args: an SOP's arguments, from the request (null for anything not stated)
SOP_ARGS = OBJ

# improve: extra cue words for decision options
CUES = {"type": "object", "required": ["cues"], "properties": {
    "cues": {"type": "object", "additionalProperties": STR}, "rationale": STR}}

# fleet.manager: approaches to fork into, and a request split into subtasks
APPROACHES = {"type": "object", "required": ["approaches"], "properties": {"approaches": STRS}}
SUBTASKS = {"type": "object", "required": ["subtasks"], "properties": {"subtasks": {"type": "array", "items": {
    "type": "object", "required": ["title", "task"], "properties": {
        "title": STR, "task": STR, "kind": {"type": "string", "enum": ["deliver", "research"]},
        "depends_on": INTS, "size": {"type": "string", "enum": ["small", "large"]}, "needs": STRS}}}}}

# learning.repair_sop: the SOP's script and tests after seeing what its tests actually returned
SOP_REPAIR = {"type": "object", "required": ["script", "tests"], "properties": {"script": STR, "tests": TESTS,
                                                                            "explanation": STR}}
