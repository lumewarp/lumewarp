# shadcn 遷移計畫

目標：`ui/` 的每個畫面都由 shadcn 元件組成，刪掉 `styles.css` 的舊 class，之後也不會退回去。API 請求、驗證規則、權限判斷都不動，只換呈現層；確實會變的行為列在「核准的行為變更」。

基準：main @ 0d2c6db。

## 現況

已完成：Tailwind v4、主題、`components/ui/` 的 13 個元件（alert、badge、button、card、checkbox、dialog、input、label、radio-group、skeleton、sonner、table、textarea）。`Toast.tsx`、`Search.tsx` 已經是 shadcn，不用重做。

| 還沒換的東西 | 數量 | 位置 |
|---|---|---|
| `styles.css` 的舊 class | 45 個 class，`className` 用了 189 次；最多的是 `btn` 27、`muted` 20、`btn-sm` 18、`field-hint` 13、`field-error` 11 | `Login`、`TeamDetail`、`Settings`、`Routers`、`Users`、`Keys`、`Gateways` 最多 |
| 原生 `<button>` | 29 | `TeamDetail` 6、`Routers` 5、`Keys` 4、`Users` 4、`Settings` 3、`Gateways` 2、`Login` 2、`Config`、`Layout`、`Table` |
| 原生 `<select>` | 4，全站下拉都經過這裡 | `Form.tsx::SelectField`、`SchemaForm.tsx` 2、`Routers.tsx` 1 |
| `<datalist>` | 1 | `Usage.tsx` 上游模型篩選 |
| 自製 tabs | 1 | `TeamDetail.tsx` |
| 自製 sidebar 和手機導覽 | 194 行 | `Layout.tsx` |
| 直接用 Radix 的 Dialog，drawer 是自己的變體 | 1 | `Dialog.tsx`（`ui/dialog.tsx` 目前沒人用） |
| 刪除確認用一般 Dialog | 1 | `Confirm.tsx` |

Bundle 基準（既有 `dist`）：JS 607,402 bytes，gzip 182,764 bytes；CSS 53,987 bytes，gzip 9,344 bytes。s0 開始前用乾淨 build 重量一次，以那個數字為準。

## 決定

### 元件對照

| 現在 | 換成 | 說明 |
|---|---|---|
| `.btn*`、原生 `<button>` | `ui/button` | 含 tab、pager、表格內的按鈕 |
| 舊 class 的卡片、`.stats`、`.kv` | `ui/card`；`.kv` 用自建的 `ui/description-list` | |
| `Dialog.tsx` 的 dialog | `ui/dialog` | 對外介面不變 |
| `Dialog.tsx` 的 drawer | `ui/sheet` | 見下方 Sheet |
| `ConfirmDialog` | `ui/alert-dialog` | 見下方確認對話框 |
| 固定選項（見清單）、schema enum | `SelectField`，內部 `ui/select` | 見下方 Select |
| 資料決定長度的清單（見清單） | `ComboboxField`，內部 popover + command | 見下方 Combobox |
| Usage 上游模型的 `<datalist>` | `ComboboxField` 的自由輸入版 | 要能輸入清單裡沒有的已退役名字 |
| 自製 tabs | `ui/tabs` | 見下方 Tabs |
| `Layout.tsx` | `ui/sidebar` | 見下方 Sidebar |
| `.field-*`、`.form-*`、`.form-section` | `ui/field` | 不引入 react-hook-form，見下方 |
| `.form-warn`、`.form-error` | `ui/alert` | |
| `.state`、`.state-error`、空狀態 | `ui/empty`、`ui/alert` | |
| `.pager` | `ui/button` 組合 | API 是 cursor，沒有頁碼，shadcn Pagination 不適用 |
| `.muted`、`.section-gap`、`.toolbar`、`.page-actions`、`.brand*`、`.login*` 等版面 class | Tailwind utility | |
| `<input type="date">` | 保留原生，外面是 `ui/input` | 見待決定 1 |
| JSON 編輯器 | `ui/textarea` | |

### 哪些用 Select，哪些用 Combobox

