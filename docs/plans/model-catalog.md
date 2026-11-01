# 模型目錄計畫：Upstream、Model、Alias、Router

狀態：計畫，尚未實作。基準：`p25/w1` @ 12cdc01。UI 稿：<https://claude.ai/artifact/QABCtDXssnaZFkGbc7eLhU>

## 為什麼要改

現在註冊模型要建 upstream → deployment → route，對外名稱只存在 route 上，deployment 只是內部資源。實際上大多數模型只是「把這個名字對應到上游的那個名字」，卻一定要多建一個 route。

## 概念

| 概念 | 意思 | 例子 |
|---|---|---|
| Upstream | OpenAI-compatible 端點。概念不變 | `vllm-gpu-1` |
| Model | 一個對外名字對應到一個 upstream 上的一個模型名字，取代 deployment | `lumewarp/model` → `vllm-gpu-1` 的 `Qwen/Qwen3-8B` |
| Alias | 另一個名字，導向已存在的 model 或 router | `coder-latest` → `qwen-coder` |
| Router | 也是一個可呼叫的名字，負責把請求分給後面的 models，取代 route | `qwen-coder` → 兩個 model 各 50 |

用戶端和權限看到的都是「名字」。名字可能是 model、alias 或 router，呼叫方式完全相同。

## Andy 已定（2026-11-02）

1. **名字全域唯一。** 一個 model 只對應一個 upstream + upstream_model，反過來不限制：同一個上游模型可以有多個 model。Model、alias、router 共用同一個名字空間，要負載平衡一律建 router。
2. **名字可以改。** 見「改名」。
3. **Alias 可指向 model 或 router，只有一層，獨立授權。** 有 alias 權限就能呼叫，不需要目標的權限；有目標的權限也不會自動得到 alias。
4. **Router 底下只有 model 名字。** 成員只能是 model，不能放 alias 或其他 router。Fallback 目標也只能是 model，而且可以不設。
5. **權重放在 router 的成員上。** 同一個 model 在不同 router 可以有不同權重。Model 上不再有 weight。
6. **Retry、fallback、traffic split、strategy 都只在 router 上。** 直接呼叫 model 就是一次嘗試。Health check 和熔斷的設定仍在 upstream 上，熔斷狀態依 model 分開。需要重試就建單一成員的 router。
7. **權限依呼叫的名字檢查。** Key 能呼叫 `qwen-coder`，不代表能直接呼叫 `lumewarp/qwen3-a`，直接呼叫 model 要有該 model 的權限。Router 的 fallback 不重新檢查權限，和現在一樣。
8. **權限維持 team → member / service account。** Key 繼承持有者的範圍，不加 key 層。
9. **被參照的名字不能刪。** 被 router、alias 或 fallback 參照時回 409，並列出參照者。
10. **回應的 `model` 一律改寫成呼叫時的名字。** Control plane UI 要能同時用對外名字和上游模型名篩選。
11. **內部命名全部一起改。** 目前沒有遷移成本，不保留舊詞。見「命名」。
12. **Team 設成 All 就包含之後新增的名字。** 需要限制的 team 改用 Selected。
13. **Usage 的 `model` 記最後實際服務的 model，另外記錄來源的 alias 和 router。** 見「Usage 記錄」。
14. **Router 的設定由 Rust 擴充介面定義。** 內建和自訂策略走同一套，UI 依策略宣告的 schema 產生表單。策略只定義自己的設定，成員清單、retry、fallback、traffic split 由 gateway 統一提供。見「Router 策略與設定」。
15. **Gateway 不寫 access log，要查單一請求就到 OTel 找。** 見「用 OTel 查請求」。
16. **只有 model 改名有「Keep old name as alias」。** 預設勾選；router 和 alias 沒有。
17. **有 gateway 沒有某個策略時，預設不能儲存。** API 回 422 並列出缺少的 gateway，帶 `force` 才能存。
18. **Control plane 的上游資訊暫時維持現狀（Andy：先這樣處理）。** 任何已登入的人都能讀路由設定，team member 也能在 Usage 看到實際的 model 和上游模型名。回應改寫只影響 API 用戶端看到的名字，不算保密。之後要對成員隱藏再另外設計。

