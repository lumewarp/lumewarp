// Tests for check-shadcn.mjs: every bypass has a failing fixture, every look-alike a passing one.
// Run: node --test scripts/check-shadcn.test.mjs

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { after, describe, test } from "node:test";
import { fileURLToPath } from "node:url";

import { checkProject } from "./check-shadcn.mjs";

const scriptsDir = dirname(fileURLToPath(import.meta.url));

const dirs = [];
after(() => dirs.forEach((d) => rmSync(d, { recursive: true, force: true })));

/** Build a throwaway project from {path: source} and check it. */
function run(files) {
  const root = mkdtempSync(join(tmpdir(), "check-shadcn-"));
  dirs.push(root);
  for (const [rel, code] of Object.entries(files)) {
    const p = join(root, rel);
    mkdirSync(dirname(p), { recursive: true });
    writeFileSync(p, code);
  }
  return checkProject(root);
}

const page = (body, head = "") => ({ "src/pages/P.tsx": `${head}\nexport function P() { return ${body}; }\n` });
const rulesOf = (r) => r.violations.map((v) => v.rule);
const flags = (files, rule, detail) => {
  const r = run(files);
  assert.ok(r.violations.some((v) => v.rule === rule && (!detail || v.detail.includes(detail))), `expected ${rule} ${detail ?? ""}, got ${JSON.stringify(r.violations)}`);
};
const clean = (files) => {
  const r = run(files);
  assert.deepEqual(r.violations, [], JSON.stringify(r.violations));
};

/** A throwaway copy of the checker (it finds @babel/parser through the symlinked node_modules) plus `files`. */
function cliProject(files, { allowlist } = {}) {
  const root = mkdtempSync(join(tmpdir(), "check-shadcn-cli-"));
  dirs.push(root);
  mkdirSync(join(root, "scripts"), { recursive: true });
  copyFileSync(join(scriptsDir, "check-shadcn.mjs"), join(root, "scripts/check-shadcn.mjs"));
  symlinkSync(join(scriptsDir, "..", "node_modules"), join(root, "node_modules"));
  for (const [rel, code] of Object.entries(files)) {
    mkdirSync(dirname(join(root, rel)), { recursive: true });
    writeFileSync(join(root, rel), code);
  }
  if (allowlist) writeFileSync(join(root, "scripts/check-shadcn.allowlist.json"), JSON.stringify(allowlist));
  return { root, script: join(root, "scripts/check-shadcn.mjs") };
}

/** Run the CLI on a throwaway copy of the project: the same entry point `pnpm check` runs on the real tree. */
function runCli(files, { allowlist, args = [] } = {}) {
  const { root, script } = cliProject(files, { allowlist });
  const result = spawnSync(process.execPath, [script, ...args], { encoding: "utf8" });
  return { ...result, root, script };
}