| 元件 | 使用處 |
|---|---|
| `SelectField` | Role（`TeamDetail`）、Kind、Streaming usage（`Upstreams`）、Sticky（`Routers`）、Group by（`TeamDetail`、`Usage`）、schema enum 和布林（`SchemaForm`） |
| `ComboboxField` | Points to（`Aliases`）、Upstream（`Models`）、Strategy（`Routers`）、Add fallback model（`Routers`）、Team（`Audit`、`Keys`、`Usage`）、User、Owner（`TeamDetail`）、Model、Alias、Router、Upstream model（`Usage`） |

Schema enum 上限 64 個，加上 Not set 是 65 個，不算短清單，所以要有 64 項的測試 fixture；但它的選項和型別都來自 schema，維持 Select。

### Select：這是最大的風險

鎖定的版本是 `radix-ui@1.8.1`、`@radix-ui/react-select@2.4.1`。Radix Select 和原生 `<select>` 行為不同，直接替換會把 c6 修好的問題弄回來：

- **值的編碼。** 這個版本的 `SelectItem` 不會因空字串拋錯，但 `""` 和 `undefined` 會被當成 placeholder，所以合法的 `""`（「全部」、enum 的「(empty text)」）不能直接當 value。
- **轉換契約，集中在一處：**
  - 領域值是唯一的 state。Radix 的 value 在 render 時才推導，是選項的索引 `i:<n>`，不存起來。
  - 「未設定」是獨立狀態，不等於 `""`。
  - 目前的值不在選項裡（還沒載入、選項被刪、enum 已改）時，顯示「目前值不在清單」，不選第一項，也不送出 `i:-1`。儲存時不改這個欄位。
  - onChange 只接受範圍內的索引，從原始 options 還原值，所以 enum 的數字型別（含 `0`）保留。`SchemaForm` 用 typed adapter，刪除 key 的語意不變（`setKey(..., undefined)`）。
- **Label。** `id`、`aria-invalid`、`aria-describedby` 放在 Trigger，`FieldLabel` 的 `htmlFor` 指向它。每個 label 只對應一個 control。
- **不可信的 schema。** enum 的 label 和 value 只當文字，不能產生 HTML 或連結。`e2e_hostile` 的測試要繼續過。
- **Playwright。** Radix 仍會產生隱藏的 `<select>`，但測試不能操作它，也不能用 evaluate 改值。`selectOption` 共 49 處，加上 `locator("option")` 2 處、datalist 選擇器 2 處，全部換成共用的 `pick(scope, label, optionName)`：
  - 從 trigger 的 `aria-controls` 找到彈出層，在 page 層找 option，因為它 portal 在 dialog 外面；
  - label 和選項名稱都用 exact；
  - 同一個 helper 要能操作 Select 和 Combobox，這樣頁面後來把 Select 換成 Combobox 時，spec 不用改。

### Combobox

沿用 Radix：`shadcn add popover command`，再自建 `ComboboxField`。**不用 `shadcn add combobox`**，registry 現在給的是 Base UI 版本，會多一個 `@base-ui/react`，彈出層和 focus 行為也跟其他元件不同。

- 對外介面和 `SelectField` 相同（`value`、`onChange`、`options`）。
- cmdk 會 trim value，所以內部用索引，不用使用者看得到的字串，也不把 `onSelect` 的字串當領域值。搜尋比對 label。
- 自由輸入版要能送出清單外的文字，空白不被 trim 掉，清空回到「全部」。
- Trigger 帶 label；搜尋 input 有自己的 `aria-label`，不共用同一個 label。
- 開在 Dialog 或 Sheet 裡時的 portal、捲動鎖定組合在 s0 用測試決定，不預設一律 `modal`。

### 確認對話框

`AlertDialogAction` 內部是 `Close`，按下就關，不等 async，會把 409 的「Cannot delete」畫面吞掉。所以：

- 用受控的 `open`，確認按鈕是普通的 `ui/button`，不用 `AlertDialogAction`。只有成功才關。
- 409 留在原對話框：標題換成 `Cannot delete`，內容有 `role="alert"` 和參照者清單，只有 `Got it` 一個按鈕，焦點在它上面。`delete-blocked` 的 test id 保留。
- 其他錯誤照舊 toast 後關閉。
- 預設焦點維持在 Cancel。
- 測試 helper 分成 `confirmDialog(page)`（`alertdialog`）和 `dialog(page)`（`dialog`），不用寬鬆的聯集。
- 新增測試：成功、慢回應、409、非 409、Cancel 預設焦點、點背景不關閉。

### Sheet