## 其他規則

- **價格放在 model 上。** 費用永遠依實際服務的 model 計算。
- **權限清單驗證名字存在。** Member / service account 的清單必須是 team 範圍的子集，UI 也只列 team 範圍內的名字。
- **Team 範圍縮小時，** 不改 member 的設定，實際權限仍取交集（現有 `narrow` 的行為）。
- **刪除名字時，** 指向它的授權一起刪除並寫入 audit；被 router、alias 或 fallback 參照則依第 9 點回 409。

## Router 策略與設定

W1 已把 router 搬到 Rust（`lumewarp-router-api`，編譯期註冊），但設定還沒有定義：`custom_strategy` 加一包沒有 schema 的 `strategy_params`，control plane 只檢查大小和 `headers`，UI 也不知道該顯示什麼。內建策略是另一條路（enum `RoutingStrategy`），要新增欄位就得同時改 proto、API、UI。

**原則：** 新增策略只需要寫 Rust crate 並重新 build gateway。Proto、control plane、UI 都不用改，表單會自動出現。

### 策略宣告什麼

```rust
#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct Params { /// Skip members with more waiting requests than this.
                #[schemars(title = "Max waiting requests", range(min = 0))]
                max_waiting: Option<u32> }

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct MemberParams { #[schemars(title = "Priority", range(min = 1))] priority: u32 }

lumewarp_router_api::register! {
    name: "priority_failover",
    title: "Priority failover",
    description: "Use the lowest priority number that is healthy.",
    uses_weight: false,
    needs: Needs::default(),
    params: Params,
    member_params: MemberParams,
    strategy: PriorityFailover,
}
```

- **Schema 和程式碼同一份。** 用 `schemars` 從 `configure` 實際解析的 struct 產生 JSON Schema，不會脫節。實作時先驗證它的輸出能收斂到下面的子集，不行就改手寫 schema 加一致性測試。
- **`needs` 改成靜態宣告。** 現在要 `configure` 之後才拿得到，control plane 無法在儲存時檢查，例如 `vllm_load` 需要 metrics URL。
- **`uses_weight`。** 權重留在 gateway（`Candidates::weighted` 依賴預先算好的表），策略只宣告用不用。目前內建策略只有 `weighted` 真的讀權重，其他三種的權重填了也無效，UI 應該隱藏。
- **成員專屬設定。** `member_params` 讓策略定義每個成員的欄位，例如優先序或區域。`configure` 可讀到，`Candidate` 新增穩定的 `member_index()`，`pick` 用它查 `configure` 預先算好的表（`Candidate.index` 是當前 pool 的索引，traffic split 時不穩定）。
- **內建策略走同一套。** `weighted`、`least_in_flight`、`lowest_latency`、`vllm_load` 也用 `register!` 宣告，目前都沒有 params。內部仍用 enum dispatch，避免多一層 vtable。
- **誰定義什麼：** 策略定義 strategy settings 和成員專屬欄位。名字、成員清單、retry、fallback、traffic split、sticky 由 gateway 提供，因為「只在第一個 byte 前重試」和 retry budget 這些保證不應該交給每個策略重做，這也符合 `lumewarp-router-api` 現有的契約。
- `API_VERSION` 升到 2。W1 還沒合併到 main，不需要相容 v1。

### Control plane 怎麼拿到 schema

策略編譯在 gateway 裡，所以 schema 只能由 gateway 提供。

- `Hello` 新增 `repeated StrategyDescriptor`（name、title、description、uses_weight、needs、params_schema_json、member_schema_json、digest）。Hello 在連線時才送，不在請求路徑上。
- Control plane 把描述存到 `gateway_strategies`，gateway 離線時 UI 仍然可以編輯。超過 30 天沒連線或 token 已撤銷的 gateway，描述不再算數，避免已下線的 gateway 讓策略一直顯示可用。
- **內建策略要在沒有 gateway 時也能用：** 新增 `lumewarp-gateway strategies --json`，輸出內建策略的 manifest 並 commit 進 control plane，CI 比對不一致就失敗。
- Standalone：YAML 同 schema，`configure` 在載入時驗證；同一個指令也可以列出自訂策略。

