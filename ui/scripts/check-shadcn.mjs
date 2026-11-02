// Guard: the UI is built from shadcn components, and stays that way.
//
// Static (AST) checks over src/**/*.{ts,tsx}; no runtime DOM, because Radix itself needs hidden
// <select>/<input> elements. Rules:
//   legacy-class       one of the 45 classes of the old styles.css used as a full class token
//   native-element     <button> <select> <input> <textarea> <table> <datalist> <option>
//   native-input-type  <Input type="checkbox|radio"> instead of Checkbox / RadioGroup
//   radix-import       `radix-ui` or `@radix-ui/*` imported outside src/components/ui/
//   dynamic-class      a class expression that cannot be decided statically (unknown call)
//   css-layer          `@layer components` in styles.css
//
// Where a class is looked for: className (and *ClassName / class) attributes, `className:` object
// properties, the arguments of cn / clsx / cva / twMerge / ..., template strings, and constants
// those expressions reference, including constants exported from other .ts/.tsx files.
// Plain identifiers that cannot be resolved (props, parameters) are skipped: whatever the caller
// passes is checked at the caller's own site.
//
// There is no allowlist: every finding fails. Only the generated primitives in GENERATED_PRIMITIVES
// are exempt, and only from the native-element rule (that is what shadcn generates).

import { parse } from "@babel/parser";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

export const LEGACY_CLASSES = new Set(
  (
    "brand brand-mark btn btn-danger-ghost btn-primary btn-sm card field field-error field-grid " +
    "field-hint filters form form-error form-fieldset form-section form-warn input kv login " +
    "login-card muted order-row pad page-actions page-head pager panel-head plain row-actions " +
    "section-gap split-card split-head split-share stat stat-value state state-error stats " +
    "tab-button tabs target-row targets tight toolbar"
  ).split(" "),
);

export const NATIVE_ELEMENTS = new Set(["button", "select", "input", "textarea", "table", "datalist", "option"]);
/** Radix may be imported anywhere in here. */
export const PRIMITIVES_DIR = "src/components/ui/";
/**
 * The files shadcn generated (the older ones carry this project's variants and theme). Only these are exempt from the
 * native element rule. Anything else in the directory, e.g. combobox-field.tsx, is ours: it goes through `adapters`
 * like any other hand-written file, and a new generated file is added here on purpose, in review.
 */
export const GENERATED_PRIMITIVES = new Set(
  [
    "alert-dialog", "alert", "badge", "button", "card", "checkbox", "command", "dialog", "empty", "field", "input", "label",
    "popover", "radio-group", "select", "separator", "sheet", "sidebar", "skeleton", "sonner", "table", "tabs", "textarea",
  ].map((n) => `${PRIMITIVES_DIR}${n}.tsx`),
);

const CLASS_FUNCTIONS = new Set(["cn", "clsx", "cva", "twMerge", "cx", "classNames", "tv"]);
const PASS_THROUGH_METHODS = new Set(["join", "filter", "trim", "concat", "flat", "toString", "trimEnd", "trimStart"]);
const HOLE = "\u0000";

const toPosix = (p) => p.split(sep).join("/");

function parseSource(code, file) {
  return parse(code, {
    sourceType: "module",
    plugins: ["typescript", "jsx"],
    errorRecovery: false,
    sourceFilename: file,
  });
}

function* children(node) {
  for (const key of Object.keys(node)) {
    if (key === "loc" || key === "start" || key === "end" || key === "extra" || key === "leadingComments" || key === "trailingComments" || key === "innerComments") continue;
    const v = node[key];
    if (Array.isArray(v)) {
      for (const c of v) if (c && typeof c.type === "string") yield c;
    } else if (v && typeof v.type === "string") yield v;
  }
}

function walk(node, visit) {
  visit(node);
  for (const c of children(node)) walk(c, visit);
}

const unwrap = (n) => {
  while (n && (n.type === "TSAsExpression" || n.type === "TSSatisfiesExpression" || n.type === "TSNonNullExpression" || n.type === "ParenthesizedExpression" || n.type === "TSTypeAssertion")) n = n.expression;
  return n;
};

const propName = (p) => (p.computed ? null : p.key.type === "Identifier" ? p.key.name : p.key.type === "StringLiteral" ? p.key.value : null);