describe("legacy classes: each way to write one is caught", () => {
  test("className string", () => flags(page(`<div className="btn" />`), "legacy-class", '"btn"'));
  test("className among other tokens, with a variant prefix", () => flags(page(`<div className="flex md:hover:btn-sm gap-2" />`), "legacy-class", '"btn-sm"'));
  test("className expression container", () => flags(page(`<div className={"card pad"} />`), "legacy-class", '"card"'));
  test("conditional", () => flags(page(`<div className={x ? "stats" : "kv"} />`), "legacy-class", '"stats"'));
  test("cn()", () => flags(page(`<div className={cn("p-2", "field-hint")} />`, `import { cn } from "@/lib/utils";`), "legacy-class", '"field-hint"'));
  test("cn() with object key", () => flags(page(`<div className={cn({ "state-error": bad })} />`, `import { cn } from "@/lib/utils";`), "legacy-class", '"state-error"'));
  test("clsx()", () => flags(page(`<div className={clsx("a", cond && "toolbar")} />`, `import clsx from "clsx";`), "legacy-class", '"toolbar"'));
  test("cva() base, variant and compound variant", () => {
    const head = `import { cva } from "class-variance-authority";
      const v = cva("btn", { variants: { size: { sm: "btn-sm", lg: "p-4" } }, compoundVariants: [{ size: "lg", class: "tight" }] });`;
    const r = run(page(`<div className={v()} />`, head));
    assert.deepEqual(r.violations.map((x) => x.detail).sort(), ['"btn"', '"btn-sm"', '"tight"']);
  });
  test("cva() that nothing references is still checked", () => flags({ "src/a.ts": `import { cva } from "x"; export const v = cva("pager");` }, "legacy-class", '"pager"'));
  test("template string, static part", () => flags(page("<div className={`flex ${x} muted`} />"), "legacy-class", '"muted"'));
  test("template string, dynamic part that can build a legacy class", () => flags(page("<div className={`btn-${size}`} />"), "legacy-class", "\"btn-sm\""));
  test("template string, expression value is itself checked", () => flags(page("<div className={`flex ${x ? 'login' : ''}`} />"), "legacy-class", '"login"'));
  test("string concatenation", () => flags(page(`<div className={"a " + "login-card"} />`), "legacy-class", '"login-card"'));
  test("array join", () => flags(page(`<div className={["a", "panel-head"].filter(Boolean).join(" ")} />`), "legacy-class", '"panel-head"'));
  test("referenced const in the same file", () => flags(page(`<div className={BOX} />`, `const BOX = "section-gap";`), "legacy-class", '"section-gap"'));
  test("referenced const object member", () => flags(page(`<div className={S.row} />`, `const S = { row: "target-row" };`), "legacy-class", '"target-row"'));
  test("referenced const inside cn()", () => flags(page(`<div className={cn("a", BOX)} />`, `const BOX = "split-card";`), "legacy-class", '"split-card"'));
  test("constant from a .ts file, alias import", () =>
    flags({ "src/lib/styles.ts": `export const BTN = "btn btn-primary";`, ...page(`<div className={BTN} />`, `import { BTN } from "@/lib/styles";`) }, "legacy-class", '"btn"'));
  test("constant from a .ts file, relative import", () =>
    flags({ "src/lib/styles.ts": `export const S = { x: "page-head" };`, "src/pages/Q.tsx": `import { S } from "../lib/styles";\nexport const Q = () => <i className={S.x} />;` }, "legacy-class", '"page-head"'));
  test("constant chain through two modules", () =>
    flags(
      { "src/lib/a.ts": `export const A = "kv";`, "src/lib/b.ts": `import { A } from "./a";\nexport const B = A;`, ...page(`<i className={B} />`, `import { B } from "@/lib/b";`) },
      "legacy-class",
      '"kv"',
    ));
  test("className as an object property", () => flags({ "src/a.tsx": `export const p = { className: "filters" };` }, "legacy-class", '"filters"'));
  test("custom *ClassName prop", () => flags(page(`<Foo contentClassName="plain" />`), "legacy-class", '"plain"'));
  test("TS cast and satisfies do not hide it", () => flags(page(`<i className={("order-row" as string)} />`), "legacy-class", '"order-row"'));
  test("an unknown function in a class position is reported, not trusted", () => flags(page(`<i className={pick(x)} />`), "dynamic-class"));
  test("tagged template is reported", () => flags(page("<i className={tw`flex`} />"), "dynamic-class"));
});

