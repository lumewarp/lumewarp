/**
 * The JSON Schema subset strategies may use for their settings (docs/plans/model-catalog.md, "Schema 子集").
 *
 * A schema comes from a gateway, so it is untrusted data: it is size-limited, anything outside the subset
 * makes `parseSchema` return null (the caller falls back to a JSON editor), and every string in it is only
 * ever rendered as text. Nothing here evaluates, fetches or interprets markup.
 */

export const MAX_SCHEMA_BYTES = 16 * 1024;
export const MAX_PROPERTIES = 64;
export const MAX_ENUM = 64;
const MAX_TITLE = 200;
const MAX_DESCRIPTION = 2000;
export const HEADER_LIST_WIDGET = "header-list";

export type Scalar = string | number | boolean;

export interface Field {
  key: string;
  kind: "string" | "integer" | "number" | "boolean" | "array" | "object";
  title: string;
  description: string;
  required: boolean;
  enum?: (string | number)[];
  minimum?: number;
  maximum?: number;
  default?: unknown;
  /** Only `header-list` is known; arrays of strings without it are edited as a plain list. */
  widget?: typeof HEADER_LIST_WIDGET;
  fields?: Field[];
}

export interface ParsedSchema {
  title: string;
  description: string;
  fields: Field[];
}

const COMMON = new Set(["type", "title", "description", "default"]);
const OBJECT_KEYS = new Set([...COMMON, "properties", "required", "additionalProperties"]);
const SCALAR_KEYS: Record<string, Set<string>> = {
  string: new Set([...COMMON, "enum"]),
  integer: new Set([...COMMON, "enum", "minimum", "maximum"]),
  number: new Set([...COMMON, "enum", "minimum", "maximum"]),
  boolean: COMMON,
  array: new Set([...COMMON, "items", "x-lumewarp-widget"]),
};

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const isNumber = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const clip = (v: unknown, max: number) => (typeof v === "string" ? (v.length > max ? `${v.slice(0, max)}…` : v) : "");

function parseField(key: string, schema: unknown, required: boolean, mayNest: boolean): Field | null {
  if (!isRecord(schema)) return null;
  const type = schema.type;
  if (typeof type !== "string") return null;
  const text = { title: clip(schema.title, MAX_TITLE) || key, description: clip(schema.description, MAX_DESCRIPTION) };
  for (const keyword of ["title", "description"]) if (keyword in schema && typeof schema[keyword] !== "string") return null;

  if (type === "object") {
    if (!mayNest) return null;
    const nested = parseObject(schema, false);
    return nested && { key, kind: "object", ...text, required, default: schema.default, fields: nested };
  }
  const allowed = SCALAR_KEYS[type];
  if (!allowed || Object.keys(schema).some((k) => !allowed.has(k))) return null;
  const field: Field = { key, kind: type as Field["kind"], ...text, required, default: schema.default };
  for (const bound of ["minimum", "maximum"] as const) {
    if (bound in schema) {
      if (!isNumber(schema[bound])) return null;
      field[bound] = schema[bound];
    }
  }
  if (field.minimum !== undefined && field.maximum !== undefined && field.minimum > field.maximum) return null;
  if ("enum" in schema) {
    const values = schema.enum;
    const ok = (v: unknown) => (type === "string" ? typeof v === "string" : type === "integer" ? Number.isInteger(v) : type === "number" ? isNumber(v) : false);
    if (!Array.isArray(values) || !values.length || values.length > MAX_ENUM || !values.every(ok)) return null;
    field.enum = values as (string | number)[];
  }
  if (type === "array") {
    const items = schema.items;
    if (!isRecord(items) || items.type !== "string" || Object.keys(items).length !== 1) return null;
    const widget = schema["x-lumewarp-widget"];
    if (widget !== undefined && widget !== HEADER_LIST_WIDGET) return null;
    if (widget) field.widget = HEADER_LIST_WIDGET;
  }
  return field;
}

function parseObject(schema: Record<string, unknown>, mayNest: boolean): Field[] | null {
  if (schema.type !== "object") return null;
  if (Object.keys(schema).some((k) => !OBJECT_KEYS.has(k))) return null;
  if (schema.additionalProperties !== undefined && schema.additionalProperties !== false) return null;
  const properties = schema.properties ?? {};
  if (!isRecord(properties)) return null;
  const names = Object.keys(properties);
  if (names.length > MAX_PROPERTIES) return null;
  const required = schema.required ?? [];
  if (!Array.isArray(required) || required.some((r) => typeof r !== "string" || !names.includes(r))) return null;
  const fields: Field[] = [];
  for (const name of names) {
    const field = parseField(name, properties[name], required.includes(name), mayNest);
    if (!field) return null;
    fields.push(field);
  }
  return fields;
}