| 情況 | 行為 |
|---|---|
| 策略沒有任何 gateway 回報 | API 回 422，不再等到 gateway NACK 才發現 |
| 部分連線中的 gateway 沒有這個策略 | 預設回 422 並列出缺少的 gateway，帶 `force` 才能存，UI 要勾選「Save anyway」。那個 gateway 收到後會 NACK 整個版本，連撤銷 key 都收不到，所以不能只是警告 |
| 滾動升級中 schema 不同 | Params 必須同時通過所有連線中 gateway 的版本；表單用最新連上的版本 |
| Gateway 升級後，已存的 params 不符新 schema | Routers 顯示 `Config invalid`，不自動修改資料 |

同一個策略名字下，schema 只能做相容的變更（新增選填欄位）；不相容的改動要換新名字。寫進 `docs/router-strategies.md`。

### 驗證與安全

- **三層驗證：** UI 依 schema 驗證，control plane 寫入時用 `jsonschema` 驗證，gateway `configure` 是最終權威（`deny_unknown_fields`）。
- **Schema 子集：** `string`、`integer`、`number`、`boolean`、`enum`、字串陣列、一層巢狀物件，加上 `title`、`description`、`default`、`required`、`minimum`、`maximum`；`Option<T>` 視為選填的 `T`。`register!` 帶一個測試 helper，超出子集就讓策略 crate 的測試失敗。收到超出子集的 schema 時，UI 退回 JSON 編輯器。
- **Schema 視為不可信的資料：** 限制大小（每份 16 KiB），只當文字渲染，不執行也不解讀成 HTML。
- **不放祕密：** Params 會進 snapshot、API 和 audit，所以策略設定不能放 secret。文件要寫明。
- **`headers` 維持平台規則：** Header 白名單由 `x-lumewarp-widget: "header-list"` 標示，control plane 仍然擋 `authorization`、`cookie` 這類 header，不依賴策略的 schema。`headers` 是保留欄位，gateway 讀走後才把其餘 params 交給 `configure`，所以策略的 struct 不用宣告，`deny_unknown_fields` 也不會擋掉。

## 改名

- 所有參照都用 id（router 成員、alias 目標、fallback、權限），改名不會弄斷內部關係。
- **用戶端用舊名字會立刻 404。** Model 的改名表單提供「Keep old name as alias」（預設勾選）：建立指向新名字的 alias，並複製舊名字的授權，既有呼叫方不中斷。Router 和 alias 改名時沒有這個選項，需要的話到 Aliases 頁自己建。
- `catalog_name_history` 記錄每個名字用過的舊名。Usage 篩選和分組依 id，所以改名前後的用量連續；歷史 event 保留當時的名字字串。
- 舊名字可以被其他項目重新使用，所以解析 usage event 時要依種類和 event 時間查（見「Usage 記錄」）。
- Metrics 的 label 會換新名字，舊名字的時間序列就此結束。這是 Prometheus 的正常行為，文件要寫明。
- Audit 記錄 `model.rename`、`router.rename`、`alias.rename` 和新舊名字。

## Rollback

現在的 rollback 是用 snapshot 依名字改寫來源表（`_restore_routing`）。名字可以改之後這行不通：snapshot 沒有 id，改過名的項目會被當成刪除再新增，授權和參照就斷了。

- `config_versions` 新增 `catalog_state`（JSONB），發布時保存當下的目錄來源資料，包含 id。Compact 時和 `payload` 一起清掉。
- Rollback 依 id 還原 model、alias、router 和名字；已被刪除的項目用原本的 id 重建。
- 授權不屬於路由設定，維持現狀不還原：已刪除項目的授權不會回來，文件要寫明。
- 發布的路由內容仍以舊版本 snapshot 為準，key、team、policy 取當下狀態，撤銷的 key 不會復活。

## 回應改寫

現有的 `AliasBody` 不能直接套到每個請求：

- 非串流回應要**緩衝整個 body** 才改寫，大批 embeddings 會增加延遲和記憶體，超過 `MAX_BODY` 時連線會直接中斷。
- `model` 不一定在開頭，例如 OpenAI embeddings 可能排在 `data` 之後，所以只看前綴不夠。

做法：