/** Split a class string into tokens; returns tokens as {token, partial}. */
function classTokens(text) {
  return text
    .split(/\s+/)
    .filter(Boolean)
    .map((raw) => {
      let t = raw;
      // Drop Tailwind variants (md:, hover:, dark:) unless the colon is inside an arbitrary value.
      if (!t.includes("[")) t = t.slice(t.lastIndexOf(":") + 1);
      t = t.replace(/^!+/, "").replace(/!+$/, "");
      return { token: t, partial: t.includes(HOLE) };
    })
    // A token that is nothing but unknown parts says nothing about which class it is.
    .filter((t) => t.token.replaceAll(HOLE, "") !== "");
}

function partialMatchesLegacy(token) {
  const re = new RegExp("^" + token.split(HOLE).map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join(".*") + "$");
  const hits = [...LEGACY_CLASSES].filter((c) => re.test(c));
  return hits.length ? hits.map((c) => `"${c}"`).join(", ") : null;
}

/** Index a project: parsed ASTs, top-level consts and imports per file. */
export function loadProject(root, srcDir = "src") {
  const files = new Map();
  const abs = join(root, srcDir);
  const stack = existsSync(abs) ? [abs] : [];
  while (stack.length) {
    const dir = stack.pop();
    for (const name of readdirSync(dir)) {
      const p = join(dir, name);
      if (statSync(p).isDirectory()) stack.push(p);
      else if (/\.(ts|tsx)$/.test(name) && !name.endsWith(".d.ts")) {
        const rel = toPosix(relative(root, p));
        const code = readFileSync(p, "utf8");
        const ast = parseSource(code, rel);
        files.set(rel, { rel, code, ast, consts: new Map(), imports: new Map(), exports: new Map() });
      }
    }
  }
  const project = { root, srcDir, files };
  for (const f of files.values()) indexFile(f);
  return project;
}

function indexFile(f) {
  // What the module exports: name -> {local} | {source, name} (re-export) | {expr}; plus `export *` sources.
  f.stars = [];
  for (const n of f.ast.program.body) {
    if (n.type === "ExportNamedDeclaration") {
      if (n.declaration?.type === "VariableDeclaration") {
        for (const d of n.declaration.declarations) if (d.id.type === "Identifier") f.exports.set(d.id.name, { local: d.id.name });
      } else if (n.declaration) {
        const id = n.declaration.id?.name;
        if (id) f.exports.set(id, { opaque: true }); // function, class, enum, type: nothing to evaluate
      }
      for (const sp of n.specifiers ?? []) {
        if (sp.type !== "ExportSpecifier") continue;
        const out = sp.exported.name ?? sp.exported.value;
        const inn = sp.local.name ?? sp.local.value;
        f.exports.set(out, n.source ? { source: n.source.value, name: inn } : { local: inn });
      }
    } else if (n.type === "ExportAllDeclaration") {
      if (n.exported) f.exports.set(n.exported.name ?? n.exported.value, { opaque: true });
      else f.stars.push(n.source.value);
    } else if (n.type === "ExportDefaultDeclaration") {
      const d = unwrap(n.declaration);
      f.exports.set("default", d.type === "Identifier" ? { local: d.name } : d.type.endsWith("Expression") || d.type.endsWith("Literal") ? { expr: d } : { opaque: true });
    }
  }
  walk(f.ast.program, (n) => {
    if (n.type === "VariableDeclarator" && n.id.type === "Identifier" && n.init) {
      const list = f.consts.get(n.id.name) ?? [];
      list.push(n.init);
      f.consts.set(n.id.name, list);
    }
    if (n.type === "ImportDeclaration") {
      for (const s of n.specifiers) {
        if (s.type === "ImportSpecifier") f.imports.set(s.local.name, { source: n.source.value, name: s.imported.name ?? s.imported.value });
        else if (s.type === "ImportDefaultSpecifier") f.imports.set(s.local.name, { source: n.source.value, name: "default" });
        else if (s.type === "ImportNamespaceSpecifier") f.imports.set(s.local.name, { source: n.source.value, name: "*" });
      }
    }
  });
}