/** The fields to render, or null when the schema is missing or outside the subset (use a JSON editor). */
export function parseSchema(schema: unknown): ParsedSchema | null {
  if (!isRecord(schema)) return null;
  try {
    if (JSON.stringify(schema).length > MAX_SCHEMA_BYTES) return null;
  } catch {
    return null;
  }
  const fields = parseObject(schema, true);
  if (!fields) return null;
  return { title: clip(schema.title, MAX_TITLE), description: clip(schema.description, MAX_DESCRIPTION), fields };
}

export type Values = Record<string, unknown>;

// What the platform refuses in a header-list, whatever the strategy's schema says (server is the authority).
export const HEADER_NAME = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
const CREDENTIAL_HEADERS = new Set(["authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"]);

export function headerProblem(name: string): string | undefined {
  if (!HEADER_NAME.test(name)) return `“${name}” is not an HTTP header name`;
  if (CREDENTIAL_HEADERS.has(name.toLowerCase())) return `“${name}” carries credentials and cannot be used`;
  return undefined;
}

/** Problems keyed by field path ("max_waiting", "nested.field"); `prefix` is added in front. */
export function validateValues(fields: Field[], values: Values, prefix = ""): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const f of fields) {
    const path = `${prefix}${f.key}`;
    const v = values[f.key];
    if (f.kind === "object") {
      const inner = isRecord(v) ? v : {};
      Object.assign(errors, validateValues(f.fields ?? [], inner, `${path}.`));
      continue;
    }
    const empty = v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0 && f.kind !== "array");
    if (empty) {
      if (f.required && f.kind !== "boolean") errors[path] = `${f.title} is required`;
      continue;
    }
    if (f.kind === "string" && typeof v !== "string") errors[path] = "Must be text";
    else if ((f.kind === "integer" || f.kind === "number") && !isNumber(v)) errors[path] = "Must be a number";
    else if (f.kind === "integer" && !Number.isInteger(v)) errors[path] = "Must be a whole number";
    else if (f.kind === "boolean" && typeof v !== "boolean") errors[path] = "Must be on or off";
    else if (f.kind === "array") {
      const list = Array.isArray(v) ? v : [];
      const blank = list.some((x) => typeof x !== "string" || !x.trim());
      if (blank) errors[path] = "Entries cannot be empty";
      else if (f.widget === HEADER_LIST_WIDGET) {
        const problem = list.map((x) => headerProblem(String(x))).find(Boolean);
        if (problem) errors[path] = problem;
      } else if (f.required && !list.length) errors[path] = `${f.title} is required`;
    }
    if (errors[path]) continue;
    if (isNumber(v)) {
      if (f.minimum !== undefined && v < f.minimum) errors[path] = `Must be at least ${f.minimum}`;
      else if (f.maximum !== undefined && v > f.maximum) errors[path] = `Must be at most ${f.maximum}`;
    }
    if (f.enum && !f.enum.includes(v as string | number)) errors[path] = "Pick one of the listed values";
  }
  return errors;
}

/** Drop empty optional entries so the stored params hold only what was set. */
export function cleanValues(fields: Field[], values: Values, keepUnknown = false): Values {
  const out: Values = keepUnknown ? { ...values } : {};
  for (const f of fields) delete out[f.key];
  for (const f of fields) {
    const v = values[f.key];
    if (f.kind === "object") {
      const inner = cleanValues(f.fields ?? [], isRecord(v) ? v : {});
      if (Object.keys(inner).length) out[f.key] = inner;
    } else if (v !== undefined && v !== null && v !== "") {
      out[f.key] = f.kind === "array" ? (v as string[]).map((x) => x.trim()) : v;
    }
  }
  return out;
}

/** Keys of `values` the schema does not declare (kept out of the form, shown as a warning). */
export function unknownKeys(fields: Field[], values: Values): string[] {
  const known = new Set(fields.map((f) => f.key));
  return Object.keys(values).filter((k) => !known.has(k) && k !== "headers");
}

/** Titles of everything a schema lets a user set, for the strategies table. */
export function settingTitles(schema: unknown): string[] {
  const parsed = parseSchema(schema);
  return parsed ? parsed.fields.map((f) => f.title) : [];
}