官方預設是 `w-3/4 sm:max-w-sm`，直接換的話，手機只剩 75% 寬，桌面也會變窄。`Dialog` 保留三種 variant：

- `drawer`：`w-full sm:max-w-md`，`wide-drawer`：`w-full sm:max-w-xl`，都要蓋掉 Sheet 預設的 max-width。
- Header、footer 不捲動，內容區 `min-h-0 flex-1 overflow-y-auto`；表單 submit 的關聯和 `Close` label 不變。
- 測試：手機和桌面的寬度；長表單捲到底仍能按 Cancel 和 Save。

### Sidebar

- **不用 `collapsible="none"`：** 官方元件在這個值會直接回傳固定 div，手機的 Sheet 分支就不見了。桌面不放收合按鈕，也不改 open 狀態；手機保留 Sheet。
- 寬度維持 15rem（現在的 `w-60`），覆寫官方的 16rem、18rem。
- 拿掉 cookie 和快捷鍵（`Cmd+B`），這次不新增行為。
- 手機的 overlay 和 Sheet 要在 sticky header（`z-50`）之上。
- 保留：`Toggle navigation` label、展開狀態、Main nav、目前頁面的標示、點連結後關閉、Skip to content。
- 390 px 實測開關、Esc、背景鎖定、焦點返回。

### Tabs

- 用 `activationMode="manual"`：方向鍵只移動焦點，Enter 或 Space 才切換，避免方向鍵每移一格就換 panel 並發出查詢。
- Inactive panel 維持 unmount，不加 `forceMount`。現在就是條件渲染，切走再回來狀態會重置，這點不變。
- 測試：方向鍵、Home、End、Tab、Shift+Tab；離開再返回的 panel 狀態。

### 表單不換 react-hook-form

`useForm` 已經處理驗證、422 對應到欄位（`ALIAS`）、`Save anyway`。換掉會動到每個表單的邏輯，使用者也看不出好處。只換呈現層的 `Field`。

### 共用元件留著

`DataTable`、`FormDialog`、`TextField`、`SelectField`、`ComboboxField`、`ConfirmDialog`、`PageHeader` 有領域邏輯，或讓頁面少寫東西，留著，內部全部是 shadcn。只有純轉發的薄包裝才刪。

### `components/ui/` 的來源

- 用 `pnpm dlx shadcn@4.23.0 add <name>` 產生再 commit，先用 dry-run 看 diff。registry 是遠端的，固定 CLI 版本不等於固定內容，所以 commit 裡的原始碼才是來源，不再重新產生。
- 不加 `--overwrite`。既有元件有本專案的變體（`Badge` 的 `warn`、`muted`）和主題。
- Registry 用 `import { cn } from "cn"` 的地方改成本地 `@/lib/utils`，不加 `cn` 依賴。
- 自建的 `description-list`、`combobox-field` 放同一個資料夾。
- 新增的依賴只有 `cmdk`。`sidebar` 會產生 `hooks/use-mobile.ts`，和現有的 `hooks.ts` 並存時要確認 import 不衝突。

### 守門

新增 `ui/scripts/check-shadcn.mjs`，接進 `pnpm check`，CI 的 ui job 明確執行 `pnpm check`。用 parser 走 AST，不用 regex，掃 `src/` 的 `.ts` 和 `.tsx`：

- **舊 class：** 45 個 class 名稱（清單寫在腳本裡）。比對完整 token，涵蓋 `className`、`cn`、`clsx`、`cva`、template string、引用的常數；無法靜態判定的組合要明確列入例外。`text-muted-foreground` 和 variant 名 `"muted"` 不能誤判。
- **原生元素：** 不得出現 `<button`、`<select`、`<input`、`<textarea`、`<table`、`<datalist`、`<option`；不得用 `<Input type="checkbox">` 或 `type="radio"` 取代 `Checkbox`、`RadioGroup`。
- **Radix：** `components/ui/` 以外不得 import `radix-ui` 或 `@radix-ui/*`。
- **豁免分兩種：** shadcn 產生的 primitives 豁免原生元素檢查；自建的 adapter 只豁免真正需要的部分，不豁免舊 class。
- **CSS：** `styles.css` 不得有 `@layer components`。
- **腳本自己要有測試：** 正反向 fixture，含上面每一種繞過方式。

不要改成檢查 runtime DOM：Radix 本身需要隱藏的 `select` 和 `input`。