function resolveModule(project, from, source) {
  let base;
  if (source.startsWith("@/")) base = join(project.srcDir, source.slice(2));
  else if (source.startsWith(".")) base = toPosix(join(dirname(from.rel), source));
  else return null;
  base = toPosix(base);
  for (const cand of [base + ".ts", base + ".tsx", base + "/index.ts", base + "/index.tsx", base]) {
    if (project.files.has(cand)) return project.files.get(cand);
  }
  return null;
}

/**
 * What a name refers to in file `f`.
 *   {status:"ok", file, inits}   constants it may hold
 *   {status:"namespace", file}   `import * as ns`: the module whose exports are reachable through it
 *   {status:"external"}          an import from outside the project (a package, a css file): not ours to follow
 *   {status:"unresolved"}        an import into the project that is not a constant we can evaluate
 *   null                         not a module-level binding: a parameter or a global, checked where it is produced
 */
function resolveBinding(project, f, name) {
  const local = f.consts.get(name);
  if (local) return { status: "ok", file: f, inits: local };
  const imp = f.imports.get(name);
  if (!imp) return null;
  const target = resolveModule(project, f, imp.source);
  if (!target) return { status: "external" };
  if (imp.name === "*") return { status: "namespace", file: target };
  return lookupExport(project, target, imp.name, new Set());
}

/** The constants behind export `name` of `file`, following aliases, `export default`, `export { } from` and `export *`. */
function lookupExport(project, file, name, seen) {
  const key = `${file.rel}#${name}`;
  if (seen.has(key)) return { status: "unresolved" };
  seen.add(key);
  const spec = file.exports.get(name);
  if (spec?.expr) return { status: "ok", file, inits: [spec.expr] };
  if (spec?.local) {
    const consts = file.consts.get(spec.local);
    if (consts) return { status: "ok", file, inits: consts };
    if (file.imports.has(spec.local)) return resolveBinding(project, file, spec.local) ?? { status: "unresolved" };
    return { status: "unresolved" };
  }
  if (spec?.source) {
    const target = resolveModule(project, file, spec.source);
    return target ? lookupExport(project, target, spec.name, seen) : { status: "external" };
  }
  if (spec) return { status: "unresolved" };
  if (name !== "default") {
    for (const source of file.stars) {
      const target = resolveModule(project, file, source);
      if (!target) continue;
      const hit = lookupExport(project, target, name, seen);
      if (hit.status === "ok" || hit.status === "external") return hit;
    }
  }
  return { status: "unresolved" };
}

/** Every exported constant of a module (a namespace used without a static member name). */
function allExports(project, file) {
  const out = [];
  const names = new Set(file.exports.keys());
  for (const source of file.stars) {
    const target = resolveModule(project, file, source);
    if (target) for (const n of target.exports.keys()) names.add(n);
  }
  for (const n of names) {
    const hit = lookupExport(project, file, n, new Set());
    if (hit.status === "ok") out.push(hit);
  }
  return out;
}

const memberName = (m) => (m.computed ? (m.property.type === "StringLiteral" ? m.property.value : null) : m.property.type === "Identifier" ? m.property.name : null);

/** The initialisers an identifier or member chain refers to, with an outcome for what could not be followed. */
function resolveRef(project, f, node) {
  node = unwrap(node);
  const id = rootIdentifier(node);
  if (!id) return { hits: [], unresolved: false };
  const b = resolveBinding(project, f, id.name);
  if (!b || b.status === "external") return { hits: [], unresolved: false };
  if (b.status === "unresolved") return { hits: [], unresolved: true };
  if (b.status === "namespace") {
    // ns.member: that export; ns[x] or ns alone: all of them
    const direct = node.type === "MemberExpression" && unwrap(node.object) === id ? memberName(node) : null;
    if (direct) {
      const hit = lookupExport(project, b.file, direct, new Set());
      return hit.status === "ok" ? { hits: [{ ...hit, name: direct }], unresolved: false } : { hits: [], unresolved: hit.status === "unresolved" };
    }
    return { hits: allExports(project, b.file), unresolved: false };
  }
  return { hits: [{ ...b, name: id.name }], unresolved: false };
}

/**
 * Fold an expression to the string it builds. Parts that cannot be known are HOLEs, so "b" + "tn" is "btn" and
 * "b" + prop is "b<hole>". Only the shapes that build strings are folded; the rest is a hole.
 */