- **不緩衝的串流掃描器。** 逐 byte 追蹤 JSON 深度和字串狀態，只替換最外層 `model` 的值，其餘 bytes 直接轉發，不解析也不配置記憶體。非串流和每個 SSE event 共用它。
- **快速路徑。** 呼叫的名字和 `upstream_model` 相同時，完全不走掃描器。
- 回應長度會變，改寫時移除 `Content-Length`，改用 chunked。
- **壓縮：** 目前會轉發用戶端的 `Accept-Encoding`，壓縮過的回應既不能改寫，usage 也讀不到（`Meter::Encoded`）。需要改寫時，移除送往上游的 `Accept-Encoding`。上游仍然壓縮就原樣轉發，並累加 metric，不解壓縮。所以「一律改寫」的例外只有這個和錯誤回應。
- 錯誤回應維持不改寫，上游的錯誤內容可能帶有上游模型名，這是現有行為。
- Fallback、retry 共用同一條路徑，取代 `AliasBody`。

**這是每個請求都會走的路徑，必須有 bench 數字。** A6 要跑非串流和串流（TTFT），加上大回應的 embeddings 情境。基準是 1000 RPS 非串流 overhead p99 0.20 ms。如果超出 bench 雜訊範圍，先回報 Andy 再決定，不直接合併。

## Usage 記錄

`model` 永遠是最後實際服務請求的 model，費用也依它計算。怎麼走到這個 model 記在來源欄位：

| 呼叫 | `model` | `alias` | `router` | `fallback` |
|---|---|---|---|---|
| 直接呼叫 model `gpt-4o` | `gpt-4o` | | | false |
| Alias `gpt-4` → model `gpt-4o` | `gpt-4o` | `gpt-4` | | false |
| Router `qwen-coder`，由成員服務 | `lumewarp/qwen3-b` | | `qwen-coder` | false |
| Alias `coder-latest` → router `qwen-coder` | `lumewarp/qwen3-a` | `coder-latest` | `qwen-coder` | false |
| 同上，成員都失敗後走 fallback | `gpt-4o` | `coder-latest` | `qwen-coder` | true |

`requested` 另外記錄用戶端呼叫的名字。雖然成功時可以從上表推出來，但被拒絕或找不到名字的請求沒有 `model`，需要它才查得到。回應的 `model` 改寫成這個名字，所以用戶端看到的名字和 usage 的 `model` 可能不同。

**`UsageEvent`：** `requested`（取代舊的 `model`）、`model`（取代 `deployment`）、`alias`、`router`、`upstream`、`upstream_model`、`fallback`、`attempts`、`endpoint`（取代 `route`）。

- **上游資訊由 gateway 直接送，** 不在 control plane 收到時才查目錄。這樣 model 剛好改了上游也不會記錯。
- **`attempts` 是請求總嘗試次數，** 每次失敗的細節仍然看 OTel span。
- 只在請求結束時多幾個字串和數字，不增加處理前的成本，但仍是請求路徑的改動，由 c3 的 A6 驗證。

**`usage_events`：** 存當時的名字字串和 `model_id`、`alias_id`、`router_id`。Id 由 control plane 收到時解析：`model` 只查 model，`alias` 只查 alias，`router` 只查 router，現行目錄查不到才依 event 時間查 `catalog_name_history`。不分種類會出錯，例如 model 改名並保留舊名字後，舊名字同時是 alias 和該 model 的歷史名字。Usage 依 id 篩選和分組，所以改名前後的用量連續。

**`GET /usage`：** 篩選和 `group_by` 都有 `model`、`upstream_model`、`alias`、`router`。`alias` 和 `router` 可以篩「沒有經過」的流量。

**UI：**

- **Usage：** 預設依 Model 分組，並顯示 Upstream model、Via alias、Via router。依 Alias 或 Router 分組時顯示 Served by。
- **Models、Aliases、Routers：** 搜尋框同時比對對外名字和上游模型名。

## 用 OTel 查請求

Gateway 不寫 access log。要查單一請求（重試了幾次、為什麼換 model、哪個 key 打的）就到 OTel 找，所以 span 要帶得夠完整，目前不夠：