過渡期用檔案 allowlist 列出還沒換的檔案。**只有 lead 在每波合併後縮減 allowlist**，worker 只在報告說明可以移除哪些，避免平行任務改同一個檔案。s7 清空並拿掉 allowlist 機制。

## 核准的行為變更

只有這些：

- 下拉選單不再是作業系統原生的樣式，手機上不再是原生 picker。
- 刪除確認變成 `alertdialog`，點背景不再關閉。
- Team 頁的 tabs 改成一個 tab stop 加方向鍵。

其他行為都不變。若實作時發現還有別的，停下來回報，不要自己決定。

## 不會改

- API、`schema.d.ts`、`hooks.ts`、`router.tsx`、`main.tsx`、`theme.ts`、`lib/*`。任何任務要改這些都要先回報。
- 按鈕文字和 label。測試靠 role 和 label 找元件，這也是使用者看到的東西。
- 頁面結構和欄位。這不是重新設計；`TeamDetail`、`Routers` 也不順便拆檔，免得 diff 看不出改了什麼。
- 主題的三態、`lumewarp.theme`、`html[data-theme]`；Toast 的位置和行為。

## 任務

同一波內的任務不碰同一個檔案。每個任務一個 worktree。測試選擇器和 helper 在 s0 一次換完，之後的任務原則上不改 spec；要加測試時寫在報告裡，由 lead 依序合併。s0 不平行，也是唯一會同時動所有 spec 的任務。

| 波 | 任務 | 內容 | 擁有的檔案 | 依賴 |
|---|---|---|---|---|
| 0 | s0a-primitives | 加元件（select、popover、command、sheet、alert-dialog、tabs、sidebar、field、empty、tooltip、separator）；守門腳本和 fixture（先放 allowlist）；乾淨 build 的 bundle 基準 | `ui/src/components/ui/**`、`ui/src/hooks/use-mobile.ts`、`ui/scripts/**`、`ui/package.json`、`ui/pnpm-lock.yaml`、`ui/src/styles.css`（只加 token） | — |
| 0 | s0b-adapters | 換 `Form.tsx`、`Dialog.tsx`、`Confirm.tsx`、`Table.tsx`、`RowActions.tsx`、`Bits.tsx`；新增 `ComboboxField`；**把 4 個原生 `<select>` 全換掉**，含 `SchemaForm.tsx` 兩處和 `Routers.tsx` 一處，只動這幾行；`.principal`、`.secret-value`、`.state-error`、`.order-row` 換成 `data-testid`；`pick`、`confirmDialog` helper 和所有 `selectOption`；邊界 fixture（64 項 enum、65 項 fallback、數字 enum、未知的既存值）和新測試 | `ui/src/components/*.tsx`（`Layout`、`PermissionPicker`、`Policy` 除外）、`ui/e2e/**`，以及 `Routers.tsx`、`Layout.tsx` 裡選擇器用到的那幾行 | s0a |
| 1 | s1-shell | `Layout` 換 sidebar；`Login`、`Overview` | `Layout.tsx`、`pages/{Login,Overview}.tsx` | s0 |
| 1 | s2-admin | `Audit`、`Gateways`、`Settings`、`Users`、`Config` | 這些頁面 | s0 |
| 1 | s3-catalog | `Aliases`、`Models`、`Upstreams`、`Teams`、`Keys`、`Policy`、`PermissionPicker` | 這些檔案 | s0 |
| 1 | s4-usage | `Usage`：篩選換 Combobox，自由輸入上游模型，Stat 換 card | `pages/Usage.tsx` | s0 |
| 2 | s5-router | `Routers`、`SchemaForm` 的其餘部分：schema 表單、JSON 編輯器、splits、Strategy 和 fallback 換 Combobox | `pages/Routers.tsx`、`components/SchemaForm.tsx` | s3 |
| 2 | s6-team | `TeamDetail`：tabs 和其餘部分 | `pages/TeamDetail.tsx` | s3（用到 `Policy`） |
| 3 | s7-cleanup | 刪 `@layer components` 和沒人用的 `ui/*`；清空 allowlist 並移除機制；CI 的 ui job 跑 Playwright；截圖矩陣；文件；bundle 數字 | `ui/src/styles.css`、`ui/src/components/ui/**`、`ui/scripts/**`、`ui/e2e/**`、`ui/playwright.config.ts`、`.github/workflows/ci.yml`、文件 | 全部 |