function fold(project, f, node, seen = new Set()) {
  node = unwrap(node);
  if (!node) return HOLE;
  switch (node.type) {
    case "StringLiteral":
      return node.value;
    case "TemplateLiteral":
      return node.quasis.map((q, i) => (q.value.cooked ?? q.value.raw) + (i < node.expressions.length ? fold(project, f, node.expressions[i], seen) : "")).join("");
    case "BinaryExpression":
      return node.operator === "+" ? fold(project, f, node.left, seen) + fold(project, f, node.right, seen) : HOLE;
    case "Identifier":
    case "MemberExpression": {
      const { hits } = resolveRef(project, f, node);
      if (hits.length !== 1 || hits[0].inits.length !== 1) return HOLE;
      const key = `${hits[0].file.rel}#${hits[0].name}`;
      if (seen.has(key)) return HOLE;
      const next = new Set(seen).add(key);
      let init = unwrap(hits[0].inits[0]);
      // obj.prop of a constant object: that property
      if (node.type === "MemberExpression" && init.type === "ObjectExpression") {
        const want = memberName(node);
        const prop = init.properties.find((q) => q.type === "ObjectProperty" && propName(q) === want);
        return want && prop ? fold(project, hits[0].file, prop.value, next) : HOLE;
      }
      return node.type === "Identifier" ? fold(project, hits[0].file, init, next) : HOLE;
    }
    case "CallExpression": {
      const c = unwrap(node.callee);
      if (c.type !== "MemberExpression") return HOLE;
      const m = memberName(c);
      if (m === "join") {
        const array = joinArray(project, f, c.object, seen);
        if (!array) return HOLE;
        const sepArg = node.arguments[0];
        const sep = sepArg ? fold(project, f, sepArg, seen) : ",";
        return array.elements.map((e) => (e && e.type !== "SpreadElement" ? fold(project, array.file, e, seen) : HOLE)).join(sep);
      }
      if (m === "concat") return fold(project, f, c.object, seen) + node.arguments.map((a) => fold(project, f, a, seen)).join("");
      if (m === "trim" || m === "trimStart" || m === "trimEnd" || m === "toString") return fold(project, f, c.object, seen);
      return HOLE;
    }
    default:
      return HOLE;
  }
}

function rootIdentifier(node) {
  let n = unwrap(node);
  while (n && (n.type === "MemberExpression" || n.type === "OptionalMemberExpression")) n = unwrap(n.object);
  return n && n.type === "Identifier" ? n : null;
}

/** A `join` receiver may be behind one or more `filter(...)` calls; nothing else is stripped, since it would
 * change which elements take part. */
function stripFilters(node) {
  let expr = unwrap(node);
  while (expr && expr.type === "CallExpression" && unwrap(expr.callee).type === "MemberExpression" && memberName(unwrap(expr.callee)) === "filter") expr = unwrap(unwrap(expr.callee).object);
  return expr;
}

/**
 * The array a `join` receiver stands for: an array literal, or a constant array reached through other modules,
 * possibly behind `filter(...)` calls. Returns its element expressions and the file they are written in, or null
 * when the receiver cannot be decided (a parameter, a function call, a cycle).
 */
function joinArray(project, f, receiver, seen = new Set()) {
  const expr = stripFilters(receiver);
  if (!expr) return null;
  if (expr.type === "ArrayExpression") return { file: f, elements: expr.elements };
  if (expr.type !== "Identifier" && expr.type !== "MemberExpression" && expr.type !== "OptionalMemberExpression") return null;
  const { hits } = resolveRef(project, f, expr);
  if (hits.length !== 1 || hits[0].inits.length !== 1) return null;
  const key = `${hits[0].file.rel}#${hits[0].name}`;
  if (seen.has(key)) return null;
  let init = unwrap(hits[0].inits[0]);
  // obj.parts of a constant object: that property's value
  if (expr.type === "MemberExpression" && init.type === "ObjectExpression") {
    const want = memberName(expr);
    const prop = init.properties.find((p) => p.type === "ObjectProperty" && propName(p) === want);
    if (!want || !prop) return null;
    init = unwrap(prop.value);
  }
  return joinArray(project, hits[0].file, init, new Set(seen).add(key));
}