describe("legacy classes: a class built from pieces is judged by what it builds", () => {
  test('"b" + "tn"', () => flags(page(`<div className={"b" + "tn"} />`), "legacy-class", '"btn"'));
  test("sum with a space-free tail", () => flags(page(`<div className={"flex " + "pa" + "ger"} />`), "legacy-class", '"pager"'));
  test('["b", "tn"].join("")', () => flags(page(`<div className={["b", "tn"].join("")} />`), "legacy-class", '"btn"'));
  test('parts.join("") with a local constant array', () => {
    const files = page(`<div className={parts.join("")} />`, `const parts = ["b", "tn"];`);
    flags(files, "legacy-class", '"btn"');
    const cli = runCli(files);
    assert.equal(cli.status, 1, cli.stderr);
    assert.match(cli.stderr, /src\/pages\/P\.tsx:2\s+legacy-class\s+"btn"/);
  });
  test('parts.join("") with an imported constant array', () => {
    const files = { "src/lib/styles.ts": `export const parts = ["b", "tn"];`, ...page(`<div className={parts.join("")} />`, `import { parts } from "@/lib/styles";`) };
    flags(files, "legacy-class", '"btn"');
    const cli = runCli(files);
    assert.equal(cli.status, 1, cli.stderr);
    assert.match(cli.stderr, /src\/pages\/P\.tsx:2\s+legacy-class\s+"btn"/);
  });
  test('the join separator is kept: parts.join(" ") is not btn', () => clean(page(`<div className={parts.join(" ")} />`, `const parts = ["b", "tn"];`)));
  test("a join receiver that cannot be resolved is dynamic, never safe", () => flags(page(`<div className={parts.join("")} />`, `declare const parts: string[];`), "dynamic-class"));
  test("a join over a function call is dynamic, not a crash", () => flags(page(`<div className={makeParts().join("")} />`), "dynamic-class"));
  test("join after filter(Boolean)", () => flags(page(`<div className={["b", cond && "tn"].filter(Boolean).join("")} />`), "legacy-class", "btn"));
  test('"b".concat("tn")', () => flags(page(`<div className={"b".concat("tn")} />`), "legacy-class", '"btn"'));
  test("template with a constant part", () => flags(page("<div className={`b${\"tn\"}`} />"), "legacy-class", '"btn"'));
  test("constants added together", () => flags(page(`<div className={A + B} />`, `const A = "b"; const B = "tn";`), "legacy-class", '"btn"'));
  test("constant from another module added to a literal", () =>
    flags({ "src/lib/s.ts": `export const TAIL = "tn";`, ...page(`<div className={"b" + TAIL} />`, `import { TAIL } from "@/lib/s";`) }, "legacy-class", '"btn"'));
  test("member of a constant object", () => flags(page(`<div className={"b" + PARTS.tail} />`, `const PARTS = { tail: "tn" };`), "legacy-class", '"btn"'));
  test("an unknown tail that could complete a legacy class", () => flags(page(`<div className={"bt" + suffix} />`), "legacy-class", "can produce"));
  test("an unknown head that could complete a legacy class", () => flags(page(`<div className={prefix + "-sm"} />`), "legacy-class", "btn-sm"));
  test("sum whose part is a conditional", () => flags(page(`<div className={"b" + (x ? "tn" : "")} />`), "legacy-class"));
  test("sum of unrelated pieces is fine", () => clean(page(`<div className={"flex " + x + " px-2" + ["a", "b"].join(" ")} />`)));
  test("sums that are not classes are fine", () => clean(page(`<div className={"bg-" + tone + "-100"} />`)));
  test("a lone unknown part in a template is fine", () => clean(page("<div className={`flex ${x} gap-2`} />")));
});

