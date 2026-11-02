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

/*
 * Settings objects are plain JSON from the server and may carry any property name, including `__proto__`,
 * `constructor` and `toString`. They are only ever read with `getOwn` and written with `withKey`/`withoutKey`,
 * which create own data properties on a null-prototype object and never go through the prototype chain.
 */
export const hasOwn = (o: object, key: string): boolean => Object.hasOwn(o, key);
export const getOwn = (o: Values, key: string): unknown => (Object.hasOwn(o, key) ? o[key] : undefined);

function define(target: Values, key: string, value: unknown): void {
  Object.defineProperty(target, key, { value, enumerable: true, writable: true, configurable: true });
}

/** A copy of `values` with `key` set to `value` (as an own property, in place if it exists). */
export function withKey(values: Values, key: string, value: unknown): Values {
  const next: Values = Object.create(null);
  for (const k of Object.keys(values)) define(next, k, values[k]);
  define(next, key, value);
  return next;
}

/** A copy of `values` without `key`. */
export function withoutKey(values: Values, key: string): Values {
  const next: Values = Object.create(null);
  for (const k of Object.keys(values)) if (k !== key) define(next, k, values[k]);
  return next;
}

/** Structural equality over own enumerable keys (settings are JSON: no cycles, no prototypes). */
export function sameJson(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== "object" || typeof b !== "object" || a === null || b === null) return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => sameJson(x, b[i]));
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  return ka.length === kb.length && ka.every((k) => Object.hasOwn(b, k) && sameJson((a as Values)[k], (b as Values)[k]));
}

// What the platform refuses in a header-list, whatever the strategy's schema says (server is the authority).
export const HEADER_NAME = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
const CREDENTIAL_HEADERS = new Set(["authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"]);

export function headerProblem(name: string): string | undefined {
  if (!HEADER_NAME.test(name)) return `“${name}” is not an HTTP header name`;
  if (CREDENTIAL_HEADERS.has(name.toLowerCase())) return `“${name}” carries credentials and cannot be used`;
  return undefined;
}

/**
 * Problems keyed by field path ("max_waiting", "nested.field"); `prefix` is added in front.
 *
 * Presence follows JSON Schema as the control plane applies it (`compiler/strategy.py`): a property is
 * missing only when it is not an own key; `required` asks for presence and nothing else. `""`, `false`, `0`
 * and `[]` are values, not absence, and no `minLength`/`minItems` is implied. An optional object that is
 * absent is not looked into.
 */
export function validateValues(fields: Field[], values: Values, prefix = ""): Record<string, string> {
  const errors: Record<string, string> = Object.create(null);
  for (const f of fields) {
    const path = `${prefix}${f.key}`;
    if (!hasOwn(values, f.key) || values[f.key] === undefined) {
      if (f.required) errors[path] = f.kind === "boolean" ? `${f.title} is required: choose On or Off` : `${f.title} is required`;
      continue;
    }
    const v = values[f.key];
    if (f.kind === "object") {
      if (!isRecord(v)) errors[path] = "Must be an object";
      else Object.assign(errors, validateValues(f.fields ?? [], v, `${path}.`));
      continue;
    }
    if (f.kind === "string" && typeof v !== "string") errors[path] = "Must be text";
    else if ((f.kind === "integer" || f.kind === "number") && !isNumber(v)) errors[path] = "Must be a number";
    else if (f.kind === "integer" && !Number.isInteger(v)) errors[path] = "Must be a whole number";
    else if (f.kind === "boolean" && typeof v !== "boolean") errors[path] = "Must be On or Off";
    else if (f.kind === "array") {
      if (!Array.isArray(v) || v.some((x) => typeof x !== "string")) errors[path] = "Must be a list of text";
      else if (f.widget === HEADER_LIST_WIDGET) {
        const problem = v.map((x) => headerProblem(x as string)).find(Boolean);
        if (problem) errors[path] = problem;
      }
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

/** Keys of `values` the schema does not declare (kept out of the form, shown as a warning). */
export function unknownKeys(fields: Field[], values: Values): string[] {
  const known = new Set(fields.map((f) => f.key));
  return Object.keys(values).filter((k) => !known.has(k));
}

/** Titles of everything a schema lets a user set, for the strategies table. */
export function settingTitles(schema: unknown): string[] {
  const parsed = parseSchema(schema);
  return parsed ? parsed.fields.map((f) => f.title) : [];
}

/** A strategy's own title and description are as untrusted as its schema: bounded, shown as text. */
export const strategyTitle = (s: { title: string; name: string }) => clip(s.title, MAX_TITLE) || clip(s.name, MAX_TITLE);
export const strategyDescription = (s: { description: string }) => clip(s.description, MAX_DESCRIPTION);