/**
 * Collect class text from an expression. `direct` means the expression sits in a class position,
 * so object keys are class names (clsx style); through a reference (`ref`), an object is a lookup
 * table and its values are classes while its keys are not.
 */
function collectClass(project, f, node, mode, out, seen) {
  node = unwrap(node);
  if (!node) return;
  switch (node.type) {
    case "StringLiteral":
      out.texts.push({ text: node.value, loc: node.loc, file: f });
      return;
    case "TemplateLiteral":
      out.texts.push({ text: fold(project, f, node), loc: node.loc, file: f });
      for (const e of node.expressions) collectClass(project, f, e, mode, out, seen);
      return;
    case "JSXExpressionContainer":
      collectClass(project, f, node.expression, mode, out, seen);
      return;
    case "ConditionalExpression":
      collectClass(project, f, node.consequent, mode, out, seen);
      collectClass(project, f, node.alternate, mode, out, seen);
      return;
    case "BinaryExpression":
      if (node.operator === "+") {
        // The whole sum is what lands in the class list: "b" + "tn" is btn. Its operands are looked at as well.
        out.texts.push({ text: fold(project, f, node), loc: node.loc, file: f });
        for (const side of [node.left, node.right]) {
          const n = unwrap(side);
          if (n.type !== "StringLiteral") collectClass(project, f, n, mode, out, seen);
        }
        return;
      }
      return;
    case "LogicalExpression":
      collectClass(project, f, node.left, mode, out, seen);
      collectClass(project, f, node.right, mode, out, seen);
      return;
    case "SequenceExpression":
      collectClass(project, f, node.expressions[node.expressions.length - 1], mode, out, seen);
      return;
    case "ArrayExpression":
      for (const e of node.elements) if (e) collectClass(project, f, e.type === "SpreadElement" ? e.argument : e, mode, out, seen);
      return;
    case "ObjectExpression":
      for (const p of node.properties) {
        if (p.type === "SpreadElement") collectClass(project, f, p.argument, mode, out, seen);
        else if (mode === "direct") {
          const name = propName(p);
          if (name != null) out.texts.push({ text: name, loc: p.key.loc, file: f });
        } else if (p.type === "ObjectProperty") collectClass(project, f, p.value, mode, out, seen);
      }
      return;
    case "Identifier":
    case "MemberExpression":
    case "OptionalMemberExpression": {
      const { hits, unresolved } = resolveRef(project, f, node);
      // An import into the project that is not a constant we can evaluate (a function, a class, a cycle): say so.
      if (unresolved) out.dynamic.push({ loc: node.loc, file: f });
      for (const hit of hits) {
        const key = hit.file.rel + "#" + hit.name;
        if (seen.has(key)) continue;
        seen.add(key);
        for (const init of hit.inits) collectClass(project, hit.file, init, "ref", out, seen);
      }
      return;
    }
    case "CallExpression":
    case "OptionalCallExpression": {
      const callee = unwrap(node.callee);
      const calleeName = callee.type === "Identifier" ? callee.name : null;
      if (calleeName === "cva" || calleeName === "tv") {
        collectCva(project, f, node, out, seen);
        return;
      }
      // buttonVariants({...}): a cva() result; its own definition is scanned wherever it appears
      if (calleeName) {
        const hit = resolveBinding(project, f, calleeName);
        if (hit?.status === "ok" && hit.inits.some((i) => isCvaCall(i))) return;
      }
      if (calleeName && CLASS_FUNCTIONS.has(calleeName)) {
        for (const a of node.arguments) collectClass(project, f, a.type === "SpreadElement" ? a.argument : a, "direct", out, seen);
        return;
      }
      // [..].join(..) and "..".concat(..) build one string: look at what they build as well as at the parts
      if (callee.type === "MemberExpression" && ["join", "concat"].includes(memberName(callee) ?? "")) out.texts.push({ text: fold(project, f, node), loc: node.loc, file: f });
      // member chain pass-through: [...].filter(Boolean).join(" ")
      if (callee.type === "MemberExpression" && !callee.computed && callee.property.type === "Identifier" && PASS_THROUGH_METHODS.has(callee.property.name)) {
        // A join over a receiver that is not a known array builds a class string that cannot be judged: report it
        // rather than treating the parts as if they were the whole.
        if (callee.property.name === "join" && !joinArray(project, f, callee.object)) out.dynamic.push({ loc: node.loc, file: f });
        collectClass(project, f, callee.object, mode, out, seen);
        for (const a of node.arguments) collectClass(project, f, a, mode, out, seen);
        return;
      }
      out.dynamic.push({ loc: node.loc, file: f });
      return;
    }
    case "TaggedTemplateExpression":
      out.dynamic.push({ loc: node.loc, file: f });
      return;
    default:
      return;
  }
}