| 現況 | 計畫 |
|---|---|
| `request` span 只有 endpoint、呼叫的名字、實際的 model、status、stream | 新增 `request_id`、`team_id`、`key_id`、`requested`、`alias`、`router`、`upstream`、`upstream_model`、`attempts`、`fallback`、`config_version` |
| `upstream_attempt` span 有 model、attempt、outcome | 新增 `upstream`、`upstream_model`、上游回應的 status |
| 用戶端拿不到任何可以查的 id | 回應加上 `x-lumewarp-request-id`，值就是 usage event 的 `request_id`。不用 `x-request-id`，因為上游（例如 OpenAI）也會回傳這個 header |
| Helm 沒有 OTel 設定 | Values 新增 OTLP endpoint、取樣設定 |
| Bench A6 沒有開 OTel | A6 增加開啟 OTel 的情境，分開記錄數字 |

- **`key_id` 可以進 span，** 規則 5 禁止的是 metric label，prompt 和 response 仍然不進 span。
- **這條查詢路徑有前提，** 文件要寫明：
  - 沒設 `OTEL_EXPORTER_OTLP_ENDPOINT` 就完全沒有紀錄。
  - 取樣低於 100% 或 exporter 佇列滿了，部分請求會查不到。要當 access log 用，建議 100%。
  - 保留多久由 OTel 後端決定。
- **實作時要驗證：** SDK 預設的 sampler 是否會聽從用戶端 `traceparent` 的 sampled flag。如果會，用戶端可以讓自己的請求不留紀錄，那就要改成 gateway 自己決定。
- Usage event 不受這些前提影響，只是沒有逐次嘗試的細節。

## 命名

| 舊 | 新 | 範圍 |
|---|---|---|
| `deployment` | `model` | DB、API、UI、Python、Rust |
| `route` | `router` | DB、API、UI、Python、Rust、`lumewarp-router-api` 的 `RouteInfo` |
| `Deployment`、`ModelRoute` | `Model`、`Router` + `Alias` | proto |
| `Snapshot.deployments`、`routes` | `models`、`routers`、`aliases` | proto |
| YAML `deployments:`、`routes:` | `models:`、`aliases:`、`routers:` | standalone config |
| metric / OTel / usage `deployment` | `model` | metrics、OTel、usage、dashboards、alerts |
| metric / OTel / usage `model`（呼叫的名字） | `requested` | 同上 |
| metric / usage `route`（API 路徑） | `endpoint` | 同上，避免和 router 混淆 |
| `lumewarp_fallbacks{model, fallback_model}` | `{router, model}` | metrics |
| `lumewarp_retries_budget_exhausted{model}`、`lumewarp_split_requests` 的 route 名字 | `router` | metrics |

`model` 在 usage、metrics、OTel 都是實際服務的 model；`lumewarp_requests` 的 label 數量和現在相同。`upstream_model` 維持。Prometheus rules 和 Helm 內的副本要同步更新。

## 資料模型

新的 Alembic migration，不改寫既有 revision，編號依合併順序。目前沒有正式部署，舊資料直接轉換，dev DB 也可以重建。

```text
catalog_names         id, name (unique), kind (model | alias | router)
catalog_name_history  name_id, old_name, renamed_at
models                name_id PK/FK, upstream_id FK, upstream_model, price_input, price_output
aliases               name_id PK/FK, target_name_id FK (kind <> alias)
routers               name_id PK/FK, strategy, params, retry_*, sticky*
router_members        router_id FK, model_id FK, weight, params, position
gateway_strategies    gateway_id FK, name, digest, descriptor, seen_at
router_splits         router_id FK, name, weight, position
router_split_members  split_id FK, model_id FK, weight, params, position
router_fallbacks      router_id FK, model_id FK, position
policy_grants         (team | member | service account), name_id FK
```

