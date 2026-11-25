/**
 * json-schema.js — replacement for pi-subagents 0.23.0 `dist/workflow/json-schema.js`.
 *
 * The upstream module validates through `typebox/value`, a third-party package the
 * vendored runtime must not import. This one keeps its export, `compileJsonSchema`,
 * its root rules (a JSON-serialisable object schema with `type: "object"`, at most
 * 64 KiB) and its error shape (`$.path: message`, at most five), and checks the
 * subset the runtime's `agent({ schema })` needs: `type` (one or a list), `properties`,
 * `required`, `enum`, `items` and `additionalProperties`. Other keywords are
 * annotations here and are not enforced.
 */
import { isDeepStrictEqual } from "node:util";

const MAX_SCHEMA_BYTES = 64 * 1024;
const MAX_REPORTED_ERRORS = 5;
const TYPES = new Set(["object", "array", "string", "number", "integer", "boolean", "null"]);

const isPlainObject = (value) => typeof value === "object" && value !== null && !Array.isArray(value);

function typeOf(value) {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value;
}

function matchesType(type, value) {
  if (type === "integer") return Number.isInteger(value);
  if (type === "number") return typeof value === "number" && Number.isFinite(value);
  return typeOf(value) === type;
}

/** Throws on a schema node this validator cannot walk, so the call that wrote it fails. */
function assertWalkable(node, where) {
  if (typeof node === "boolean") return;
  if (!isPlainObject(node)) throw new Error(`${where} is not a schema object`);
  const types = node.type === undefined ? [] : Array.isArray(node.type) ? node.type : [node.type];
  for (const type of types) if (!TYPES.has(type)) throw new Error(`${where}.type ${JSON.stringify(type)} is unknown`);
  if (node.enum !== undefined && !Array.isArray(node.enum)) throw new Error(`${where}.enum is not an array`);
  if (node.required !== undefined && !(Array.isArray(node.required) && node.required.every((k) => typeof k === "string"))) {
    throw new Error(`${where}.required is not a list of names`);
  }
  if (node.properties !== undefined) {
    if (!isPlainObject(node.properties)) throw new Error(`${where}.properties is not an object`);
    for (const [key, sub] of Object.entries(node.properties)) assertWalkable(sub, `${where}.properties.${key}`);
  }
  if (node.items !== undefined) assertWalkable(node.items, `${where}.items`);
  if (node.additionalProperties !== undefined) assertWalkable(node.additionalProperties, `${where}.additionalProperties`);
}

// Structural, so an enum object matches in any key order.
const equal = isDeepStrictEqual;

function collect(node, value, path, errors) {
  if (errors.length >= MAX_REPORTED_ERRORS) return;
  if (node === true) return;
  if (node === false) {
    errors.push(`${path}: not allowed`);
    return;
  }
  if (node.type !== undefined) {
    const types = Array.isArray(node.type) ? node.type : [node.type];
    if (!types.some((type) => matchesType(type, value))) {
      errors.push(`${path}: expected ${types.join(" or ")}, got ${typeOf(value)}`);
      return;
    }
  }
  if (node.enum !== undefined && !node.enum.some((option) => equal(option, value))) {
    errors.push(`${path}: must be one of ${JSON.stringify(node.enum)}`);
  }
  if (Array.isArray(value)) {
    if (node.items !== undefined) value.forEach((item, i) => collect(node.items, item, `${path}[${i}]`, errors));
    return;
  }
  if (!isPlainObject(value)) return;
  for (const key of node.required ?? []) {
    if (!Object.hasOwn(value, key)) errors.push(`${path}: missing required property "${key}"`);
  }
  const properties = node.properties ?? {};
  for (const [key, entry] of Object.entries(value)) {
    if (Object.hasOwn(properties, key)) collect(properties[key], entry, `${path}.${key}`, errors);
    else if (node.additionalProperties !== undefined) collect(node.additionalProperties, entry, `${path}.${key}`, errors);
  }
}

function checkAgainst(schema, value) {
  const errors = [];
  collect(schema, value, "$", errors);
  return errors.length === 0 ? true : errors.slice(0, MAX_REPORTED_ERRORS).join("; ");
}

/** Same contract as upstream: `{ ok: true, compiled: { schema, check } }` or `{ ok: false, message }`. */
export function compileJsonSchema(schema) {
  if (!isPlainObject(schema)) {
    return { ok: false, message: "agent() opts.schema must be a JSON Schema object." };
  }
  if (schema.type !== "object") {
    return {
      ok: false,
      message: 'agent() opts.schema must have `type: "object"` at its root — it becomes the tool\'s input schema, '
        + "and a non-object root is not something a model can be asked to fill.",
    };
  }
  let serialized;
  try {
    serialized = JSON.stringify(schema);
  } catch {
    return { ok: false, message: "agent() opts.schema must be JSON-serializable." };
  }
  if (serialized.length > MAX_SCHEMA_BYTES) {
    return {
      ok: false,
      message: `agent() opts.schema is too large (${serialized.length} bytes; the limit is ${MAX_SCHEMA_BYTES}).`,
    };
  }
  try {
    assertWalkable(schema, "$");
  } catch (error) {
    return { ok: false, message: `agent() opts.schema is not a schema this runtime can validate: ${error.message}` };
  }
  return { ok: true, compiled: { schema, check: (value) => checkAgainst(schema, value) } };
}