describe("legacy classes: through exports, imports and re-exports", () => {
  const consumer = (head) => page(`<div className={X} />`, head);
  test("export { LEGACY as alias }", () =>
    flags({ "src/lib/s.ts": `const LEGACY = "btn";\nexport { LEGACY as buttonClass };`, ...page(`<div className={buttonClass} />`, `import { buttonClass } from "../lib/s";`) }, "legacy-class", '"btn"'));
  test("export default of a name, default import", () =>
    flags({ "src/lib/s.ts": `const LEGACY = "btn";\nexport default LEGACY;`, ...page(`<div className={cls} />`, `import cls from "../lib/s";`) }, "legacy-class", '"btn"'));
  test("export default of an expression", () =>
    flags({ "src/lib/s.ts": `export default "card pad";`, ...page(`<div className={cls} />`, `import cls from "@/lib/s";`) }, "legacy-class", '"card"'));
  test("export default of an object, member used", () =>
    flags({ "src/lib/s.ts": `export default { row: "target-row" };`, ...page(`<div className={S.row} />`, `import S from "@/lib/s";`) }, "legacy-class", '"target-row"'));
  test("barrel: export { A } from", () =>
    flags({ "src/lib/a.ts": `export const A = "kv";`, "src/lib/index.ts": `export { A } from "./a";`, ...page(`<div className={A} />`, `import { A } from "@/lib";`) }, "legacy-class", '"kv"'));
  test("barrel: export { A as B } from", () =>
    flags({ "src/lib/a.ts": `export const A = "kv";`, "src/lib/index.ts": `export { A as B } from "./a";`, ...page(`<div className={B} />`, `import { B } from "@/lib";`) }, "legacy-class", '"kv"'));
  test("barrel: export * from, two levels", () =>
    flags(
      { "src/lib/a.ts": `export const A = "kv";`, "src/lib/b.ts": `export * from "./a";`, "src/lib/index.ts": `export * from "./b";`, ...page(`<div className={A} />`, `import { A } from "@/lib";`) },
      "legacy-class",
      '"kv"',
    ));
  test("an imported name exported again", () =>
    flags({ "src/lib/a.ts": `export const A = "kv";`, "src/lib/b.ts": `import { A } from "./a";\nexport { A };`, ...page(`<div className={A} />`, `import { A } from "@/lib/b";`) }, "legacy-class", '"kv"'));
  test("namespace import, named member", () =>
    flags({ "src/lib/s.ts": `export const row = "target-row";`, ...page(`<div className={S.row} />`, `import * as S from "@/lib/s";`) }, "legacy-class", '"target-row"'));
  test("namespace import, computed member sees every export", () =>
    flags({ "src/lib/s.ts": `export const row = "target-row";`, ...page(`<div className={S[k]} />`, `import * as S from "@/lib/s";`) }, "legacy-class", '"target-row"'));
  test("a cycle is not followed forever and is reported", () =>
    flags({ "src/lib/a.ts": `export { A } from "./b";`, "src/lib/b.ts": `export { A } from "./a";`, ...page(`<div className={A} />`, `import { A } from "@/lib/a";`) }, "dynamic-class"));
  test("an import into the project that is not a constant is dynamic-class", () =>
    flags({ "src/lib/s.ts": `export function cls() { return "btn"; }`, ...page(`<div className={cls} />`, `import { cls } from "@/lib/s";`) }, "dynamic-class"));
  test("an import of a name the module does not export is dynamic-class", () =>
    flags({ "src/lib/s.ts": `export const other = "x";`, ...page(`<div className={missing} />`, `import { missing } from "@/lib/s";`) }, "dynamic-class"));
  test("a package import is not ours to follow", () => clean(page(`<div className={cls} />`, `import cls from "some-package";`)));
  test("an import that is a plain constant of other text is fine", () =>
    clean({ "src/lib/s.ts": `const OK = "flex gap-2";\nexport { OK as row };\nexport default OK;`, ...consumer(`import X from "@/lib/s";`) }));
  test("the checked file is not blamed for a dynamic import elsewhere", () =>
    clean({ "src/lib/s.ts": `export function cls() { return "x"; }`, "src/pages/Q.tsx": `export const Q = () => <i />;` }));
  test("a call in another module's constant is dynamic at the consumer, with its source", () => {
    const files = {
      "src/lib/styles.ts": `function makeClass() { return "btn"; }\nexport const BOX = makeClass();`,
      ...page(`<div className={BOX} />`, `import { BOX } from "@/lib/styles";`),
    };
    const r = run(files);
    assert.deepEqual(r.violations.map((v) => [v.file, v.rule]), [["src/pages/P.tsx", "dynamic-class"]]);
    assert.match(r.violations[0].detail, /from src\/lib\/styles\.ts:2/);
    const cli = runCli(files);
    assert.equal(cli.status, 1, cli.stderr);
    assert.match(cli.stderr, /src\/pages\/P\.tsx:2\s+dynamic-class\s+class expression cannot be decided statically \(from src\/lib\/styles\.ts:2\)/);
  });
});