- `catalog_names` 保證跨種類的唯一性，並讓 alias 能用真正的外鍵指向 model 或 router。
- 因為名字可改，`allowed_models text[]` 改成 `policy_grants`。`model_access`（all / inherit / list）保留。
- 轉換既有資料：
  - deployment → model；route → router，成員權重取自原本的 `deployment.weight`。
  - 只有一個成員、沒有 retry、fallback、split 的 route，合併成以 route 名字命名的 model。
  - 其餘 route 和 deployment 同名時，migration 失敗並列出衝突。
  - 兩個單一成員的 route 共用同一個 deployment 時，各建一個 model。
  - `usage_events`：舊 `deployment` 轉成 `model`，舊 `model` 轉成 `requested`，是 router 的再填 `router`。
  - 舊版 `config_versions` 和 gateway 的 `snapshot.pb` 格式不再相容，一律清掉，gateway 下次連線時重新取得完整 snapshot。

## API

`/deployments`、`/routes` 移除（不考慮向後相容，見 `docs/plans/p25.md`）。

```text
/upstreams   不變
/models      CRUD；name、upstream_id、upstream_model、價格
/aliases     CRUD；name、target（model 或 router 的 id）
/routers     CRUD；strategy、params、members: [{model_id, weight, params}]，fallback_model_ids，其餘同舊 route
/strategies  唯讀；每個策略的描述、schema，以及哪些 gateway 支援
/catalog     唯讀，回傳所有名字與 kind，供權限選單使用
```

- `/models`、`/aliases`、`/routers` 的 PATCH 都可以改 `name`；只有 `/models` 可帶 `keep_old_name_as_alias`。
- 寫入仍是 `ROUTING_MANAGE`，讀取仍是 `ROUTING_READ`（任何已登入的人）。`/catalog` 同樣回傳所有名字，表單自己依 team 範圍過濾；只過濾 `/catalog` 沒有意義，因為 `/models`、`/routers` 都讀得到。

## Snapshot 與 gateway

Snapshot 直接帶三種資料，由 gateway 編譯成同一張查表：

```text
Snapshot { upstreams, models, aliases, routers, api_keys, teams, ... }
Model    { name, upstream, upstream_model, price }
Alias    { name, target }
Router   { name, members: [{model, weight, params_json}], strategy, params_json, retry, fallbacks: [model], traffic_split: [{name, weight, members: [...]}], sticky, ... }
```

- Gateway 在套用 snapshot 時建表：`Model` 成為單次嘗試的 pool，`Router` 成為有策略的 pool，`Alias` 指向目標的同一個 `Arc`。查表結果另外帶呼叫的名字和來源（alias、router），不從 route 取名字，否則透過 alias 呼叫時，回應改寫和 usage 都會記成目標的名字。請求路徑仍然是一次名字查表。
- `Router.fallbacks` 只有 model，不再有巢狀。
- 有 `traffic_split` 時，成員寫在各 split 底下，`members` 留空（和現在一樣）。`member_index()` 橫跨所有 split，同一個 model 在不同 split 可以有不同的權重和成員設定。
- 權重改由成員提供，要確認只在 snapshot apply 時讀取，不增加 `pick` 的成本。
- `Router.strategy` 改成字串名字，移除 enum `RoutingStrategy` 和 `custom_strategy`。Capability 維持 `strategy:<name>`，內建策略同樣要宣告。
- 權限檢查邏輯不變，仍然比對呼叫的名字。`ApiKey` 的 `allowed_models` 由 control plane 展開成當下的名字；改名會讓相關 key 進入下一次 delta。
- `GET /v1/models` 回傳 key 可用的所有名字，不分種類。
- Standalone YAML 與 snapshot 同一份 schema（規則 4）。
- Capability：`fallbacks`、`retry`、`traffic_split` 等依新欄位重新定義。`docs/config-protocol.md` 的回應 `model` 契約要改寫。

## UI

依 UI 稿，和 `w2-ui-pages` 同一個任務做，頁面只寫一次：