類別都是 visual-engineering。每一波後跨家族 review。s0b 和 s5 的 review 要特別看 Select 的編碼和 catalog 的回歸項。

**CI：** ui job 目前只有 `pnpm build`。加 Playwright 要有 Postgres、uv、Python、瀏覽器；`serve.py` 讀的是 `LUMEWARP_TEST_DATABASE_URL`，不是 e2e job 的 `E2E_PG_URL`。

## 驗收條件

- `pnpm check`（含守門腳本）、`pnpm build:cp`、`pnpm test:e2e` 全過，全新資料庫連跑 3 次都過。
- `scripts/e2e-p1.sh`、`scripts/e2e-p2.sh` 全過。不動 gateway，不需要跑 bench。
- 守門腳本的 allowlist 是空的，機制也拿掉了；`styles.css` 沒有 `@layer components`。
- Catalog 的回歸項都還在，測試沒有被放寬：
  - 改名只送 `name`，隱藏的權重和 params 不變。
  - `__proto__`、`constructor`、`toString`、`""`、`false`、`0`、`[]` 經過表單和 JSON 編輯器都保留原樣。
  - Usage 名為 `none`、`__none__`、`__has__` 的 alias 和 router 可以篩選；已退役的上游模型名可以自由輸入。
  - `e2e_hostile` 的 schema 只顯示成文字，沒有 `img`、`onerror`、`href`。
- Select 契約的 payload 測試：數字 enum 含 `0`、`""` 與未設定不同、未知的既存值不被改動、選項為空、選項非同步載入後順序改變。
- 鍵盤：Select 和 Combobox 能用方向鍵、Enter、Esc；在 Dialog 或 Sheet 裡按 Esc 只關最上層；關閉後焦點回到原位，頁面可以再捲動。寫成 Playwright 測試。
- 長清單：64 項 enum 和 65 項 Combobox 在 Dialog、Sheet、390 px 下都能選到第一項和最後一項，能用鍵盤和捲動。
- 截圖：`E2E_SHOTS` 設定下產生，核對檔案數量，一般的 `pnpm test:e2e` 不算。矩陣是 15 頁，加 Team 的每個 tab、Router 表單、Upstream 表單、權限選單、開啟中的 Select 和 Combobox、確認對話框、409 畫面，各有桌面和 390 px、亮暗兩色。Lead 逐張看過。
- Bundle：有前後數字。預算是 JS gzip 不超過基準 +15%（以現有數字算是 210,179 bytes）。超過就停下來回報；`router.tsx` 不動，要做 code splitting 由 Andy 決定。

## 風險

| 風險 | 處理 |
|---|---|
| Select 的值編碼出錯，篩選或表單送錯值 | 編碼集中在一處；測試斷言實際送出的 payload |
| s0b 太大：共用元件、4 個 select、49 處測試一起換 | 分開 commit：adapter → consumer → spec；全部測試過才進下一波 |
| Select 或 Combobox 在 Dialog、Sheet 裡的焦點和捲動鎖定 | s0b 先測，有問題就在進第 1 波前解決 |
| 只讀程式碼看不出的行為（focus restore、touch 捲動、螢幕閱讀器） | 實跑驗收；螢幕閱讀器沒有自動化，只驗 role 和 aria 關聯，報告要寫明 |
| 和 w2-ui-self-service 衝突 | 那個任務排在遷移之後，頁面只寫一次 |

## 待 Andy 決定（未回覆就用建議值）

1. **日期選擇**：保留原生日期輸入（建議，省下 react-day-picker，手機體驗也較好），或換 shadcn Calendar + Popover。
2. **Sidebar 收合**：維持固定寬度（建議，這次不改行為），或加桌面版收合。
3. **CI 跑 Playwright**：加（建議，這次的改動全靠它保護），或維持只有手動。

## 不在這份計畫內

- Overview 的 usage 報表、Usage 頁的趨勢圖、Tokens／Cost 切換。這些是新功能，等遷移做完再用 shadcn Chart 做，不混在只換呈現層的改動裡。按天的資料 API 已有，每小時要加 `hour` 維度。
- 移除 `isSuper`、一般使用者首頁：`w2-ui-self-service`，見 `docs/plans/p25.md`。
- 視覺重新設計、mock 裡沒有 API 資料的欄位。
