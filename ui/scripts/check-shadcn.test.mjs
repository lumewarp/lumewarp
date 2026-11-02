// Tests for check-shadcn.mjs: every bypass has a failing fixture, every look-alike a passing one.
// Run: node --test scripts/check-shadcn.test.mjs

import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { after, describe, test } from "node:test";

import { checkProject } from "./check-shadcn.mjs";

const dirs = [];
after(() => dirs.forEach((d) => rmSync(d, { recursive: true, force: true })));

/** Build a throwaway project from {path: source} and check it. */
function run(files, config = {}) {
  const root = mkdtempSync(join(tmpdir(), "check-shadcn-"));
  dirs.push(root);
  for (const [rel, code] of Object.entries(files)) {
    const p = join(root, rel);
    mkdirSync(dirname(p), { recursive: true });
    writeFileSync(p, code);
  }
  return checkProject(root, config);
}

const page = (body, head = "") => ({ "src/pages/P.tsx": `${head}\nexport function P() { return ${body}; }\n` });
const rulesOf = (r) => r.violations.map((v) => v.rule);
const flags = (files, rule, detail) => {
  const r = run(files);
  assert.ok(r.violations.some((v) => v.rule === rule && (!detail || v.detail.includes(detail))), `expected ${rule} ${detail ?? ""}, got ${JSON.stringify(r.violations)}`);
};
const clean = (files, config) => {
  const r = run(files, config);
  assert.deepEqual(r.violations, [], JSON.stringify(r.violations));
};

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
  test("primitives are not exempt from legacy classes", () => flags({ "src/components/ui/x.tsx": `export const T = () => <div className="btn" />;` }, "legacy-class"));
  test("adapter exemption covers only the named element", () => {
    const files = { "src/components/Hidden.tsx": `export const H = () => <><input /><button /></>;` };
    const r = run(files, { adapters: { "src/components/Hidden.tsx": ["input"] } });
    assert.deepEqual(r.violations.map((v) => v.detail), ["<button>"]);
  });
  test("adapter exemption never covers legacy classes", () => {
    const r = run({ "src/components/A.tsx": `export const A = () => <input className="btn" />;` }, { adapters: { "src/components/A.tsx": ["*"] } });
    assert.deepEqual(rulesOf(r), ["legacy-class"]);
  });
  test("adapters cannot name a legacy class", () => {
    const r = run({}, { adapters: { "src/x.tsx": ["btn"] } });
    assert.equal(r.errors.length, 1);
  });
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

describe("allowlist", () => {
  const files = page(`<div className="btn btn-sm"><button /></div>`);
  test("exact count passes", () => clean(files, { allowlist: { "src/pages/P.tsx": { "legacy-class": 2, "native-element": 1 } } }));
  test("one more than allowed fails", () => {
    const r = run(files, { allowlist: { "src/pages/P.tsx": { "legacy-class": 1, "native-element": 1 } } });
    assert.deepEqual(rulesOf(r), ["legacy-class", "legacy-class"]);
  });
  test("an unlisted rule in a listed file fails", () => {
    const r = run(files, { allowlist: { "src/pages/P.tsx": { "legacy-class": 2 } } });
    assert.deepEqual(rulesOf(r), ["native-element"]);
  });
  test("another file is not covered", () => {
    const r = run({ ...files, ...{ "src/pages/Q.tsx": `export const Q = () => <i className="btn" />;` } }, { allowlist: { "src/pages/P.tsx": { "legacy-class": 2, "native-element": 1 } } });
    assert.equal(r.violations.length, 1);
    assert.equal(r.violations[0].file, "src/pages/Q.tsx");
  });
  test("fixed files are reported as can shrink, not failed", () => {
    const r = run(page(`<div />`), { allowlist: { "src/pages/P.tsx": { "legacy-class": 3 } } });
    assert.deepEqual(r.violations, []);
    assert.deepEqual(r.stale, [{ file: "src/pages/P.tsx", rule: "legacy-class", allowed: 3, actual: 0 }]);
  });
  test("bad allowlist entries are config errors", () => {
    assert.equal(run({}, { allowlist: { "src/x.tsx": { nope: 1, "legacy-class": 0 } } }).errors.length, 2);
  });
});