function isCvaCall(n) {
  n = unwrap(n);
  return !!n && n.type === "CallExpression" && n.callee.type === "Identifier" && (n.callee.name === "cva" || n.callee.name === "tv");
}

function collectCva(project, f, call, out, seen) {
  const [base, config] = call.arguments;
  if (base) collectClass(project, f, base, "direct", out, seen);
  const cfg = unwrap(config);
  if (!cfg || cfg.type !== "ObjectExpression") return;
  for (const p of cfg.properties) {
    if (p.type !== "ObjectProperty") continue;
    const name = propName(p);
    const val = unwrap(p.value);
    if (name === "base") collectClass(project, f, val, "direct", out, seen);
    if (name === "variants" && val.type === "ObjectExpression") {
      for (const group of val.properties) {
        const g = group.type === "ObjectProperty" ? unwrap(group.value) : null;
        if (g && g.type === "ObjectExpression") for (const opt of g.properties) if (opt.type === "ObjectProperty") collectClass(project, f, opt.value, "direct", out, seen);
      }
    }
    if (name === "compoundVariants" && val.type === "ArrayExpression") {
      for (const e of val.elements) {
        const o = unwrap(e);
        if (o && o.type === "ObjectExpression") for (const q of o.properties) if (q.type === "ObjectProperty" && ["class", "className"].includes(propName(q))) collectClass(project, f, q.value, "direct", out, seen);
      }
    }
  }
}

const isClassAttr = (name) => name === "className" || name === "class" || /ClassName$/.test(name);

function jsxName(n) {
  if (n.type === "JSXIdentifier") return n.name;
  if (n.type === "JSXMemberExpression") return jsxName(n.property);
  if (n.type === "JSXNamespacedName") return n.name.name;
  return "";
}

function stringValueOf(attrValue) {
  if (!attrValue) return null;
  if (attrValue.type === "StringLiteral") return attrValue.value;
  if (attrValue.type === "JSXExpressionContainer") {
    const e = unwrap(attrValue.expression);
    if (e.type === "StringLiteral") return e.value;
    if (e.type === "TemplateLiteral" && e.expressions.length === 0) return e.quasis[0].value.cooked;
  }
  return null;
}