describe("legacy classes: look-alikes are not flagged", () => {
  test("text-muted-foreground and friends", () => clean(page(`<p className="text-muted-foreground bg-muted/50 muted-foo foo-muted btn2 card-title" />`)));
  test('variant "muted" is a name, not a class', () => clean(page(`<Badge variant="muted" />`)));
  test('cva variant key "muted" is not a class', () => {
    clean(page(`<i className={v({ tone: "muted" })} />`, `import { cva } from "x"; const v = cva("rounded", { variants: { tone: { muted: "bg-muted", warn: "bg-warn" } } });`));
  });
  test("lookup object keys are not classes, only values", () => clean(page(`<i className={TONES[t]} />`, `const TONES = { muted: "bg-muted", card: "bg-card" };`)));
  test("data-slot and test ids that equal a legacy name", () => clean(page(`<div data-slot="card" data-testid="stat" id="field" />`)));
  test("plain strings outside class positions", () => clean({ "src/lib/k.ts": `export const labels = { state: "State", muted: "x", kv: "kv" };` }));
  test("props and parameters pass through", () => clean(page(`<i className={cn("p-2", className)} />`, `import { cn } from "@/lib/utils"; declare const className: string;`)));
  test("calls of a cva result are fine", () => clean(page(`<i className={cn(buttonVariants({ size: "sm" }))} />`, `import { cva } from "x"; import { cn } from "@/lib/utils"; const buttonVariants = cva("p-2", { variants: { size: { sm: "p-1" } } });`)));
  test("a quoted legacy name inside a longer unrelated token", () => clean(page(`<i className="has-[>.card]:p-2 [&_.btn]:p-1 data-[state=open]:bg-accent" />`)));
});

describe("native elements", () => {
  for (const el of ["button", "select", "input", "textarea", "table", "datalist", "option"]) {
    test(`<${el}>`, () => flags(page(`<${el} />`), "native-element", `<${el}>`));
  }
  test("createElement", () => flags({ "src/a.tsx": `import { createElement } from "react"; export const a = createElement("button", null);` }, "native-element"));
  test("PascalCase components are fine", () => clean(page(`<Button><Input /><Select /><Table /></Button>`)));
  test("member components named like elements are fine", () => clean(page(`<Foo.button />`)));
  test('<Input type="checkbox">', () => flags(page(`<Input type="checkbox" />`), "native-input-type"));
  test('<Input type="radio">', () => flags(page(`<Input type="radio" />`), "native-input-type"));
  test("<Input type={'checkbox'}>", () => flags(page(`<Input type={"checkbox"} />`), "native-input-type"));
  test('<Input type="text"> and <Input type="date"> are fine', () => clean(page(`<><Input type="text" /><Input type="date" /><Input type={t} /></>`)));
  test("shadcn primitives may use native elements", () => clean({ "src/components/ui/table.tsx": `export const T = () => <table><tbody /></table>;` }));
  test("generated select may use <select> and <option>", () => clean({ "src/components/ui/select.tsx": `export const S = () => <select><option /></select>;` }));
  test("a hand-written file in components/ui is not exempt: combobox-field", () => {
    const r = run({ "src/components/ui/combobox-field.tsx": `export const C = () => <><select><option /></select><button /></>;` });
    assert.deepEqual(rulesOf(r), ["native-element", "native-element", "native-element"]);
  });
  test("a file we add to components/ui is not exempt until it is listed", () => flags({ "src/components/ui/description-list.tsx": `export const D = () => <table />;` }, "native-element"));
  test("the generated list names files that exist in the repo", async () => {
    const { GENERATED_PRIMITIVES } = await import("./check-shadcn.mjs");
    const { existsSync } = await import("node:fs");
    const { fileURLToPath } = await import("node:url");
    const root = join(dirname(fileURLToPath(import.meta.url)), "..");
    for (const f of GENERATED_PRIMITIVES) assert.ok(existsSync(join(root, f)), `${f} is listed as generated but missing`);
    assert.ok(!GENERATED_PRIMITIVES.has("src/components/ui/combobox-field.tsx"));
  });
  test("primitives are not exempt from legacy classes", () => flags({ "src/components/ui/x.tsx": `export const T = () => <div className="btn" />;` }, "legacy-class"));
});