- **Sidebar：** Routing 群組改成 Upstreams、Models、Aliases、Routers。
- **Models：** 名字、Maps to、被誰使用；搜尋同時比對兩邊的名字。
- **Aliases：** 獨立一頁，顯示名字、指向的 model 或 router、實際的上游模型；搜尋也比對上游模型名。
- **Routers：** 成員、策略、健康狀態、fallback model、對應的 alias；下方列出已安裝的策略和支援的 gateway 數。
- **Router 表單：** 選策略後，strategy settings 和成員欄位依 schema 產生。策略沒有使用權重就不顯示權重。有 gateway 不支援就顯示警告，要勾選「Save anyway」才能存。開啟 traffic split 時，成員改為寫在各 split 底下。Fallback model 可以不設。
- **改名：** 三種編輯表單都可改 name，只有 model 有「Keep old name as alias」。
- **權限選單：** 取代現在用逗號分隔的文字欄位。Team 選 All 或 Selected；member / service account 選 Inherit 或 Selected，依 Models、Aliases、Routers 分組打勾，選項限於 team 範圍。
- **My keys：** 建立 key 時只顯示這把 key 能呼叫的名字，不能選。
- **Usage：** 見「Usage 記錄」。

## 任務與順序

併入 P2.5。前提是 `p25/w1` 的 A6 重跑有結論並合回 main。

| 順序 | 任務 | 為什麼 |
|---|---|---|
| 1 | `c0-rename` | 全 repo 改名，一定要在其他 W2 分支開始前合併，否則全部衝突 |
| 2 | `w2-secrets`、`w2-self-service-api` | 和目錄無關，可以平行，但 migration 依序合併 |
| 3 | `c1` → `c2` → `c3` | 依序，後者依賴前者的 schema 和 proto |
| 4 | `c4`、`c5`、`c8` | 可以平行，檔案不重疊；`c5`、`c8` 的 migration 依序合併 |
| 5 | `w2-ui-pages` + `c6` | 同一個任務，舊頁面不必先換 shadcn 再重寫。它會改 OpenAPI 型別，所以排在 API 完成之後 |
| 6 | `w2-router-skill` | 排在 c8 之後，產生的策略 crate 和 `cheapest-first` 範例都要帶 schema |
| 7 | `w3-e2e` + `w3-docs` + `c7` | 同一波，避免重複改同樣的文件和 e2e |

**c0 不能一次全域取代，** 因為 `model` 和 `deployment` 互相衝突：要先把舊的 `model` 改成 `requested` 或 `name`（`route.model` → `router.name`），才能把 `deployment` 改成 `model`。這兩步都只是改名，行為不變。Proto 只改名字，不改欄位編號。全 repo（不含 docs）有 96 個檔案含 `deployment`。

| 任務 | 內容 | 擁有的檔案 | 依賴 |
|---|---|---|---|
| c0-rename | 只做改名（見「命名」），含 migration，行為不變，測試全過 | 全 repo，proto、dashboards、alerts、docs | W1 |
| c1-schema | migration、`/models`、`/aliases`、`/routers`、`/catalog`、改名、`policy_grants`、驗證與 409、rollback 的 `catalog_state` | `models.py`、`migrations/`、`api/routing.py`、`api/policy.py`、`api/members.py`、`api/config.py`、`api/schemas.py`、相關測試 | c0 |
| c2-snapshot | snapshot 三種資料、compile、validate、delta、rollback | `proto/`、`compiler/*`、`_proto` | c1 |
| c3-gateway | 建表與 alias、查表帶來源、`UsageEvent` 新欄位、span 欄位、`x-lumewarp-request-id`、成員權重、`Router.strategy` 字串化、standalone YAML、bench A6（含開 OTel） | `gateway/**`、`bench/lumewarp` | c2 |
| c4-rewrite | 串流掃描器、快速路徑、壓縮處理、取代 `AliasBody`、bench A6（含串流與大回應） | `proxy.rs` 與其測試、`docs/config-protocol.md` | c3 |
| c5-usage | `usage_events` 新欄位、ingest 解析、`GET /usage` 篩選與分組 | `usage/`、`api/usage.py`、migration | c1、c3 |
| c6-ui | Models、Aliases、Routers、schema 表單、改名、權限選單、My keys、Usage，併入 `w2-ui-pages` | `ui/src/**`、`ui/e2e/**` | c1、c5、c8 的 OpenAPI |
| c7-docs-e2e | e2e-p1/p2、load-e2e、dev seed、Helm（含 OTel values）、架構文件、`CLAUDE.md`、`control-plane/README.md` 移除 `lumewarp.router`，併入 W3 | `tests/**`、`scripts/dev.sh`、`docs/**`、`deploy/**`、各 README | c2–c6、c8 |
| c8-strategy-schema | `register!` 描述與 schema、`needs`、`member_index`、內建 manifest、`Hello` 描述、`gateway_strategies`（migration）、`/strategies`、依 schema 驗證、bench A6 | `lumewarp-router-api`、`router.rs`、`config_service.proto`、`grpc/`、`compiler/strategy.py`、`migrations/` | c2、c3 |