/** Scan one file; returns [{file, line, rule, detail}]. */
export function scanFile(project, f) {
  const found = [];
  const add = (rule, loc, detail) => found.push({ file: f.rel, line: loc?.start.line ?? 0, rule, detail });
  const inPrimitives = f.rel.startsWith(PRIMITIVES_DIR);

  const checkClasses = (node, mode) => {
    const out = { texts: [], dynamic: [] };
    collectClass(project, f, node, mode, out, new Set());
    for (const d of out.dynamic) {
      // A constant followed into another module can be undecidable there, but the risk is at the class expression
      // that consumes it: report that, with the location it came from, instead of dropping it because the two
      // files differ.
      if (d.file === f) add("dynamic-class", d.loc, "class expression cannot be decided statically");
      else add("dynamic-class", node.loc, `class expression cannot be decided statically (from ${d.file.rel}:${d.loc?.start.line ?? 0})`);
    }
    for (const t of out.texts) {
      for (const { token, partial } of classTokens(t.text)) {
        if (!partial && LEGACY_CLASSES.has(token)) add("legacy-class", f === t.file ? t.loc : node.loc, `"${token}"${t.file === f ? "" : ` (from ${t.file.rel})`}`);
        else if (partial) {
          const m = partialMatchesLegacy(token);
          if (m) add("legacy-class", f === t.file ? t.loc : node.loc, `dynamic "${token.replaceAll(HOLE, "${…}")}" can produce ${m}`);
        }
      }
    }
  };

  walk(f.ast.program, (n) => {
    if (n.type === "JSXOpeningElement") {
      const name = jsxName(n.name);
      if (n.name.type === "JSXIdentifier" && NATIVE_ELEMENTS.has(name)) add("native-element", n.loc, `<${name}>`);
      if (n.name.type === "JSXIdentifier" && name === "Input") {
        for (const a of n.attributes) {
          if (a.type === "JSXAttribute" && a.name.name === "type") {
            const v = stringValueOf(a.value);
            if (v === "checkbox" || v === "radio") add("native-input-type", n.loc, `<Input type="${v}">`);
          }
        }
      }
      for (const a of n.attributes) if (a.type === "JSXAttribute" && a.name.type === "JSXIdentifier" && isClassAttr(a.name.name) && a.value) checkClasses(a.value, "direct");
    }
    // a cva() definition is checked wherever it is written, referenced or not
    if (isCvaCall(n)) checkClasses(n, "direct");
    if (n.type === "ObjectProperty" && ["className", "class"].includes(propName(n) ?? "")) checkClasses(n.value, "direct");
    // createElement("button") / jsx("button") bypass
    if (n.type === "CallExpression") {
      const c = unwrap(n.callee);
      const cn = c.type === "Identifier" ? c.name : c.type === "MemberExpression" && c.property.type === "Identifier" ? c.property.name : "";
      if (["createElement", "jsx", "jsxs", "jsxDEV"].includes(cn)) {
        const first = unwrap(n.arguments[0]);
        if (first && first.type === "StringLiteral" && NATIVE_ELEMENTS.has(first.value)) add("native-element", n.loc, `createElement("${first.value}")`);
      }
    }
    // Radix imports
    const src = n.type === "ImportDeclaration" || n.type === "ExportNamedDeclaration" || n.type === "ExportAllDeclaration" ? n.source?.value : null;
    const dyn = n.type === "ImportExpression" && n.source.type === "StringLiteral" ? n.source.value : null;
    const req = n.type === "CallExpression" && n.callee.type === "Identifier" && n.callee.name === "require" && n.arguments[0]?.type === "StringLiteral" ? n.arguments[0].value : null;
    for (const s of [src, dyn, req]) if (typeof s === "string" && (s === "radix-ui" || s.startsWith("radix-ui/") || s.startsWith("@radix-ui/")) && !inPrimitives) add("radix-import", n.loc, `import of "${s}"`);
  });
  const seen = new Set();
  return found.filter((v) => {
    const k = `${v.line}|${v.rule}|${v.detail}`;
    return seen.has(k) ? false : (seen.add(k), true);
  });
}

export function scanCss(code, rel = "src/styles.css") {
  const stripped = code.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "));
  const found = [];
  const re = /@layer\s+([^{;]+)/g;
  let m;
  while ((m = re.exec(stripped))) {
    if (m[1].split(",").map((s) => s.trim()).includes("components")) found.push({ file: rel, line: stripped.slice(0, m.index).split("\n").length, rule: "css-layer", detail: "@layer components" });
  }
  return found;
}

/** Whole-project check. Every finding is a failure; there is no way to allow one. */
export function checkProject(root, srcDir = "src") {
  const project = loadProject(root, srcDir);
  const all = [];
  for (const f of project.files.values()) all.push(...scanFile(project, f));
  const css = join(root, srcDir, "styles.css");
  if (existsSync(css)) all.push(...scanCss(readFileSync(css, "utf8"), toPosix(relative(root, css))));

  // primitives: native elements are what shadcn generates
  const violations = all.filter((v) => !(v.rule === "native-element" && GENERATED_PRIMITIVES.has(v.file)));
  return { violations };
}

function main() {
  const here = dirname(fileURLToPath(import.meta.url));
  const root = resolve(here, "..");
  const { violations } = checkProject(root);
  for (const v of violations) console.error(`${v.file}:${v.line}  ${v.rule}  ${v.detail}`);
  if (violations.length) {
    console.error(`check-shadcn: ${violations.length} violation(s); every finding fails, there is no allowlist`);
    process.exit(1);
  }
  console.log("check-shadcn: ok");
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