describe("radix imports", () => {
  test("radix-ui", () => flags({ "src/components/D.tsx": `import { Dialog } from "radix-ui";` }, "radix-import"));
  test("@radix-ui/*", () => flags({ "src/pages/D.tsx": `import * as S from "@radix-ui/react-select";` }, "radix-import"));
  test("subpath", () => flags({ "src/pages/D.tsx": `import { x } from "radix-ui/internal";` }, "radix-import"));
  test("re-export", () => flags({ "src/pages/D.tsx": `export { Dialog } from "radix-ui";` }, "radix-import"));
  test("dynamic import", () => flags({ "src/pages/D.tsx": `export const l = () => import("@radix-ui/react-popover");` }, "radix-import"));
  test("allowed inside components/ui", () => clean({ "src/components/ui/dialog.tsx": `import { Dialog } from "radix-ui"; import * as P from "@radix-ui/react-popover";` }));
  test("unrelated packages are fine", () => clean({ "src/pages/D.tsx": `import { x } from "radix-ui-like"; import y from "@radixish/z";` }));
});

describe("css", () => {
  test("@layer components", () => flags({ "src/styles.css": `@layer base { a { color: red } }\n@layer components { .btn { color: red } }\n`, "src/a.ts": "export {}" }, "css-layer"));
  test("@layer list containing components", () => flags({ "src/styles.css": `@layer base, components;\n`, "src/a.ts": "export {}" }, "css-layer"));
  test("commented-out layer and other layers are fine", () => clean({ "src/styles.css": `/* @layer components { } */\n@layer base { a { color: red } }\n@theme inline { --x: 1 }`, "src/a.ts": "export {}" }));
});

describe("no allowlist: any finding fails", () => {
  const page = (body) => ({ "src/pages/P.tsx": `export function P() { return ${body}; }\n` });
  test("a legacy class fails the check", () => assert.deepEqual(rulesOf(run(page(`<div className="btn" />`))), ["legacy-class"]));
  test("a native element fails the check", () => assert.deepEqual(rulesOf(run(page(`<button />`))), ["native-element"]));
  test("the CLI fails on a finding, and no config file can allow it", () => {
    const files = page(`<div className="btn" />`);
    const allowlist = { "src/pages/P.tsx": { "legacy-class": 9 } };
    const failed = runCli(files, { allowlist });
    assert.equal(failed.status, 1);
    assert.match(failed.stderr, /src\/pages\/P\.tsx:1\s+legacy-class\s+"btn"/);
    assert.match(failed.stderr, /every finding fails, there is no allowlist/);
    // --write-allowlist and --report are gone: the flags are not a way to pass, and nothing is written.
    const flag = runCli(files, { allowlist, args: ["--write-allowlist", "--report"] });
    assert.equal(flag.status, 1);
    assert.deepEqual(readdirSync(join(flag.root, "scripts")), ["check-shadcn.allowlist.json", "check-shadcn.mjs"]);
  });
  test("the CLI passes a clean project", () => {
    const ok = runCli(page(`<Button />`));
    assert.equal(ok.status, 0, ok.stderr);
    assert.equal(ok.stdout.trim(), "check-shadcn: ok");
  });
});