## 驗收條件

- `control-plane` pytest 與 ruff 全過，包含：
  - 跨種類同名回 422。
  - Router 沒有 fallback 也能建立。
  - Alias 指向 alias 回 422；router 成員或 fallback 不是 model 回 422。
  - 刪除被參照的名字回 409。
  - 改名後 router、alias、權限都還在；model 保留舊名時 alias 帶著原本的授權。
  - Member 權限超出 team 範圍回 422。
- `gateway` fmt、clippy、test 全過，新增：
  - 同一個 model 在兩個 router 有不同權重時，分流比例各自正確。
  - 掃描器：`model` 在開頭、結尾、巢狀、跨 chunk、跳脫字元、非法 JSON，以及大於 `MAX_BODY` 的回應都能正確處理且不中斷。
  - 用戶端送 `Accept-Encoding: gzip` 時回應仍然改寫，上游仍然壓縮時原樣轉發並累加 metric。
- E2E：
  - 建立 model 後可用名字呼叫，`/v1/models` 看得到，回應的 `model` 是呼叫的名字（含串流）。
  - 呼叫 alias、router、fallback 時，回應都是呼叫的名字。
  - 改 alias 目標後，新請求走新目標。
  - Model、alias、router 改名後舊名字 404；model 勾選保留時舊名字繼續可用。
  - 只授權 router 的 key 呼叫成員 model 回 403；只授權 alias 的 key 呼叫目標回 403。
  - Rollback 後三種名字與權重都還原；回到改名前的版本時，授權和參照都還在。
- 策略：
  - 新增一個測試策略 crate，只重新 build gateway，不改 proto、control plane、UI，就能在 UI 看到表單、建立 router、呼叫成功。
  - Params 或成員欄位不符 schema 回 422；沒有 gateway 回報的策略回 422。
  - 連線中的 gateway 缺策略時回 422，帶 `force` 才能存，並寫入 audit。
  - 沒有 gateway 連線時，內建策略仍可建立，manifest 與 gateway 不一致時 CI 失敗。
  - 部分 gateway 缺策略時 UI 顯示警告並要求確認；兩個 gateway 的 schema 不同時，只接受兩者都通過的 params。
- Usage：
  - 上表每一種呼叫都記錄到正確的 `model`、`alias`、`router`、`fallback`、`attempts`，費用依實際服務的 model 計算。
  - Model 的 upstream 改了之後，舊 event 仍是當時的 `upstream_model`。
  - 用 model、上游模型、alias、router 都篩得到；改名前後的用量連續。
  - Model 改名並保留舊名字後，改名前送出的 event 仍然算在該 model，不算到 alias。
  - 被拒絕或名字不存在的請求，`requested` 仍有值。
- OTel：
  - 用回應的 `x-lumewarp-request-id` 能在 trace 裡找到請求，看得到呼叫的名字、alias、router、最後的 model 和每次嘗試。
  - Span 裡沒有 prompt、response、完整的 API key。
  - 用戶端送 `traceparent` 的 sampled flag 為 0 時，行為符合文件。
- UI：`pnpm check`、`pnpm build:cp`、Playwright 全過。
- Prometheus：promtool 測試和 dashboard 檢查全過，舊 label 不再出現。
- Bench A6（`BENCH_AUTH=1`）：c3、c4、c8 各跑一次，c3 另外跑開啟 OTel 的情境，結果寫進 PR 或 `bench/results`。

## 不在這份計畫內

先前討論過、尚未排入的項目：

- 註冊時的連線測試（確認 upstream 連得上、`upstream_model` 存在）。
- 在 Models、Routers 顯示「已生效／被 NACK」。
- `api_key_ref` 錯誤造成 gateway 收不到新設定的問題。這項尚未實測，應該獨立設計。
- Token 月配額、Tokens / Cost 切換的實作。
