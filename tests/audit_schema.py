"""Tiny stdlib validator for the JSON Schema subset used by docs/spec/audit/schemas/*.json.

Supported: type (name or list), enum, const, pattern, maxLength, minimum, maximum, minItems, maxItems, required,
properties, additionalProperties (bool or schema), items, $ref (#/$defs/x), anyOf, allOf, if/then.
validate(instance, schema, root=None) returns a list of error strings (empty = valid)."""
import json
import os
import re

SCHEMAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "spec", "audit", "schemas")
TYPES = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}


def load(name):
    with open(os.path.join(SCHEMAS, name), encoding="utf-8") as f:
        return json.load(f)


def _is(v, t):
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, TYPES[t])


def validate(inst, sch, root=None, path="$"):
    root = root if root is not None else sch
    errs = []
    if "$ref" in sch:
        return validate(inst, root["$defs"][sch["$ref"].rsplit("/", 1)[1]], root, path)
    if "type" in sch:
        ts = sch["type"] if isinstance(sch["type"], list) else [sch["type"]]
        if not any(_is(inst, t) for t in ts):
            return ["%s: expected %s, got %s" % (path, "/".join(ts), type(inst).__name__)]
    if "const" in sch and inst != sch["const"]:
        errs.append("%s: expected const %r" % (path, sch["const"]))
    if "enum" in sch and inst not in sch["enum"]:
        errs.append("%s: %r not in enum" % (path, inst))
    if isinstance(inst, str):
        if "maxLength" in sch and len(inst) > sch["maxLength"]:
            errs.append("%s: longer than %d" % (path, sch["maxLength"]))
        if "pattern" in sch and not re.search(sch["pattern"], inst):
            errs.append("%s: %r does not match %s" % (path, inst, sch["pattern"]))
    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        if "minimum" in sch and inst < sch["minimum"]:
            errs.append("%s: below minimum" % path)
        if "maximum" in sch and inst > sch["maximum"]:
            errs.append("%s: above maximum" % path)
    if isinstance(inst, list):
        if "minItems" in sch and len(inst) < sch["minItems"]:
            errs.append("%s: too few items" % path)
        if "maxItems" in sch and len(inst) > sch["maxItems"]:
            errs.append("%s: too many items" % path)
        if "items" in sch:
            for i, x in enumerate(inst):
                errs += validate(x, sch["items"], root, "%s[%d]" % (path, i))
    if isinstance(inst, dict):
        for k in sch.get("required", []):
            if k not in inst:
                errs.append("%s: missing %s" % (path, k))
        props = sch.get("properties", {})
        for k, v in inst.items():
            if k in props:
                errs += validate(v, props[k], root, path + "." + k)
            elif sch.get("additionalProperties") is False:
                errs.append("%s: unexpected field %s" % (path, k))
            elif isinstance(sch.get("additionalProperties"), dict):
                errs += validate(v, sch["additionalProperties"], root, path + "." + k)
    if "anyOf" in sch and not any(not validate(inst, s, root, path) for s in sch["anyOf"]):
        errs.append("%s: matches none of anyOf" % path)
    for s in sch.get("allOf", []):
        errs += validate(inst, s, root, path)
    if "if" in sch and not validate(inst, sch["if"], root, path) and "then" in sch:
        errs += validate(inst, sch["then"], root, path)
    return errs
