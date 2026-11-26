# Lumewarp 架構設計

> 來源：[Lumewarp 架構設計提案](https://claude.ai/artifact/HmNpAUHpzFNzQLCURx3nHo)（2026-10-21，Andy 已核可）。本檔是給 repo 與 Claude Code 讀的版本。

## 摘要

Lumewarp 是比 LiteLLM 更快、更好管理的 LLM gateway，拆成兩個獨立部署的元件：

- **Data plane（gateway）**：Rust。只處理請求，熱路徑不碰資料庫、不跑 Python。
- **Control plane（管理端）**：Python（FastAPI）+ React Admin UI。管理 Org／Team／Member／Service Account、SSO、路由、plugin，產生設定快照。

兩者之間只透過一條版本化的 gRPC 設定串流溝通。control plane 掛掉時 gateway 照常服務。

核心原則：

1. 熱路徑不碰 DB、不跑 Python；計數與用量非同步送出。
2. 客製化分成兩層：control plane 的認證與授權用 Python plugin（`lumewarp.auth`、`lumewarp.authz`），產生的宣告式設定編譯進快照；gateway 端的 plugin（router 策略、上游認證、A2A middleware）是 WASM 元件，開機時從目錄載入，內建策略與內建認證仍是 native。
3. 只做 OpenAI 相容 API，上游只有 vLLM 與 OpenAI（及 OpenAI 相容端點），不做其他 provider 轉換；MCP 與 A2A 只做代理轉發。
4. 設定是不可變的版本快照，gateway 以 ACK／NACK 確認套用；installation-wide rollback 還原 catalog state 並重編每個 zone。
5. LLM、MCP、A2A 三種流量共用同一套 API key 認證與用量、metrics、trace；預算只套用 LLM，MCP `tools/call` 使用 key 的 RPM，A2A 不限流。

## 架構

```mermaid
flowchart TB
  IdP["Keycloak / OIDC IdP"] -->|OIDC 登入| API
  subgraph CP["Control plane (Python)"]
    API["管理 API + plugin<br/>Org／Team／Member／Service Account、授權、路由設定"] --> PG[("Postgres<br/>唯一事實來源、稽核")]
    PG --> COMP["Config compiler<br/>展開繼承 → 版本化快照"]
    UI["React Admin UI (static SPA)"] --> API
  end
  COMP -->|"gRPC 設定串流：全量 + delta、ACK/NACK"| SNAP
  subgraph DP["Data plane (Rust) · 可水平擴展"]
    SNAP["ArcSwap 設定快照（本地磁碟保留最後一版）"]
    AUTH["認證<br/>API key（MCP／A2A 用 X-Lumewarp-Key）"] --> POL["策略引擎<br/>預算、限流、權限"] --> RT["Router<br/>vLLM 負載感知、fallback"] --> PX["上游轉接<br/>零轉換轉發"]
    PLUG["WASM plugins<br/>策略／出站認證／middleware"] --> RT
    PLUG --> MCP
    POL --> MCP["MCP / A2A 代理"]
  end
  Clients["Clients: App / IDE / agent"] -->|OpenAI 相容 API| AUTH
  PX --> UP["vLLM / OpenAI 端點"]
  MCP --> UP2["MCP server / 外部 agent"]
  DP -->|用量事件（非同步）| STORE[("Postgres（MVP）→ ClickHouse（之後）")]
```

## 語言與技術選型

| 元件 | 技術 | 理由 |
| --- | --- | --- |
| gateway | Rust：tokio、hyper、rustls、ArcSwap | 無 GC、p99 穩定、大量串流連線記憶體低 |
| gateway plugin | WASM（wasmtime、Component Model、WIT，Rust SDK） | 換 plugin 不需重 build image，同一個沙箱介面涵蓋三種擴充點 |
| control plane | Python：FastAPI、SQLAlchemy + Alembic、Authlib、grpcio | plugin 門檻最低，OIDC 生態成熟 |
| Admin UI | React + Vite 靜態 SPA、TanStack Query / Router、Tailwind v4、shadcn/ui（Radix primitives） | 編譯成靜態檔由 FastAPI 提供，部署不需 Node.js；型別由 OpenAPI 產生。可換 Preact 更輕 |
| 合約 | protobuf（設定快照 schema） | 兩種語言各自產生型別，schema 是唯一定義 |
| 授權 | Apache-2.0 | |

Admin UI 的呈現層全部是 shadcn/ui 元件（底層 Radix），版面用 Tailwind utility，`ui/src/styles.css` 只有主題 token 與 `@layer base`。`ui/scripts/check-shadcn.mjs` 是守門腳本：用 AST 掃 `ui/src` 的 `.ts`／`.tsx`，禁止舊的 page class、`components/ui/` 之外的原生 `<button>`／`<select>`／`<input>`／`<textarea>`／`<table>`／`<datalist>`／`<option>` 與 `radix-ui` import，並要求 `styles.css` 沒有 `@layer components`；沒有 allowlist，任何發現都讓檢查失敗。在 `ui/` 跑 `pnpm check` 會執行型別檢查、守門腳本自己的 fixture 測試與守門本身，CI 的 ui job 另外跑 Playwright。頁面按路由拆成 lazy chunk，只有登入後的首頁會先載入。Bundle（`pnpm build`，`gzip -9`）：entry chunk 122,340 bytes，安裝管理員進站實際下載的 JS 229,814 bytes（首頁是 Usage；`ui/e2e/s11-lazy-routes.spec.ts` 守 232,100 bytes 上限），所有 JS chunk 分別壓縮後合計 478,330 bytes，CSS 17,177 bytes。Usage 的每日圖（`ui/src/components/UsageChart.tsx`）依模型堆疊，可切換 Tokens／Cost（每個瀏覽器各自記住，預設 Tokens），每個模型在頁面、範圍與指標切換間保持同色，超過八個模型折入 Other，資料表保留全部數字。

## 設定下發

1. 管理者在 UI/API 改設定，寫入 Postgres 並留稽核紀錄。
2. Config compiler 依 zone 編譯 org、team 與 key owner 的有效策略和該 zone 的部署，產生已解析的 protobuf 快照與遞增版本，發布前驗證。
3. gateway 使用 zone 的固定環境 token 訂閱 gRPC 串流，送出 zone metadata，先收到 Welcome，再收全量與 delta（例如新增／撤銷 API key）。
4. gateway 背景建好新索引，ArcSwap 原子替換；成功回 ACK，失敗回 NACK 並保留舊版。
5. 最後一份成功快照寫到本地磁碟，control plane 掛掉仍可重啟服務。
6. 回滾是 installation-wide：還原該版本的 catalog state，再依每個 zone 的資源重新編譯；不是只重播一個 zone 的舊快照。

反方向：用量、token、花費、稽核由 gateway 非同步批次送出（OTLP / NATS），MVP 寫入 Postgres，之後換 ClickHouse。限流計數在 gateway 本地累加，設定 Redis 時背景每 100 ms 與 Redis 對帳（見「跨 gateway 共享限流與多個 gateway」）；預算由 control plane 從用量推導。

另有 standalone 模式：gateway 直接讀同格式 YAML，不需 control plane（GitOps、本機開發）。

### Zone（設定區域）

Zone 是設定與資料流量的邊界，與 org／team／user／role／grant 階層正交。Org、team、成員、角色、授權，以及 model／alias／router 的定義與名稱都是全域共用；每個 zone 各自擁有 upstream、model deployment、MCP server、A2A agent、gateway process 與固定 token、API key、rate limit、budget、usage 和 config version。相同 team 可在不同 zone 有不同的限額與預算，無須複製身分或授權資料。

一個 control plane compiler task 逐一處理所有 live zones。全域階層寫入在固定的 hierarchy advisory lock 下，為每個 live zone 建立自己的 outbox row，將 source revision 一起推進；zone-bound 寫入只更新該 zone。compiler 對每個 zone 使用獨立的發布 advisory lock、transaction、outbox 與版本序列，因此某個 zone 的編譯錯誤不會阻塞其他 zone。

控制平面從 `LUMEWARP_GATEWAY_ZONE_<ID>_TOKEN` 讀取各 zone 的固定 token；`<ID>` 是 zone ID 大寫並將 `-` 換成 `_`，token 至少 32 個字元。每個環境 token 會在啟動時建立對應 zone；zone 不再由 UI／API 建立。控制平面只在記憶體保留 token 的 SHA-256 hash。Gateway 在 config 與 usage 兩個 RPC 都送出 `authorization: Bearer <token>` 和 `x-lumewarp-zone`；`Hello.zone` 一律填入 `--zone`，必須與 metadata 相同。未知 zone 與錯誤 token 都回相同訊息的 `PERMISSION_DENIED`；已封存 zone 回 `FAILED_PRECONDITION`，啟動時不會自動解除封存。缺少 token 的 zone 仍保留資料並顯示未設定。輪替時兩端都更新同一 secret 並重啟控制平面與 gateway，沒有寬限期；gateway 重新連線時持續服務最後一份快照。

Session、health、plugin 支援與 quarantine 都按 `(zone, gateway_id)` 隔離。控制平面在每條串流的第一則 `Welcome` 指派 `<hostname>-<8 hex>` gateway id；每條串流（包括 reconnect）都是新 id 與新列。Usage report 使用最近收到的 Welcome id；第一次 Welcome 前的 report 不帶 gateway id，且不歸屬任何 gateway。任意數量 gateway process 可共用同 zone token。

Catalog 名稱仍是全域唯一。`Model` 是全域定義，`ModelDeployment(name_id, zone)` 才把 model 綁到同 zone 的 upstream、upstream model 與 probe 設定；MCP server 與 A2A agent 也各自屬於一個 zone。compiler 只讀該 zone 的 targets/deployments，沒有 deployment 的 model 不輸出；router member、traffic split member 或 fallback 若無部署會被略過並發出「not available in this zone」警告，空 router 和目標不可用的 alias 也會略過。每個 zone 都由共享的全域 model／router／alias 定義編譯出不同 snapshot。

API key 建立時綁定一個 zone，rotate 不會更換 zone；compiler 只把它放入該 zone 的 snapshot。`zone_limits` 和 `zone_member_defaults` 保存 org／team／member／service account 各層的 per-zone 限流、預算與 team member defaults。Usage event 由已驗證 key 的 zone 標記，`usage_totals`、org budget window、Usage、Leaderboard、Requests 與 Playground 都按 zone 分開；API 未指定 zone 時可查全部 zone。`LUMEWARP_ZONE` 指定啟動時建立的 default zone；installation admins 透過 Admin UI 編輯與封存 zones。Archive 會停止編譯並拒絕該 zone 的 gateway handshake，但在 default zone、有連線 gateway 或仍有未撤銷 key 時拒絕。環境中仍有該 zone token 時也不會解除封存。

## 功能

### SSO（Keycloak / OIDC）

- control plane 是標準 OIDC RP；Keycloak 是預設但非必要。SAML 交給 Keycloak brokering。
- 管理端登入：Authorization Code + PKCE 由 control plane 後端完成（Authlib），之後發伺服器端 session（httpOnly、`SameSite=Lax` cookie，寫入需 `X-Lumewarp-CSRF`）；以 `iss`+`sub` 認人；任何通過 IdP 驗證的使用者都能登入，首次登入 JIT 建立零權限使用者（只接受已驗證 email（`LUMEWARP_OIDC_REQUIRE_EMAIL_VERIFIED=false` 可關閉此檢查，預設 true，初始管理員 bootstrap 同樣適用）；`LUMEWARP_OIDC_JIT_USERS=false` 時只能登入已存在的帳號，唯一例外是還沒有 `proxy_admin` 時符合 `LUMEWARP_INITIAL_ADMIN_EMAIL` 的首次登入）。程式呼叫管理 API 用 PAT（bearer 只接受 PAT）。登入過程的 nonce 與 PKCE code verifier 不進 DB，放在用 `LUMEWARP_SECRET_KEY` 做 AES-256-GCM 封存的登入 cookie；沒設金鑰時服務拒絕啟動。
- IdP 只負責認證，不授予任何 Lumewarp 角色：groups／roles claim 一律忽略。安裝層角色存在 Postgres 的 `installation_admins`（`proxy_admin` 唯一，`admin` 可多人），每次請求讀取，撤銷在下一個請求生效；org 與 team 的 admin／member 關係只來自授權後端（安裝層角色永遠讀 Postgres）。IdP 與 IAM 的人員狀態同步由部署方負責。
- 緊急登入（break-glass）：`POST /api/v1/session/break-glass` 用密碼換 session cookie，IdP 或 IAM 故障時仍可管理。生產用 argon2id hash（`LUMEWARP_BREAK_GLASS_PASSWORD_HASH`），明文密碼要 `LUMEWARP_DEV_MODE=true`。這個 session 不是使用者，不問授權後端；有 TTL（預設 1 小時，上限 4 小時）、失敗延遲與次數限制、可限制來源網段，嘗試都寫稽核，換密碼立刻失效。
- 用戶端呼叫 gateway 時只使用虛擬 API key（只存 hash）；gateway 連接 control plane 則使用每 zone 固定環境 token。JWT／mTLS 用戶端認證延後。
- 停權時推撤銷 delta，所有 gateway 秒級內拒絕：內建後端在移除成員的同一交易停用 key；外部 IAM 由 key reconciler 讀變更串流，約 5 秒內停用；IAM 錯誤或逾時絕不停用 key。

### 憑證儲存與稽核

- 憑證只以不可逆或不可用的形式保存。API key 與 PAT 在 DB 只存 sha256；gateway zone token 由控制平面讀取環境變數，僅在記憶體保留其 SHA-256 hash。Session／CSRF token 與 OIDC state 也只存 sha256。OIDC 的 nonce 與 PKCE code verifier 只放在 AES-256-GCM 封存的登入 cookie（金鑰由 `LUMEWARP_SECRET_KEY` 派生），`oidc_login_states` 只有 state hash、return path、redirect URI 與到期時間，用過即刪；SSO 開啟但沒設金鑰時服務拒絕啟動。Helm 以 Secret 傳入 OIDC 金鑰，範例見 [Helm 部署](./operations/helm.md#sso-and-authorization-backend)。
- Upstream API key 有兩種來源，擇一（或用下一項的 WASM auth plugin）：`api_key_ref`（gateway 環境變數名稱）或在 API 輸入的 key。輸入的 key 以 AES-256-GCM 封存在 `upstreams.api_key_ciphertext`（金鑰由 `LUMEWARP_ENCRYPTION_KEY` 以 HKDF 派生，與 `LUMEWARP_SECRET_KEY` 分開），API 只回 `api_key_set`，稽核只記 `credential_changed`。snapshot 帶密文（capability `encrypted_api_key`），gateway 用同一把 `LUMEWARP_ENCRYPTION_KEY` 在套用 snapshot 時解開，不在請求路徑上；落地的 snapshot 也只有密文。gateway 沒有或金鑰不符時 NACK `INVALID_CONFIG`。換這把金鑰後，已存的 key 必須重新輸入。Helm 以 `encryptionKey.secretName` 同時傳給 control plane 與 gateway。
- LLM upstream 也可改用 WASM `upstream-auth` plugin（`Upstream.auth`；只接受 WASM，內建 `none`／`static_bearer`／`passthrough` 只給 MCP／A2A，且與 API key 擇一，capability `auth:<plugin>`）；params 只放設定、不放 secret，secret 從 `LUMEWARP_PLUGIN_SECRET_*` 讀。細節見下面「Gateway plugin」。
- `upstreams.metrics_url` 不帶 query string（`?token=...` 會把憑證放進 API 會回傳、也會進稽核的欄位）；API 與 compiler 都拒絕帶 query 或 userinfo 的值。
- 稽核 `details` 是每個 action 的 allowlist：沒分類的欄位不記錄；`last_error` 類欄位只存 exception class。守門測試掃每個 Text／String／JSONB 欄位與原始碼中每個 `audit.record(action=...)`，新增而未分類就失敗。
- Request log 與 audit 分開：`GET /requests`（`listRequests`）從 `usage_events` 每列一個 gateway 請求，新的在前，keyset cursor，預設 `kind=llm`、最近 24 小時；篩選 team／key／owner／requested／model／alias／router／status／outcome／request_id 與時間，權限同 `/usage`。欄位是名字解析鏈 `requested → alias → router →（fallback）→ model → upstream`／`upstream_model`、attempts、status、outcome、stream、tokens、audio、cost、usage_state、duration、key／team／owner；不存 prompt、response 或 header，細節用 `x-lumewarp-request-id` 查 OTel。Playground 呼叫不寫 audit；control plane 從 gateway 回應的 `x-lumewarp-request-id` 寫 `playground_requests(request_id, user_id, key_id, ts)`，`/requests` left join 帶出 `playground_user_id`（header 缺失就不記）。只有 control plane 模式有 request log。
- Upstream auth 的稽核：`upstream.create` 記 `auth_plugin`，`upstream.update.changes` 記 `auth`（plugin、params、timeout_ms）；缺 plugin 的強制儲存另記 `upstream.force_save`（name、plugin、missing_gateways）。
- e2e suite 會掃描資料庫，確認沒有明文憑證。

### Org / Team / Member / Service Account

五個角色，上層繼承下層的管理權：

| 角色 | 來源 | 能做什麼 |
| --- | --- | --- |
| `proxy_admin` | `installation_admins`，全庫唯一一列 | 安裝層全部：上游、路由、gateway、config、使用者、org 與安裝層角色。只有本人或緊急 session 能轉移這個角色、停用／刪除帳號、改 email 或代發 PAT |
| `admin` | `installation_admins`，可多人 | 與 `proxy_admin` 相同，除了不能降級／拔除 `proxy_admin` 或動其帳號；任何 `admin` 都能指派與移除其他 `admin` |
| org admin | `org_admins` 一列，即 `admin` on `org:<id>` | 旗下 team 的建立與封存、team 的模型 grant 與各 zone 限流／預算、指派 team admin、成員、service account 與任何 key、org 用量與旗下 audit；org grant 政策本身唯讀 |
| team admin | `team_members.role=admin`，即 `admin` on `team:<id>` | team 名稱、member key 上限、每個 zone 的 member 預設與例外限額、service account、team 內任何 key、成員、team 用量與 audit；不能改 team 的模型 grant 或 org 層限額 |
| member | `team_members.role=member`，即 `member` on `team:<id>` | 讀 team 基本資料與模型目錄名字、在所選 zone 的有效限額內建立與管理自己的 key、看自己的用量 |

安裝層管理員（`proxy_admin` 或 `admin`）在 team 之上；org admin 繼承旗下 team 的 team admin 能力，team admin 是該 team 的直接成員。繼承由 control plane 套用（`RequestAuthz` 決定權限，`keys.owners` 決定 key 擁有權），授權後端只回答直接關係，`member` 永遠只代表直接成員。

繼承者也會出現在下層名單並能持有 key，不寫入任何 `team_members` 或 `org_admins` 資料列：

- 名單：`GET /orgs/{id}/admins` 列出本地 `org_admins` 與所有安裝層管理員；`GET /teams/{id}/members` 列出直接成員、該 org 的本地 org admin 與所有安裝層管理員。每列帶 `inherited_from`（`installation`／`org`／null）與 `direct`；只有繼承身分的列 `role` 是 `admin`、唯讀，不能編輯例外政策或移除，要在授予它的那一層移除。外部授權後端無法列舉某資源的所有 admin，所以只列得出安裝層管理員與本地 `org_admins` 有的人。
- 持有 key：直接成員、安裝層管理員或 team 所屬 org 的 org admin 都能在該 team 為所選 zone 建立、恢復自己的 key；採用該 zone 的 team member defaults、zone key rules，以及 team 的 `max_keys_per_member`／`max_member_key_lifetime_seconds`。compiler 以相同規則判斷 key owner 資格（builtin 後端）。緊急 session 沒有使用者，不擁有 key。
- 失去資格：移除直接成員、org admin 或安裝層管理員時，在同一筆交易內停權該使用者在「已無任何來源允許」的 team 內的 key（`owner_removed`），仍有其他來源的 team 不受影響；轉移 `proxy_admin` 後原持有者變成 `admin`，key 不受影響。外部後端的變動由 key reconciler 發現；判斷回 `ERROR` 時一律不停權。重新取得資格後，本人可以恢復 `owner_removed` 的 key。

- 階層：org → team → member（admin／member）與 service account → API key。Org/team/owner grants 是全域；每個 zone 各自解析限流、並行與預算。只有安裝層管理員能建立 org（`POST /orgs`）、改名或改政策（`PATCH /orgs/{id}`）、封存（`DELETE /orgs/{id}`）與指派／移除 org admin（`PUT|DELETE /orgs/{id}/admins/{user_id}`）；org admin 可以讀自己的 org（`GET /orgs`、`GET /orgs/{id}`）與旗下 team 的用量彙總（`GET /orgs/{id}/usage`，查詢時由 `usage_events` 依 team 分組）。org admin 的指派需要 builtin 後端（外部後端回 409），且對象必須先登入過一次。`POST /teams` 帶明確的 `org_id`，沒有預設 org；org 封存不可逆，旗下沒有未封存 team 才能封存，封存後不能再建 team。org admin 是 `org_admins` 一列，代表 `admin` on `org:<id>`；team 管理權由 `RequestAuthz` 從 `team.org_id` 繼承，後端只回答直接關係，`member` 永遠只代表直接成員；org admin 不需直接成員身分就能在旗下 team 持有 key，`/users/me` 的 team 清單包含繼承的 team（`inherited_from`、`direct`），另外回傳 `admin_orgs`。安裝層管理員是 Postgres 的 `installation_admins` 一列（`proxy_admin` 或 `admin`），緊急 session 也算安裝層管理員。第一個 `proxy_admin` 由「還沒有 proxy_admin 時，第一個通過驗證（`LUMEWARP_OIDC_REQUIRE_EMAIL_VERIFIED=false` 時不檢查 `email_verified`）且 email 等於 `LUMEWARP_INITIAL_ADMIN_EMAIL` 的登入」建立（`LUMEWARP_OIDC_JIT_USERS=false` 時只有這個 email 可以即時建立）；變數之後失效，改值不會換人。緊急 session 可以建立或轉移 `proxy_admin`，`proxy_admin` 也可以轉移給別人，轉移後自己變成 `admin`。
- team 封存（`DELETE /teams/{id}`）在同一交易鎖住 hierarchy、寫 `deleted_at`、撤銷所有 zone 的 key（含 rotation grace 的舊 hash）、停用 service account 並釋放名稱；名稱只在未封存的 team 之間唯一。compiler 略過封存的 team，所有寫入路徑（含不經 team loader 的 key 與 service account 路徑）都拒絕它；歷史讀取不過濾封存，晚到的 usage 仍找得到 team 與 owner，audit 與 org 月額也含已封存 team。service account 另有停用狀態（`PATCH /service-accounts/{id}` 的 `is_active=false`），compiler 會把停用帳號的 key 排除，重新啟用後未撤銷的 key 會再進快照。
- API key 屬於一個 team 與一個不可變 zone，owner 是該 team 的成員或 service account；key 本身只有限流（`rpm_limit`／`tpm_limit`／`max_parallel`，NULL 表示沒有 key 層），owner 身分不進快照，compiler 只把展開後的有效策略寫進 key。
- Grants 的階層是 org → team → owner（member／service account）→ key，由 `quota.py` 計算全域模型權限交集；model 名稱有 grant 不代表該 zone 有 deployment。限流與預算則由同一 resolver 針對 zone 求值：limits、member defaults 與 budget 分別存於 `zone_limits`／`zone_member_defaults`，限流逐層取最小，預算每層各自累計、剩餘額取 `min(org, team, owner)`。`/users/me` 回傳各 zone 的有效值。member 的例外 grants 仍存於獨立的 `member_policies`（不依賴 `team_members`），外部授權後端沒有成員 row 時仍生效。
- 每個 zone 的限額寫入只檢查實際變更的值：不得超過該 zone 父層當下的有效值（422），沒變更的舊值不重檢；上層調降一律成功，有效值逐層取最小。建立與封存 team（`TEAM_CREATE`／`TEAM_DELETE`）、team 總額與模型（`TEAM_QUOTA_SET`）都是 org 層權限；member 預設、member 例外、service account 與任何 key 的限額是 team admin 以上；key 的擁有者可在自己的有效值內改自己的 key，service account 的 key 只有 team admin 以上能改。每個 key 最多四個限流 scope（org、team、owner、key），control plane 與 gateway（`snapshot.rs`）都拒絕更多。
- 自助 key：key 在建立時綁定 zone。Zone 可設最低建立者角色、最大效期、是否必須到期、每位 member 的 key 數上限、是否允許 service-account key 與是否允許 Playground；team 另設 `max_keys_per_member` 與 `max_member_key_lifetime_seconds`。user key 的效期／數量上限取 zone 與 team 中較嚴者並在各 zone 計數；service-account key 使用 zone 效期規則但不計 member 數量。建立、輪替、更新與恢復會檢查當下規則；收緊規則不會自動撤銷既有 key，而會回報 `rule_violations`。成員可以自行恢復自己暫停的 key（`owner`）與因被移出 team 而暫停的 key（`owner_removed`，恢復成員身分之後）；`admin` 暫停需要 team admin，`owner_deactivated` 需要管理員在帳號重新啟用之後處理，擁有者未啟用時任何人都不能恢復。
- 輪替寬限：`POST /keys/{id}/rotate` 可帶 `grace_seconds`（預設 0，最多 24 小時）。寬限內舊 hash 與新 hash 都以同一個 key_id 認證，用量歸同一個 key；舊 hash 自帶到期時間，gateway 只靠快照就能在 CP 停機、從磁碟快照開機時停止接受它。快照最多帶一個舊 hash，寬限內再輪替一次會提前結束較舊的那段寬限。撤銷會同時清除新舊 hash。過期的舊 hash 沒有背景清理：gateway 依快照自帶的到期時間停止接受它，下一次 compile 不再帶上它並以 delta 移除，DB 的欄位要到下一次輪替或撤銷才改變。
- `GET /users/me` 逐 team 回傳全域有效模型 grants，並在 `by_zone` 回傳各 zone 的可用模型（有 deployment 的項目）、有效限流、key rules／數量與本月剩餘預算；org／team 預算數字只給 team admin 以上，一般成員只看到算好的剩餘額。建立、輪替與改 key（包含限額）都以 principal 與 operation 為單位限流（各每分鐘 30 次，與目標 team／key 無關，計數在 process 內）：認證後、目標查詢與授權前檢查；安裝層管理員與緊急 session 豁免，只有 team admin 身分的 member 不豁免。成功寫入寫 audit；429 只進 metric、不寫 audit，避免灌爆稽核表。
- 授權：安裝層管理員、org admin、team admin、member，動作分得比角色細：
  - `TEAM_READ` 只給 team 基本資料（id、名稱、時間與 `max_keys_per_member`／`max_member_key_lifetime_seconds`）；每個 zone 的 member defaults 與限額透過 zone policy API 管理，member 的有效政策一律走 `/users/me`。
  - `TEAM_ADMIN_READ` 才是成員與 service account 清單、他人 key、team 用量與 audit。member 讀這些清單回 403，非成員仍是 404。
  - `TEAM_SETTINGS_UPDATE`（team admin，org admin 繼承）只含名稱與 member key 上限；member defaults 透過 `PUT /zones/{zone}/member-defaults` 設定。Team 的全域模型 grant 是 `TEAM_QUOTA_SET`，各 zone 的 team limits 依 zone policy API 授權更新；指派／降級／移除 team admin 是 `TEAM_ADMIN_ASSIGN`。建立 team 在 body 的 `org_id` 上檢查、封存 team 在 URL 那支 team 所屬的 org 上檢查，所以 org admin 只能管自己 org 的 team，team admin 完全不能建立或封存。
  - 讀他人 key 是 team admin（含 org admin 繼承）或本人：`GET /keys/{id}` 對同 team 的其他 member 回 403，team 外的人仍是 404；`GET /teams/{id}/keys` 對 member 一律 AND 上「自己的 user key」，指定別人的 `user_id`／`service_account_id`／`owner_kind` 只會得到空清單。usage 由伺服器加 owner 條件，member 只看得到自己在所屬 team 的 key 的列（含任何 group by 的加總），查詢參數不能擴大；team admin 與 org admin 維持自己管理的 team 的完整視圖。audit 只給 team admin 以上：team admin 只看得到掛在自己 team 的列，org admin 還看得到 org 層事件（org 政策、org admin、org 預算，`team_id` 為 NULL）與旗下所有 team（含已封存）的列；`GET /audit` 可用 `org_id` 篩選（`AuditOut` 也回傳 `org_id`，UI 在有多個 org 時顯示 Org 欄與篩選），member 一律 403。
  - `ROUTING_READ` 是安裝層（upstreams、models、aliases、routers、plugins、MCP servers 與 A2A agents，含 GET）；任何已認證呼叫者可以讀 `/catalog`（只有 id、name、kind），因為設定 allowlist 只需要名字。
  - 沒有「最後一位 team admin」限制：安裝層管理員永遠在 team 之上，team 可以沒有 admin。
- 安裝層角色管理：`GET /installation-admins` 列出角色，`PUT /installation-admins/{user_id}` 指派 `admin`，`DELETE /installation-admins/{user_id}` 移除 `admin`，`POST /installation-admins/transfer` 轉移 `proxy_admin`。任何安裝層管理員都能指派／移除 `admin`（含自己）；`proxy_admin` 不能透過指派或移除被降級或拔除，只能轉移；轉移只允許目前的 `proxy_admin` 或緊急 session。`proxy_admin` 帳號本身受保護：只有該帳號自己或緊急 session 能停用、刪除、改 email 或代發 PAT，其他安裝層管理員一律 403。指派與轉移要求目標至少已有一個 IdP identity（先登入過一次），否則回 409；這是因為新的 IdP identity 不能連結到已有安裝層角色的帳號，先給角色會讓該帳號永遠無法登入。每次角色變更寫 audit（`installation_admin.assign`／`remove`／`transfer`／`bootstrap`）。
- 授權後端可換：內建用 `org_admins` 與 `team_members`；內附 `openfga` 與 `http` 後端（`http` 可選擇性實作 list 與 changes：設 `LUMEWARP_AUTHZ_HTTP_LIST_URL`、`LUMEWARP_AUTHZ_HTTP_CHANGES_URL` 才啟用，未設時退回逐一 check 與定期全掃），也可用 `lumewarp.authz` entry point 接外部系統。全部後端（含三個內建）都經同一個 entry point 載入，factory 可收 `AuthzContext`（settings、session factory、metrics）或零參數。後端只回答直接關係，單一 async 函式 `check(user_id, relation, resource) -> Decision`：`relation` 是 `admin`（org 或 team）或 `member`（只限 team），`resource` 是 `org:<uuid>` 或 `team:<uuid>`；安裝層角色永遠不問後端。`admin` 隱含 `member` 由後端保證；`member` 只代表直接成員，org 繼承只由 `RequestAuthz` 套用，team→org 一律讀 Postgres、不信任 payload。`ERROR`（後端不可用或逾時）不是 deny：管理 API 回 503，解析繼承權限時也一樣，不會變成空清單。`openfga` 可用 `LUMEWARP_OPENFGA_OBJECT_TYPES`／`LUMEWARP_OPENFGA_RELATIONS` 接既有 store 的不同 type／relation 名稱，Check、BatchCheck、ListObjects、ReadChanges 共用同一份對應；`changes()` 遇到無法對應到使用者的變更（userset、wildcard）改成要求全掃，不丟棄。store 以名稱（`LUMEWARP_OPENFGA_STORE`，首次使用時解析）或 id 指定，`LUMEWARP_OPENFGA_MODEL_ID` 選填（不設即用 store 最新的 model）；`lumewarp-control-plane openfga init` 建立缺少的 store 並在沒有 model 時寫入（依對應名稱產生，不覆寫既有 model）。啟動時後端的選用 `start()` 檢查 store 與 model 是否有所有對應的 type／relation：設定錯誤中止啟動，OpenFGA 連不上只記警告（break-glass 仍可用）。
- 用量事件、月總額與限流依 key 所屬 zone、team 與 owner 分開歸屬：member 的 rate-limit scope id 是 `uuid5(team_id, user_id)`，service account 用其 id；限流 scope 為 `org:<id>`、`team:<id>`、`member:<id>`、`sa:<id>`、`key:<id>`，未設定的層不產生 scope。限流在 gateway 本機判斷；設定同一個 Redis 與 namespace 的 gateway 透過背景同步共享計數，沒有 Redis 或 Redis 不可用時每個 gateway 各算一份（N 個 gateway 最多 N 倍）。
- 預算：依 token／音訊時長與單價計費，達上限即拒絕。org 不存自己的月總額：每個 zone 的 org 花費是旗下 team（含已封存）在該 zone 的 `usage_totals` 加總，compiler 與 `/users/me` 都如此計算。只有該 zone 設了 org 預算才在 ingest 後檢查；首次超額以 `(zone, org_id, window_start)` 唯一鍵寫入 `org_budget_windows`，audit 帶 zone，下一次 compile 只把該 zone 的 key 轉成 `BUDGET_EXCEEDED`。調高或移除預算會清除該 zone 當月 window。並行批次可能互相看不到，超額判定最晚在下一次 ingest 或 compile 生效。

### Admin UI

- **Playground**（`/playground`，緊急 session 除外）透過有 session／CSRF 保護的 control plane 代理呼叫 gateway。它列出本人在未封存 team 中仍有效的 user keys，以及 caller 有 `KEY_MANAGE_ANY` 的 team 中 active service-account keys（含 org／installation inheritance）；不能使用的 key 一律回 404。每把 key 只轉送到其 zone 的 `playground_gateway_url`，而該 zone 必須允許 Playground 且設定 URL。模型選單只列該 key 可呼叫的 LLM 名稱；model、router 與 alias 會依 reasoning 設定提供可用等級。代理以 `LUMEWARP_ENCRYPTION_KEY` 派生的 HMAC 短效 token 委派 key，SSE 不緩衝轉送，不寫 audit；若回應帶 `x-lumewarp-request-id`，control plane 會記錄 `playground_requests`，供 Requests 顯示發起者。`LUMEWARP_PLAYGROUND_GATEWAY_URL` 只作為 default zone 的初始 URL；跨主機連線必須使用 TLS。
- **Navigation** is role-based; the API remains authoritative. The header's zone switcher changes zone-aware views, and installation admins manage zones from its menu. Members see My keys, Requests and Usage; team admins also see Teams and Audit; org admins see their orgs and inherited teams; installation admins see the catalog, gateways and config versions. Global identity and catalog definitions remain global when the header zone changes. The My keys page lists keys in the selected zone, and the account menu shows direct memberships and org-admin roles. Creating a catalog entry opens a dedicated edit page and warns before discarding unsaved changes. The Models list shows each model's zone deployments and health. The Plugins page lists descriptors and gateways reporting them.
- **Requests** 與 Usage 權限相同，一列一個 gateway request，記錄 Date／request id、Requested、Resolved、key、tokens、cost、status 和 outcome；routing detail 只向 installation admins 顯示。頁面依 zone 篩選，並可依時間、team、key、requested／resolved name、kind、owner、alias、router、status、outcome 與 request id 搜尋。
- Orgs 頁（`/orgs`、`/orgs/{id}`）只對 installation admins 與 org admins 顯示。Installation admins 建立、改名與封存 org，設定全域 model grants 與各 zone 的 org limits／budget，並管理 org admins；org admins 只能讀取自己的 org policy，但可在該 org 建立／封存 teams、設定 team grants 與 per-zone team quotas。頁面顯示 org 的 team、admin 和用量；建立 team 必須明確選擇 org，沒有 default org。
- 只有一個 org 時，Orgs 頁以外不顯示 org 層：Teams 沒有 Org 欄、Audit 沒有 Org 篩選、team 設定不顯示所屬 org；多個 org 時三處都出現。Orgs 頁本身永遠顯示它的 org，否則無法建立第二個 org。
- 沒有獨立的 `/keys` 管理頁：整個 team 的 key 在 team 的 Keys 分頁。team admin 看得到並管理別人的 key 與 service account 的 key；member 的 Keys 分頁只列自己的 key。Members／Service accounts 分頁只對 team admin 掛載，member 不會對這些端點發請求。Service account 可由 team admin 停用與恢復；停用後它的 key 不進快照，恢復後未撤銷的 key 重新生效。建立 key 的 owner 選單不列已停用的 service account。
- Team admins 的 Settings 分頁可改名稱與 member key 上限；global model grants 與 team per-zone limits 唯讀，per-zone member defaults 及 member/service-account exceptions 依 zone 編輯。Org admins 可改 team 的 global model grants 與 per-zone team limits。Members 只讀 team 名稱與 member key 上限。刪除 team 是不可逆封存：撤銷 keys、停用 service accounts、釋放名稱；歷史 usage 與 audit 保留。
- My keys 依 header zone 顯示 keys，pill 標示 zone；建立對話框建立一把固定綁定該 zone 的 key。使用者可管理自己的 keys，team admins 另可管理所屬 teams 的 service-account keys。Team 摘要顯示該 zone 可用的 models/MCP/A2A targets、有效 limits、剩餘 budget 與 key rules；個人用量也按 zone 查詢。
- Key 建立／編輯可設 `rpm_limit`、`tpm_limit`、`max_parallel`，只能收窄 key owner 在該 zone 的有效 limits。建立還會套用該 zone 的最小 creator role、expiry、lifetime、member key count、service-account key 與 Playground 規則。相同 Redis endpoint 與 namespace 的 gateways 才共享 rate limits；否則每個 gateway 各自計數。
- 存取變更：受保護頁面每次進入都重驗 `/users/me`。快取允許時頁面先掛上，背景重驗發現權限被收回就換成「沒有權限」；快取拒絕時先等重驗。用網址打開看不到的頁面只顯示說明，不對該 team 發請求。
- 授權後端不可用時顯示「暫時無法確認、這不是拒絕」的狀態（`authz_unavailable`），管理 API 回 503；My keys 不會把它當成「沒有 team」。
- 一次性 secret：建立或輪替 key、建立 PAT 的 secret 由 root 的同一個對話框顯示，只有「I've saved it」能關（Esc、點擊外部、標頭的 X 都無效）。它在換頁、背景檢查失敗與 session 過期（401 轉到登入頁）後仍顯示，因為對話框在 router 與 guard 之上；登出會清掉，重新載入頁面則消失。secret 不進 query cache；mutation 快取裡只有不含 secret 的結果。Gateway token 由部署環境設定，不在 UI 顯示。

### Router

- 用戶端呼叫的是全域「名字」，一個名字是 model（有全域價格與型別、可在每個 zone 綁不同 upstream deployment）、alias（model 或 router 的另一個名字，只有一層）或 router（把請求分給後面的 models）。三種共用名字空間，都可以改名，內部參照都用 id。權限依呼叫的名字檢查；可用性依 zone 的 deployment 編譯。完整規則見下方「模型目錄已實作」。
- 回應的 `model` 是呼叫時的名字；usage 的 `model` 是實際服務的 model，另外記錄來源的 alias、router、fallback 與 attempts。
- 策略是 gateway 的 plugin（`docs/router-strategies.md`）：`weighted`、`least_in_flight`、`lowest_latency`、`vllm_load` 是 native，其他策略是 WASM 元件，放在 `LUMEWARP_ROUTER_PLUGIN_DIR`，開機載入，換策略不必重 build gateway，proto、control plane、UI 都不用改。Retry、fallback、traffic split 只在 router 上；直接呼叫 model 就是一次嘗試。
- 策略：加權、最低延遲（EWMA）、最少進行中請求、vLLM 負載感知；依 team、header、輸入 token 數條件分流（延後）。
- 彈性：fallback 鏈、有上限的重試、circuit breaker、主動健康檢查、尊重 Retry-After。
- cache 親和：相同 session 或 prompt 前綴黏在同一 model（延後）。
- 變更即設定版本，可回滾；路由層百分比切流（traffic split，可依 header／team／key sticky）已實作，設定版本 canary 延後。

### MCP gateway

- **端點：** `POST /mcp` 是依 key 聚合的虛擬 server，工具與 prompt 名是 `<server>__<name>`，在第一個 `__` 切分；`POST /mcp/servers/<name>` 是單一上游，名字不加前綴。`DELETE` 轉給對應上游；`GET /mcp` 回 405，`GET /mcp/servers/<name>` 原樣轉發（`list_changed` 只在單一 server 端點收得到）。MCP 2025-11-25（Streamable HTTP）；上游回 2025-06-18 時原樣轉發，不做欄位轉換。
- **認證：** 兩個端點都只接受恰好一個 `X-Lumewarp-Key`；0 個或多個回 401，`Authorization: Bearer` 與 Playground 的 `lwpg.` token 都不算。撤銷、暫停、過期的 key 拒絕；`BUDGET_EXCEEDED` 接受，因為預算屬於 LLM 路徑。
- **出站認證：** 每個 server 一個 `UpstreamAuth`：內建 `none`（不加憑證）、`static_bearer`（token 來自 gateway 環境變數或封存的密文）、`passthrough`（把 client 的 `Authorization` 原樣送上游），或 WASM `upstream-auth` plugin。gateway 一律移除 `X-Lumewarp-Key`、`Cookie`、`Proxy-Authorization`；client 的 `Authorization` 只有 `passthrough` 保留。auth 結果依（流量、target id、params digest）快取，`passthrough` 不快取。
- **Session 與 id：** 聚合端點的 `initialize` 由 gateway 自己回答，回一個封存的 `Mcp-Session-Id`：以 `LUMEWARP_GATEWAY_ID_KEY` 經 HKDF 派生金鑰的 AES-256-GCM，內含 key id、各上游 session id 與協商版本，24 小時過期；任何一台同金鑰的 gateway 都能接手，不需要 sticky。initialize 時平行初始化這把 key 看得到的所有上游，失敗的上游在這個 session 內視為不存在，membership 之後不變。上游回 404 → client 也回 404，讓它重新 initialize。上游發給 client 的 request（elicitation、sampling、roots）id 改寫成 `"lw:<server>:<原 id JSON>"`；聚合端點的 cursor 是 gateway 封存的 (server 序號, 上游 cursor)。
- **授權與限流：** grant 依呼叫的名字（server）檢查，工具再細到單一 tool（`McpGrant` 的 `all_tools` 或 `tools`）；被授權 server 的所有 prompt 都可用。未授權的 server／tool 在清單看不到、呼叫被拒。授權是全域的，但只有 key 所屬 zone 的 server 會進 snapshot；`tools/call` 計入 key 的 zone rate-limit scope，不計 token、並行與預算。
- **定義審核：** `review_mode` 是 `tofu`（預設）／`manual`／`off`。gateway 從上游觀察 tools 與 prompts；hash 是 RFC 8785 正規化 JSON 的 sha256（tool 取 name、title、description、inputSchema、outputSchema、annotations；prompt 取 name、title、description、arguments，其他欄位丟棄）。`tofu` 對沒見過的名稱自動核准並回報，已知名稱換了定義就從 list 移除、呼叫也拒絕，直到核准；`manual` 一律要核准；`off` 不檢查。快照只帶每個已知定義的名稱、hash 與 state（approved／pending／rejected），不帶定義本身；未指定的 state 視為 pending。
- **回報：** 被擋下的定義與 `tofu` 首次核准的定義用 `CatalogReport` 回報（與 `HealthReport` 同一條 config stream），control plane 自行重算 hash；同一 (server, hash) 每台 gateway 每 10 分鐘最多一次，每 server 待回報最多 200 筆、每筆 64 KiB，超過丟棄並計 `lumewarp_mcp_catalog_reports_dropped_total`。
- **TOFU 是多 gateway 的本地決定：** 第一台看到新名稱的 gateway 立刻服務並回報，control plane 對該名稱以第一筆回報為準（first writer wins），之後不同的 hash 一律 pending；在回報與新快照抵達其他 gateway 之前，兩台可能短暫服務同一名稱的不同定義。這只偵測定義變更，不證明上游實際執行什麼。
- **Discover now：** auth 是 `none`、`static_bearer` 或 `passthrough` 的 server，編輯頁可由 control plane 打一次 `tools/list` 與 `prompts/list`。`none` 不需要 token；`static_bearer` 用 server 已存的 secret（環境變數或封存密文）或管理員當次貼上的 token；`passthrough` 需要管理員貼上使用者自己的 token。貼上的 token 只用一次、不儲存、不稽核，錯誤訊息也不回上游內容。只有 URL 與已存設定在呼叫期間沒變時，結果才以同樣的 hash 記錄，否則只是預覽；WASM auth 的 server 只能從流量觀察。
- **大小上限：** request body 4 MiB（413）、非串流回應 16 MiB、單一 SSE event 1 MiB（502）。

### A2A gateway

- **端點：** `POST /a2a/<agent>`（JSON-RPC，含 SSE 串流與 `SubscribeToTask`）、`GET /a2a/<agent>/.well-known/agent-card.json`、`GET /a2a`（列出這把 key 能用的 agent）。認證規則與 MCP 相同（恰好一個 `X-Lumewarp-Key`），出站認證也用同一個 `UpstreamAuth`。A2A v1.0 JSON-RPC binding；每個請求要 `A2A-Version: 1.0`，缺或不同回 `-32009`；push notification 設定一律回 `-32003`。
- **id 擁有權：** task 與 context id 包成 `lwt.<owner>.<原 id>.<HMAC>`，owner 是 key 本身（同一個人的另一把 key 也看不到），MAC 金鑰是 `LUMEWARP_GATEWAY_ID_KEY`。`ListTasks` 只在 agent 的出站 auth 是 `passthrough` 時轉發（上游以使用者自己的 token 隔離），其他回 `-32004`。
- **Agent Card：** gateway 用 agent 的出站 auth 定期抓 card（預設 5 分鐘，含 jitter；`card_url` 留空是 `url` 的 origin 加 `/.well-known/agent-card.json`；憑證只送往同 origin 的 card URL），經 `CatalogReport` 回報，抓失敗時沿用上次的 card。對外的 card 是 gateway 改寫的：`supportedInterfaces` 只留一個 JSON-RPC 1.0 介面，URL 是 `LUMEWARP_GATEWAY_PUBLIC_URL` 加 `/a2a/<agent>`（不看 Host header）；`securitySchemes`／`securityRequirements` 換成 apiKey `X-Lumewarp-Key`；移除 `signatures`；`capabilities.pushNotifications=false`。`GetExtendedAgentCard` 同樣改寫。`passthrough` agent 抓不到 card 時用管理員貼上的 `card_json`。
- **Middleware：** 一個 agent 最多一個 WASM `a2a-middleware`，request 與 response（含每個 SSE event）都能改；只有 `TaskArtifactUpdateEvent` 可以被丟棄，丟棄其他 event 視為錯誤。
- 沒有 `LUMEWARP_GATEWAY_ID_KEY` 時 MCP 與 A2A 都回 503；只有 `LUMEWARP_GATEWAY_PUBLIC_URL` 缺少時只有 A2A 回 503。LLM 不受影響。

### Gateway plugin（WASM）

- 三種 gateway plugin 都是 WASM component（wasmtime + Component Model，WIT 介面在 `gateway/wit/`）：`router-strategy`、`upstream-auth`、`a2a-middleware`。開機時讀三個目錄：`LUMEWARP_ROUTER_PLUGIN_DIR`、`LUMEWARP_AUTH_PLUGIN_DIR`、`LUMEWARP_A2A_MIDDLEWARE_DIR`，每個 `.wasm` 呼叫一次它 export 的 `manifest()`（name、kind、title、description、params schema、ABI）。換 plugin 是換檔加上重啟，沒有熱載入；kind 不符、名稱重複、ABI 不符或 manifest 不合法都拒絕啟動。內建策略與內建認證（`none`、`static_bearer`、`passthrough`）是 native code；內建認證只給 MCP／A2A，LLM upstream 只接受 WASM `upstream-auth`（`Upstream.auth`，與 API key 擇一）。LLM 熱路徑只在 router 指名 WASM 策略或 upstream 有 WASM auth plugin（且 cache 未命中）時才進 WASM。
- **LLM upstream auth：** 每次嘗試（retry、fallback）都對該 attempt 的 upstream 呼叫 `authorize`；upstream health check、vLLM metrics scrape 與已存的 Test now probe 也走同一個 authenticator。`cache-ms > 0` 時依（traffic、upstream、params digest）快取，cache hit 不進 sandbox；`needs_body`（manifest，預設 false）決定 LLM body 是否傳給 plugin，MCP／A2A 一律帶 body。WIT package 仍是 `lumewarp:plugin@1.0.0`，`traffic` 多了 `llm`。configure／plugin／trap／timeout／header 失敗回 502 `upstream_auth_failed`，並計入 `lumewarp_upstream_auth_total` 與 `upstream_auth` span。
- **回報與 capability：** `Hello.plugins` 帶每個 plugin 的 descriptor（含 `builtin`、`wasm_sha256`、`digest`），`Hello.plugin_abi` 帶 ABI；gateway 為每個 plugin 廣告 `strategy:<name>`、`auth:<name>` 或 `a2a_middleware:<name>`。快照用到 gateway 沒有的 plugin → 該 gateway NACK `UNSUPPORTED_FEATURE`；control plane 對指名它的寫入回 422，帶 `force` 才能存；gateway 另廣告 `upstream_probe`，表示可代 control plane 執行已存的 Test now probe（只限與該 control plane process 有連線的 gateway）。
- **沙箱與時限：** 每次呼叫有 timeout（`Router.plugin_timeout_ms`、`PluginRef.timeout_ms`、`Upstream.auth.plugin.timeout_ms`，0 = 60 s；WASM plugin 適用，內建 plugin 忽略），只有設定的 timeout 或 client 斷線會取消，慢的 plugin 只拖慢呼叫它的請求，不佔 tokio worker。每個 instance 上限 32 MiB，全部 instance 共用 1 GiB（`LUMEWARP_PLUGIN_MEMORY_LIMIT_BYTES`）。`upstream-auth` 另有 `http-fetch`（只限 `params.allowed_origins`）與 `secret()`（只讀 `LUMEWARP_PLUGIN_SECRET_*`）；router 與 middleware 沒有任何 host I/O。trap／timeout：router 走 fall-through，MCP／A2A 的 auth 與 middleware 回 502／504，串流中則以最後一個 JSON-RPC error event 結束；LLM upstream 的 auth 失敗一律 502 `upstream_auth_failed`。
- `lumewarp plugins [--json]` 只讀目錄、不連 control plane，列出這個 process 會有的 plugin（含 built-ins），是部署前的檢查。完整介面、manifest 與載入規則見 [gateway plugins](plugins.md)。

## Python plugin

以 entry points 註冊（`lumewarp.auth`、`lumewarp.authz`），`pip install` 後重啟管理端生效。**Python 永遠不在 gateway 熱路徑上。**

| 擴充點 | 執行位置 | 例子 | 延遲影響 |
| --- | --- | --- | --- |
| 認證 plugin（`lumewarp.auth`） | control plane，只在首次 SSO 登入 | `map_identity(claims)` 把外部身分對應到 email 與顯示名稱，不授予權限 | 無 |
| 授權後端（`lumewarp.authz`） | control plane | `check(user_id, relation, resource)` 接公司自有 AAA；介面見 [Python plugins](./extending/python-plugins.md#writing-an-authorization-backend) | 無 |
| 逐請求 hook（選用，延後） | gateway 呼叫外部 gRPC 服務 | 依 prompt 分類選模型 | 多一次往返，需明確開啟，有逾時與預設路由 |

## vLLM / OpenAI 一級支援

每個 model 必須明確指定 `model_type`（API）／`type`（snapshot 與 standalone YAML），沒有預設型別；未指定的 snapshot 會 NACK `INVALID_CONFIG`。支援四種型別：

| 型別 | POST 端點 | 請求 body |
| --- | --- | --- |
| `llm` | `/v1/chat/completions`、`/v1/completions`、`/v1/responses` | JSON |
| `embedding` | `/v1/embeddings` | JSON |
| `transcription` | `/v1/audio/transcriptions`、`/v1/audio/translations` | multipart/form-data |
| `decision` | `/v1/decisions`、`/v1/systemone` | JSON，非串流 |

- Alias 繼承 target 的型別；router 的所有有效 members、split members 與 fallbacks 必須同型，API 拒絕混型設定（422），gateway 也驗證並 NACK。授權依呼叫的名字檢查後，型別不符的端點呼叫回 400 `model_type_mismatch`，計入既有 rejected metric，並產生帶 requested／endpoint 的 REJECTED usage event。
- `GET /v1/models` 的每個 model、alias、router 項目帶 `type`；清單與型別從 snapshot 建立，不在請求路徑查控制平面。
- `/v1/responses` 只接受 POST；`previous_response_id`、response ids 與未知欄位原樣透傳，`GET/DELETE /v1/responses/{id}` 是 404，沒有 id sealing。vLLM 額外參數（guided decoding、`extra_body`）也直接透傳。
- Transcription 從已緩衝、上限 32 MiB 的 multipart body 找出 `model` 與 `stream` text parts；替換上游 model 時以 reference-counted slices 保留檔案 bytes，重試重用同一份 upload。採嚴格解析：缺少 boundary／model、重複 boundary／Content-Type、preamble、非空 epilogue、`name*`、空 headers、跨越下一 delimiter 的 headers、多個 Content-Disposition，或不分大小寫的重複 model／stream 均回固定訊息的 400 `invalid_request`；model／stream 不得為 file parts，參數以 quote-aware tokenizer 解析但拒絕反斜線跳脫，delimiter 接受 RFC 2046 transport padding。每次請求最多 256 parts、256 個無效 delimiter 候選，超過也回 400；搜尋共用同一個 Finder 以限制同步 CPU 工作。轉發 Content-Type 改寫為只含實際解析 boundary 的 `multipart/form-data; boundary="..."`，不沿用原始參數。
- 自動探索：定期讀 `/v1/models`，註冊模型與 LoRA adapter（延後）。
- 負載感知路由：讀 vLLM `/metrics` 的等待中請求數與 KV cache 使用率。
- prefix cache 親和（延後）。
- 健康檢查分兩層。upstream 層：Endpoint 的 `health_url` 是完整 URL，獨立於定期探測設定；留空時不分 kind 都用去掉結尾斜線的 base URL 加 `/models`，啟用 `health_check` 後定期 GET，2xx 為正常。model 層（deployment 的 `chat_check`）：選用的型別專屬定期檢查，gateway 在每個 zone 用該 model deployment 的 upstream 憑證（`api_key_ref`／封存 key 或 `Upstream.auth` plugin）送 POST：LLM 對 `<base>/chat/completions` 送非串流 `"1 + 1 = ?"`（`max_tokens` 取 16 與 `max_output_tokens` 的較小值）；embedding 對 `/embeddings` 送 `input: "ping"`；decision 對 `/decisions` 送一個 predicate 問題；transcription 對 `/audio/transcriptions` 以 multipart 上傳程式生成一次的靜音 WAV（44-byte RIFF header、1 秒、16 kHz、mono、16-bit PCM），帶 `model`、`response_format=json` 與 `file`（`probe.wav`、`audio/wav`）。JSON probe 經過該 model 的 default body 與 LLM reasoning 規則，2xx 為正常，probe 不計用量；異常時只排除這個 model（upstream 上其他 model 不受影響），所有候選都異常時照常 fail open。bearer token 只送往與 base URL 同 origin（scheme、host、port）的 URL。gateway 在 `Hello` 後、每次 ACK 後與探測狀態改變時（每秒最多一次）送 `HealthReport{upstreams, models}`，control plane 按 zone 存進 `gateway_sessions.upstream_health` 與 `model_health`；Upstreams 與 Models 頁的狀態在各 zone 內由已連線 gateways 彙總，任一台回報異常即異常；沒有定期檢查（或還沒有 gateway 回報）時，顯示最近一次對已儲存設定的 Test now 結果（Success／Failed，tooltip 顯示時間與上次成功時間）。upstream 結果存在 `probe_at`／`probe_ok`／`probe_ok_at`，只有測的內容與已存設定相同時才記錄；model probe 結果存在該 zone 的 deployment，當 deployment 的 upstream、upstream model 或 chat-check 改變時清除。Model 編輯頁的「Test now」由 control plane 依表單與所選 zone 立即送一次同型別的 probe（`POST /models/test`，必填 `model_type`，可指定 `zone`），顯示狀態、延遲與回答開頭，不需先儲存，也不需開啟定期檢查。編輯頁的「Test now」由 control plane 依表單目前的內容（不必先儲存）立即探測一次 `health_url`（留空時為 `<base_url>/models`，`POST /upstreams/test`）：表單裡輸入的 key 只用這一次、不儲存；沒輸入時用已存的 key，或 control plane 自己環境裡的變數，回應會說用了哪一種。
- 模型清單：`GET /upstreams/{id}/models` 由 control plane 讀上游的 `/models`（OpenAI 格式），給 model 編輯頁當選單，也可以輸入清單外的名稱。上游有回報長度時一併帶入：輸入長度取 `max_model_len`（vLLM）、`context_length`、`context_window` 或 `max_input_tokens`，輸出長度取 `max_output_tokens`、`max_completion_tokens`，沒有時用 context window。不在請求路徑上。
- 用量計量：讀取上游 `usage.prompt_tokens/completion_tokens` 或 `usage.input_tokens/output_tokens`；Responses SSE 的最終 usage 在 `response.completed`／`response.incomplete`／`response.failed` 的 `response.usage`，transcription SSE 在 `transcript.text.done` 的 `usage`，也接受 vLLM 最後一個 chat-like chunk 的頂層 usage。只有 chat/completions 串流會強制加入 `stream_options.include_usage`；其他端點不注入也不剝除 usage event。Transcription `usage.type="duration"` 的 seconds 四捨五入為事件的 `audio_ms`，tokens 為 0；`type="tokens"` 記 input/output tokens，audio_ms 為 0。text／srt／vtt 回應沒有 usage，記為 MISSING。
- 價格：API／snapshot 的 input/output 以 nano-USD／Mtok 設定，YAML 使用精確十進位 USD／Mtok；transcription 另可設 API `price_nano_usd_per_audio_second`（snapshot `nano_usd_per_audio_second`、YAML `audio_usd_per_second`）。成本為 tokens × token 單價／1,000,000 加 audio_ms × 每秒單價／1,000，沿用整數 fixed-point 計費；TPM 只累計 tokens。Embedding output 價格必須是 0／未設，音訊價格限 transcription；未設價格是 unknown，只有整個 Price 明確全零才是 free；非全零 Price 若回報單位的對應單價全為零（tokens 的 input/output 皆零，或 duration 的 audio 單價為零），成本為 unknown，而非免費。Transcription 的 token 0/0 搭配非零音訊價格時，token usage 的成本仍為 unknown。

## Metrics 與 Alert

gateway 原生輸出 Prometheus `/metrics` 與（選用）OTLP traces；control plane 在獨立的 metrics listener（`LUMEWARP_METRICS_BIND`，預設 `127.0.0.1:9465`，不在 admin API port）輸出 `lumewarp_cp_*`（下表後半，完整說明見 [Control plane](./operations/control-plane.md)）；control plane 的 OTLP 尚未實作。

| 指標 | 標籤 | 用途 |
| --- | --- | --- |
| `lumewarp_requests_total` | requested, endpoint, model, team, status, error_source, code | 流量與錯誤率；`error_source` 是 none／client／policy／upstream／gateway，`code` 是 `error::CODES` 的錯誤碼、`upstream_<status>`（上游原樣回傳）或 `client_cancelled`（499），成功為空；錯誤狀態的 series 不再預先註冊為 0，第一次發生才出現 |
| `lumewarp_gateway_info` | zone, gateway_id, version | 值恆為 1；規則以 `group_left(zone)` 替只有 namespace／instance 的 gateway 序列補上 zone（每次 `Welcome` 換掉舊序列） |
| `lumewarp_rate_limited_total` | scope（key／team／member／org／service_account／other） | gateway 自己因限流回的 429（LLM 與 MCP `tools/call`）；上游的 429 不計入 |
| `lumewarp_request_attempts` | requested | 每個走到上游嘗試迴圈的請求的嘗試次數直方圖（桶 1、2、3、4、5、8） |
| `lumewarp_playground_requests_total` | result（ok／rejected） | Playground token 認證結果，不含 key 標籤 |
| `lumewarp_request_duration_seconds` | requested, endpoint, model | 端到端延遲 |
| `lumewarp_gateway_overhead_seconds` | endpoint | gateway 自身額外延遲 |
| `lumewarp_ttft_seconds` | requested, model | 串流 TTFT（沒有 TPOT 指標） |
| `lumewarp_tokens_total` | requested, team, direction | token 用量 |
| `lumewarp_spend_total` | team | 花費 |
| `lumewarp_upstream_circuit_state` | model | 熔斷與健康 |
| `lumewarp_upstream_attempts_total` | requested, model, outcome | 每次上游嘗試；success / retryable_status / connect_error / timeout / circuit_open / non_retryable |
| `lumewarp_response_model_rewrite_compressed_total` | model | 需要改寫回應 `model` 但上游仍壓縮：原樣轉發、不解壓縮，用戶端看到上游模型名，usage 為 invalid |
| `lumewarp_requests_rejected_total` | model, reason | model 設定在送往上游前回 400 的請求；reason 是 max_output_tokens／reasoning_effort／invalid_body／model_type_mismatch |
| `lumewarp_fallbacks_total` | router, model | 切換至 fallback model |
| `lumewarp_retries_budget_exhausted_total` | router | 全域重試額度耗盡 |
| `lumewarp_upstream_healthy` | upstream | 主動健康檢查狀態（1 正常、0 異常）；探測不計入請求與用量 |
| `lumewarp_model_healthy` | model | 型別專屬 model probe 狀態（1 正常、0 異常；沒設定 `chat_check` 時為 1）；不計入請求與用量 |
| `lumewarp_vllm_queue_depth`、`lumewarp_vllm_kv_cache_usage` | model | vLLM 負載（每秒背景抓取；`vllm_load` 路由使用） |
| `lumewarp_vllm_scrape_failures_total` | upstream | vLLM metrics 抓取失敗（失敗即退回 in-flight 比較） |
| `lumewarp_upstream_latency_ewma_seconds` | model | `lowest_latency` 使用的回應標頭時間 EWMA（於 `/metrics` 抓取時計算） |
| `lumewarp_split_requests_total` | router, split | 依 traffic split 分配的請求數（canary 比例） |
| `lumewarp_session_affinity_total` | router, result（hash／override／moved／none） | session affinity 的選模結果：無狀態 rendezvous 雜湊、沿用移轉後的 override、因最佳成員不可用而移轉、或沒有 session id |
| `lumewarp_session_affinity_overrides` | — | 本程序現存的 session→模型 override 數（抓取時清除過期項） |
| `lumewarp_session_affinity_sync_errors_total` | reason（connect／timeout／script） | session affinity override 的 Redis 同步失敗（只在設定共享限流 Redis 時存在） |
| `lumewarp_mcp_requests_total` | server, method, status | MCP 請求；method 是轉發的方法或 `notification`／`other`，status 是 HTTP 狀態 |
| `lumewarp_mcp_tool_calls_total` | server, tool, team, status | MCP tool 呼叫；`tool` 只放已核准的名稱，其餘是 `_unapproved`（避免上游製造標籤） |
| `lumewarp_mcp_upstream_duration_seconds` | server | MCP 上游回應標頭時間 |
| `lumewarp_mcp_catalog_reports_dropped_total` | reason（oversized／pending_limit／invalid） | 定義觀察在回報前被丟棄 |
| `lumewarp_a2a_requests_total` | agent, method, status | A2A 請求；method 是 JSON-RPC 方法標籤，card 端點是 `agent_card` |
| `lumewarp_a2a_upstream_duration_seconds` | agent | A2A 上游（含 card 抓取）回應標頭時間 |
| `lumewarp_plugin_calls_total` | kind, plugin, result（ok／error／timeout／trap） | 請求路徑上的 plugin 呼叫；`manifest`／`configure` 與 built-ins 不計 |
| `lumewarp_plugin_duration_seconds` | kind, plugin | 同一批呼叫的延遲直方圖 |
| `lumewarp_upstream_auth_total` | upstream, result（ok／cached／plugin／timeout／trap／header／configure） | LLM upstream 出站認證；`cached` 未進 sandbox |
| `lumewarp_shared_limits_sync_duration_seconds`、`lumewarp_shared_limits_sync_errors_total` | result；reason（connect／timeout／script） | 共享限流的 Redis 同步（只在設定 Redis 時存在） |
| `lumewarp_shared_limits_fallback`、`lumewarp_shared_limits_last_sync_age_seconds`、`lumewarp_shared_limits_scopes` | — | 是否退回本機計數、距上次成功同步的秒數、上次同步的 scope 數 |
| `lumewarp_config_version`、`lumewarp_config_apply_failures_total` | gateway | 設定同步 |
| `lumewarp_cp_compile_duration_seconds`、`lumewarp_cp_compile_failures_total` | — | 設定編譯（CP） |
| `lumewarp_cp_config_version`、`lumewarp_cp_outbox_pending` | `zone` | 每個 zone 的最新發布版本與 outbox 積壓（CP） |
| `lumewarp_cp_gateway_sessions`、`lumewarp_cp_gateway_acks_total` | `zone`；acks 另有 `result`（ack／nack） | config stream 連線與回覆（CP） |
| `lumewarp_cp_gateway_plugins_ignored_total` | kind | `Hello` 中超出上限或名稱不合法而被忽略的 plugin（CP） |
| `lumewarp_cp_mcp_catalog_definitions_total` | result（approved／pending／known） | 從回報與 Discover 存入的 MCP 定義（CP） |
| `lumewarp_cp_mcp_catalog_discarded_total` | reason | 被丟棄的 MCP 定義回報（id／版本過舊、hash 不符、超出上限等；CP） |
| `lumewarp_cp_mcp_definition_reviews_total` | action（approve／reject） | 管理員的定義審核（CP） |
| `lumewarp_cp_mcp_discover_requests_total` | result（ok／failed） | Discover now 呼叫（CP） |
| `lumewarp_cp_a2a_agent_cards_total` | result（stored／discarded） | 從 gateway 回報記錄的 A2A card（CP） |
| `lumewarp_cp_usage_events_ingested_total`、`lumewarp_cp_usage_events_skipped_total` | reason（skipped） | 用量匯入（CP） |
| `lumewarp_cp_authz_checks_total` | backend, decision | 授權後端（CP） |
| `lumewarp_cp_keys_suspended_total`、`lumewarp_cp_key_reconcile_last_success_timestamp_seconds` | reason（suspended） | key 停用與 reconciler 新鮮度（CP） |
| `lumewarp_cp_key_writes_total` | operation（create／rotate）, result（success／rate_limited） | 自助建立／輪替 key 與其速率限制（CP） |

### Gateway 指標定義

上表之外，gateway 另有 `lumewarp_request_body_read_seconds`、`lumewarp_upstream_errors_total`、`lumewarp_in_flight_requests` 與 `lumewarp_config_info`，都由獨立 admin listener 的 `/metrics` 輸出；`lumewarp_config_version` 在 YAML 模式為 0。OTLP traces 為選用，沒有 OTLP metrics。非串流與串流的 overhead 均為「body 讀完 → upstream request 送出」加「upstream headers 收到 → handler 返回」；串流再加第一個 frame 已 ready 到轉交的本地處理時間，不含等待 upstream headers、prefill、後續 body 生成或 client socket flush／ACK。未路由的請求以 body 讀完到 handler 返回近似，未讀 body 則從 request start 計；取消中的 upload 只進 body-read duration，不算 overhead。此為內部處理時間近似，不同於 bench 的 gateway／direct 端到端延遲差值。完整操作、TTFT／body-read 與標籤定義見 [Gateway](./operations/gateway.md)。

`endpoint` metric label 的有界值為 `chat_completions`、`completions`、`responses`、`embeddings`、`audio_transcriptions`、`audio_translations`、`decisions`、`systemone`，以及本地的 `models`、`healthz`、`metrics`、`other`；proxy dispatch、預先建立的上游 URI、usage 與 metrics 共用同一份 endpoint table。每個 model 只預建自身型別端點的 metric handles，不為其他型別擴增 series。

LLM upstream auth 另輸出 `upstream_auth` span（upstream、plugin、method、result、cached）；`lumewarp_upstream_auth_total` 的 `cached` 代表 cache hit，`ok` 代表 WASM 回答。API key 級細節不進 metrics（避免標籤爆炸）。Alert：Helm chart 可選 ServiceMonitor（gateway admin port 與 control plane metrics port）；Prometheus 告警規則（`deploy/prometheus/rules.yaml`，27 條 alert、4 條 recording rule，附 promtool 測試）與五張 Grafana dashboard（`deploy/grafana/dashboards/`，Helm 可選安裝）；每個請求一條 OTel trace，接續 W3C `traceparent`。log 預設不記 prompt/response。錯誤的 `type` 欄位：401 `authentication_error`、403 `permission_error`、429 依碼分 `insufficient_quota`／`rate_limit_error`、502／504 `upstream_error`，408 與其他 5xx 為 `api_error`，其餘 4xx 為 `invalid_request_error`。

### Alertmanager、狀態橫幅與 incident（已實作）

Lumewarp 自己不判斷「壅塞」或「掛了」；那是 Prometheus 規則的工作，control plane 只接收 Alertmanager 的結果，並只呈現自己的資料（收到的 alert 與 request log）。

- **接收端：** `POST /api/v1/alerts/alertmanager` 接 webhook v4。認證是 `LUMEWARP_ALERTMANAGER_TOKEN`（至少 32 字元）的 bearer，常數時間比較，不走 session／CSRF；未設定時回 404；先驗 token 再讀 body（未設定 404、錯誤 401，與 body 內容無關），body 以分塊讀取、超過 1 MiB 回 413，`version` 必須是 `4`，每次最多 1000 個 alert；不逐筆稽核，也沒有請求速率限制（token 加上 body／alert 上限就是邊界）。每個 alert 以 `(fingerprint, startsAt)` upsert 進 `alerts`：重送冪等，`resolved` 寫入 `endsAt`（持續時間即 MTTR），已 resolved 的列不會被遲到的 firing 重送重新打開；列永不刪除。
- **目標解析：** label `requested`／`model` 對應目錄名字（model、alias、router 同一個全域命名空間），`upstream` 對應 upstream（有 `zone` 只找該 zone，否則每個有該名稱的 zone），結果寫入 `alert_targets`，每次收到都重寫；找不到的名字不是錯誤，alert 照存，只有管理員看得到。`zone` label 限定範圍，缺少或空字串＝所有 zone（獨立 gateway 與 `LumewarpGatewayDown` 沒有 zone）。
- **顯示契約：** label `lumewarp_status`（`down`、`degraded`、`congested` 或自訂文字）讓 alert 對使用者可見，annotation `lumewarp_summary` 是給使用者看的文字，`summary`／`description` 只給管理員。出貨的規則有七條帶 `lumewarp_status` 並以 `lumewarp_gateway_info` join 出 `zone`。
- **`GET /status`：** 任何登入者可讀。回傳 firing 且有 `lumewarp_status` 的 alert 中，與「呼叫者在某 zone 可呼叫的名字」相交者（有效 model 的算法同 `/users/me`；upstream 目標展開成該 zone 部署在其上的 model）：`name`、`zone`、`state`、`summary`、`since`；同一 zone 與名字只回一筆（狀態優先序 `down` > `congested` > `degraded` > 其他，`since` 取該狀態最早的 alert）；model 目標（或 upstream 展開的 model）也會對指向它的 alias 與 router（成員、split、fallback，及其 alias）呼叫者顯示，名字是呼叫者可呼叫的那個；可呼叫性比照 compiler（只有 fallback 部署的 router、有 split 未部署的 router、權重全為 0 的 router 都不算）。不回 upstream 名稱、label 或其他租戶；安裝管理員另有 `alert_id` 供連結。
- **Incident 分組：** incident 是同一次事故的一組 alert。alert 第一次收到時，若與某個既有 incident 有相同 `alertname`、相同 zone（沒有 zone 只與沒有 zone 的相符）、相同的已解析目標（以 kind 與 id 比對；沒有目標的 alert 改比對 label，忽略 `instance`、`gateway_id`、`pod`、`job`、`endpoint`、`service`），且時間區間重疊（在 incident 結束前開始，或 incident 仍在 firing），就加入該 incident，否則開新的 incident。incident 的視窗是成員的聯集：最早的開始、任一成員 firing 即 firing、最後一個成員結束時結束；嚴重度與使用者可見狀態取最嚴重的成員。歸屬只在第一次收到時決定，之後不會移動（目標重寫不重新分組）。因此出貨的逐 gateway 規則在一次事故中只產生一個 incident，顯示「由 N 個 gateway 回報」（成員中不同 `instance` 的數量，沒有 `instance` 時用 `gateway_id`）；規則不需改成聚合。`/status` 與橫幅仍讀 firing 的 alert 本身，不受分組影響。
- **Incident（安裝管理員）：** `GET /incidents`（keyset 分頁，可依 zone、status、影響範圍（使用者可見／僅管理員）、時間過濾，另有一個搜尋框比對 alert 名稱、summary 與目標名字）、`GET /incidents/{id}`（成員 alert、受影響的可呼叫名字、前後一小時內共用目標的相關 incident；`usage_events` 在 [start − 10 分鐘, end 或現在] 內、限於受影響名字與 zone 的流量：每分鐘請求數（成功、經 fallback、失敗）、失敗數、錯誤碼與狀態類別、fallback 比例與救回數（經 fallback 且未失敗、沒有錯誤碼，與每分鐘的 fallback 同一判準）、嘗試次數分佈、前 10 名 team 與 key（含失敗數）、最近 50 筆失敗請求的中繼資料（狀態 ≥ 400、有錯誤碼，或 200 之後串流中斷的 `upstream_error`）；視窗最多 24 小時，超過會裁切並標示）。`usage_events` 新增 `error_code` 與 `ttft_ms`，Requests 也顯示並可依 `error_code` 過濾。
- **不查 Prometheus：** control plane 不對 Prometheus 發出查詢，incident 只連回觸發的運算式（alert 的 `generatorURL`）；pod、CPU、記憶體、GPU 等圖表留在 Prometheus／Grafana。
- **不做：** 轉發 alert 到 Slack 等其他 receiver（Lumewarp 只接收）；查詢 Prometheus 畫圖；Langfuse 整合。使用手冊見 [Alerts and incidents](./operations/alerts-and-incidents.md)。

## Control plane 與 gateway

以下是 control plane、設定串流、Team 模型與 Admin UI 的實作；設計內容以上文為準。

**Control plane（`control-plane/`）**

- 單一 Python 程序：FastAPI（HTTP 8091）＋ gRPC（50051）的 ConfigService／UsageService／health，加上 compiler loop 與月結 rollover；`/readyz` 檢查 DB、migration head 與背景服務，`/healthz` 為 liveness，gRPC 意外停止會讓程序結束。
- Postgres schema 由 Alembic 管理，多個 zones 共用一套安裝層身分與目錄；每人 PAT；授權為安裝層管理員、org admin 與 team admin／member，成員關係在 `team_members`，每人例外 grants 在獨立的 `member_policies`，也可換成外部 `lumewarp.authz` 後端。
- REST `/api/v1`：org（建立、改名與政策、封存、org admin、用量彙總）、team／member／service account／key（含撤銷與輪替）、預算／限流／模型 allowlist、upstream／model／router（含價格、upstream auth plugin、已存設定的 Test now）、MCP server（含定義清單、核准／拒絕、Discover now）與 A2A agent、`/plugins`（三種 kind 的唯讀 descriptor）、使用者、安裝層角色（`/installation-admins`）、usage 聚合、request log（`/requests`）、audit、gateway 狀態與刪除離線 gateway、config versions 與 routing rollback。
- 設定發布：全域 hierarchy 寫入交易內為每個 live zone 更新 source revision 並寫各自 outbox，zone-bound 寫入只觸及其 zone；單一 compiler loop 逐 zone 以 advisory lock 序列化，產生不可變版本快照。gRPC 串流支援全量／delta、digest、ACK／NACK、base mismatch 重送全量、zone 內壞版本隔離與 persist 重試，舊版 payload 依保留策略壓縮。`settings.zone`（預設 `default`）與每個 `LUMEWARP_GATEWAY_ZONE_<ID>_TOKEN` 都會在 control plane 啟動時建立 zone；UI/API 不建立 zone。Gateway token 不再由 CLI 建立。
- Test now 與 probe：沒有 plugin 的 upstream／model 由 control plane 直連（未存檔可測）；有 WASM auth plugin 的須先存檔，`POST /upstreams/{id}/test`、`POST /models/{model_id}/deployments/{zone}/test` 經 zone config stream 的 `ConfigUpdate.probe`（`ProbeRequest{request_id, upstream, model}`）交給一個已連線、廣告 `upstream_probe`（plugin upstream 另需 `auth:<plugin>`）的 gateway，對它當下的 snapshot 執行；gateway 回 `GatewayMessage.probe_result`（`ProbeResult{request_id, ok, status, latency_ms, error_class}`），不回 upstream body，probe 也不是版本、不 ACK／NACK。control plane 用 request_id 對應並自己計時（504 `probe_timeout`），沒有合適 gateway 回 409 `no_gateway_for_plugin`，gateway 斷線回 502 `probe_gateway_gone`；只涵蓋與該 process 有連線的 gateway。
- 用量與預算：`UsageService.Report` 依 `x-lumewarp-zone` metadata 和 `request_id` 冪等寫入，同一交易在該 zone 累計 team 與 owner（member／service account）的 UTC 月總額並評估預算；gateway id 來自最近一次 `Welcome`，Welcome 前的 report 不歸屬 gateway。org 花費由該 zone 旗下 team 加總，只有該 zone 設了預算才檢查，超額寫該 zone 的 `org_budget_windows` 與 audit；調高或移除預算會清掉該 zone 當月 window。超額把 key 翻成 `BUDGET_EXCEEDED` 並推新版本（eventual，約 5 秒內；不切斷進行中的串流）。
- Plugin：Python 的 `lumewarp.auth`（首次 SSO 登入的身分對應）與 `lumewarp.authz`（授權後端）已接上；gateway 端的 `router-strategy`、`upstream-auth`、`a2a-middleware` 是 WASM，由 gateway 自己載入，control plane 只驗證 gateway 在 `Hello.plugins` 回報的 schema。
- Admin UI：Vite 靜態檔由 control plane 掛在 `/`，用 SSO 或緊急登入；header zone switcher 切換 zone-aware pages，installation admins 可由選單編輯／封存 zones，zone 由啟動時的環境 token 宣告。API 文件在 `/docs`，OpenAPI 可離線輸出。UI 沒 build 時只有 API。MCP servers 與 A2A agents 各有列表與編輯頁（A2A 編輯頁有 card 預覽），Plugins 頁唯讀列出哪台 gateway 載了什麼；team／member 的政策編輯含 MCP server／tool 與 agent grant；Usage 有 LLM／MCP／A2A 三個分頁，Requests 頁一列一個請求並可依名字、狀態、team、key、owner 與時間篩選；My keys 的 team 展開列顯示 MCP server／tool 與 A2A agent grant，team admin 另有 service account key 表。

**Gateway（`gateway/`）**

- control-plane 模式使用 `--control-plane`、`--gateway-token-env`、`--state-dir`、`--zone`（`LUMEWARP_ZONE`）與選用的 `--local-config`；gateway 在 `Hello.hostname` 提供 hostname，control plane 在每條串流的第一個 `Welcome` 指派 gateway id。每次 reconnect 都會取得新 id。Standalone YAML 模式不變，`--config` 不建立 gRPC 連線，也不送 usage。
- 開機先載入三種 plugin 目錄，再載入 `<state-dir>/snapshot.pb`（驗證 zone、長度與結尾 SHA-256）並立即服務，再背景連線；沒有可用快照時 `/v1/*` 與 `/readyz` 回 503，不會退回 open router。每次套用先持久化再 swap 再 ACK，重啟可回到最後 ACK 的版本。
- MCP／A2A 端點（`/mcp`、`/mcp/servers/*`、`/a2a`、`/a2a/*`）與 LLM 端點共用同一個 listener；它們只讀設定快照與 `LUMEWARP_GATEWAY_ID_KEY`／`LUMEWARP_GATEWAY_PUBLIC_URL`，不碰資料庫或 control plane。
- CP 模式一律要求 key；status／expiry／allowlist 與預算狀態照舊（401／403／429）。rpm、tpm、max_parallel 在本機判斷（rpm／tpm 為對齊牆鐘整分鐘的滑動窗口，max_parallel 為進行中請求數），設定 Redis 時跨 gateway 共享，429 帶 `Retry-After`。
- 用量：串流在上游支援時注入 `stream_options.include_usage`，客戶端沒要求時剝除多出的 usage chunk；事件經有界佇列（100k）每 1 秒或 500 筆批次送出，滿了就丟並計數；token／spend 指標與 `model.price` 成本換算已接上。MCP／A2A 的請求也產生 usage event（`kind`、`target`、`method`、`tool`、`status`、duration），但不計成本、不進預算。
- 指標：config stream 連線／版本／NACK code、usage 佇列與 capture 狀態、tokens／spend 皆已輸出；`/metrics` 仍在 admin listener。

**部署**

- `deploy/helm/lumewarp`：control plane 與 gateway 的 Deployment／Service、依 `LUMEWARP_GATEWAY_ZONE_<ID>_TOKEN` 環境契約提供各 zone 的同一個固定 secret、`/readyz`＋`/healthz` probes、gateway 多個 pod（同一 zone 共用 token、每條串流由 control plane 指派 gateway id；預設 Deployment＋每個 pod 的 emptyDir，開啟持久化時改為 StatefulSet 每 pod 一個 PVC；選用 PDB 與 HPA）、選用的 Valkey（`valkey.enabled`，供共享限流）、plugin 目錄的 volume 掛載（三個子目錄與 `LUMEWARP_PLUGIN_MEMORY_LIMIT_BYTES`、`LUMEWARP_GATEWAY_ID_KEY`、`LUMEWARP_GATEWAY_PUBLIC_URL`）、migration hook Job、可選 ServiceMonitor。部署環境的設定方式見 [Docker](./operations/docker.md)、[Helm](./operations/helm.md) 與 [Kubernetes](./operations/kubernetes.md)。
- `deploy/dev/compose.yaml` 提供本機 Postgres 16。

**已知限制**

- 預算為 eventual；佇列滿或 gateway 重啟會遺失用量事件，進行中的串流不會因超額被切斷；月窗口固定為 UTC 日曆月。org 超額由推導花費判定，並行批次尚未互相可見時最多延後到下一次 ingest 或 compile。共享限流是近似值：兩次同步之間每個 gateway 只能用剩餘額度的公平份額，超出約 n 個請求（gateway 首次加入某 scope 時再加一個同步間隔的流量）；Redis 不可用時退回每個 gateway 各算一份。
- MCP／A2A 的已知邊界：聚合端點的 `GET /mcp` 回 405；聚合端點的反向 request（elicitation 等）與反向回應只在同一個 POST SSE 串流上，取消／進度通知的路由是 process-local；session 與 cursor 靠 `LUMEWARP_GATEWAY_ID_KEY` 跨 gateway，換金鑰會讓未到期的 session 與 task id 失效。TOFU 在回報與快照傳播前可能讓兩台 gateway 短暫服務同一名稱的不同定義。A2A `ListTasks` 只支援 `passthrough` agent。plugin 沒有熱載入，換檔要重啟。

### 跨 gateway 共享限流與多個 gateway

- 一個行程就是一個 gateway process，服務一個 zone。Gateway 在 `Hello.hostname` 傳送 hostname；control plane 在該串流的第一個 `Welcome` 指派 `<hostname>-<8 hex>` 的 id。每條串流（包括 reconnect）都有新 id 和新 row；舊 row 斷線後保留 24 小時再刪除。token 代表一個 zone、不綁定 id，同 zone 任意數量的 gateway 可以共用。
- control plane 以 `(zone, gateway_id)` 管理 config stream 與 `gateway_sessions`。每個 gateway row 有自己的 ACK／pending／NACK、capability、plugin 回報與 shared-limits 顯示；`GET /api/v1/gateways` 一行一個 gateway，已連線的在前。Usage report 帶最近一次 Welcome 指派的 id；第一次 Welcome 前的 report 沒有 gateway 歸屬。
- 任一 gateway 以 `INVALID_CONFIG` NACK 某版本，該 zone 的所有 gateway 都隔離該版本（`gateway_quarantines`），之後才連上的 gateway 也一樣；已在該版本上的 gateway 會保留到下一版。`UNSUPPORTED_FEATURE`（缺 plugin／capability）只影響送出 NACK 的那個 gateway：其他 gateway 照常收到並套用該版本，缺功能的 gateway 留在前一版，之後每個仍需要該功能的版本都會再 NACK。capability 與 plugin 必須每個已連線 gateway 都回報才算可用；target 只要任一已連線 gateway 回報不健康就算不健康。
- 限流演算法：rpm、tpm 為對齊牆鐘整分鐘的滑動窗口計數（前一分鐘依剩餘比例加權＋本分鐘），tpm 仍在用量確定後才扣；max_parallel 為進行中請求數。每個計數分成「本機」與「其他 gateway」兩部分。
- 共享：設定 `LUMEWARP_SHARED_LIMITS_REDIS_URL` 時，背景工作每 `--shared-limits-sync-ms`（預設 100 ms，慢時自動加倍到 1 s）以一支 Lua 腳本把本機各 scope 每分鐘的絕對計數 `HSET` 到 Redis（重送不會重複計數）、寫入進行中請求數（以 Redis 的時間判斷 3 秒內是否新鮮），並讀回其他 gateway 的總和與參與的 gateway 數 n。兩次同步之間每個 gateway 最多用 `ceil(剩餘額度 ÷ n)`。key 以 `{lw:<namespace>}` 為 hash tag，namespace 在 control-plane 模式預設為 zone；Redis 使用每個 gateway process 的內部隨機 key，不使用 Welcome 指派的 gateway id，因此第一次 handshake 前即可共享。此 key 不顯示給 control plane 或 UI。請求路徑不做任何 I/O。
- Redis 不可用超過 1 秒時退回本機計數（每個 gateway 各算一份，與 LiteLLM 相同），`/readyz` 不受影響，恢復後自動回到共享；`lumewarp_shared_limits_fallback` 與告警 `LumewarpSharedLimitsFallback` 反映這個狀態。沒有「Redis 不可用就拒絕」的模式。
- Helm 可用 `valkey.enabled` 部署一個不持久化的 Valkey，或以 `gateway.sharedLimits.redis.existingSecret` 指向外部 Redis。每個 pod 是一個 gateway；`gateway.replicaCount` 或 autoscaling 開幾個 pod，Gateways 頁就顯示幾個 gateway。

## 權限、路由與可觀測性

權限模型是 org → team → member／service account，加上 SSO、可抽換的授權後端、路由韌性與可觀測性。設計內容以上文為準。

**權限與 SSO（control plane）**

- 模型：安裝層管理員 → org → team → member（admin／member）與 service account → API key。安裝層角色（`proxy_admin`／`admin`）存在 Postgres；org admin 來自 `org_admins`，team 管理權由 `RequestAuthz` 依 `team.org_id` 繼承；建立／封存 team、team 的 quota 與 team admin 指派都是 org 權限（安裝層管理員也可以）；org 本身的建立、政策、封存與 admin 指派是安裝層權限。全域 model grants 沿 org → team → owner（member exception）→ key 取交集；每個 zone 的限流、並行與 budget 則沿相同 owner hierarchy 分別計算，API key 綁定單一 zone。
- 授權：`Authorizer.check(user_id, relation, resource)`（只回答直接的 org／team 關係；繼承在 `RequestAuthz`）；內建 `builtin`、`openfga`（Check／BatchCheck／ListObjects／ReadChanges，`HIGHER_CONSISTENCY`，可改名對應）、`http`（選用 list／changes 端點，行為同 `list_objects`／`changes()`），全部經 `lumewarp.authz` entry point 載入。每次呼叫有逾時（`LUMEWARP_AUTHZ_TIMEOUT_SECONDS`），逾時即 `ERROR` → 管理 API 503。安裝層角色不經後端；共用情境測試 `authz/contract_tests.py`（builtin、http、live OpenFGA）。
- key reconciler：有 `changes()` 時每 2 秒輪詢、只重算變動的使用者，另每 5 分鐘全掃；以 key 擁有權判斷（直接成員、安裝層管理員或該 org 的 org admin），全部來源都明確 deny → `SUSPENDED`（`owner_removed`）並推新版本。恢復權限不自動復原；service account 的 key 不受影響。
- OIDC 登入、伺服器端 session 與 CSRF 見上文 SSO 一節；已有安裝層角色的帳號不能由新的 IdP 身分以 email 自動連結。
- 成員可用 email 加入；成員與 key 顯示擁有者是否仍啟用。

**路由（gateway + control plane）**

- 快照與 standalone YAML 支援 `retry`、`fallbacks`、`health_check`、`circuit_breaker`、`traffic_split`、`session_affinity`、`vllm_load`、`rotation_grace`、`encrypted_api_key`、`model_limits`、`default_body`、`reasoning_effort`、`router_reasoning_default`、`model_chat_check`、`model_types`、`audio_price`、`mcp`、`a2a` 與三種 plugin capability（`strategy:`、`auth:`、`a2a_middleware:`）；control plane 只在欄位有用到時列出對應 capability，驗證規則與 gateway 一致（含 URI 文法）。非 LLM model 需要 `model_types`，非零音訊價格需要 `audio_price`。fallback 只能是同型別的 model，需要至少 2 次 attempt 的 retry policy。
- 重試僅限回應送出前，串流僅限收到上游 headers 前；最多五次，
  共用原路由 timeout 與全域重試額度（預設 20%，低流量允許三次 burst；
  只有設定 `retry` 的路由請求會存入額度）。`Retry-After` 使用 model
  cooldown（上限 60 秒與 breaker `open_ms`），重試跳過冷卻中的 model。
  被路由 `total_timeout_ms` 截斷的 attempt 不算 model 失敗。
- model 熔斷狀態以名稱保存在 Runtime，跨快照保留；主動健康探測在背景執行，
  接受新快照後依 (upstream, URL, 設定, 憑證) 比對，只重啟有變動的探測。
  所有候選都異常或開路時仍嘗試一個，避免 fail closed；已進行中的
  half-open trial 不重複，改用其他候選。
- 每次 attempt 有獨立 OTel 子 span；請求指標與用量只記一次，歸屬實際產生
  回應的 attempt。回應的 `model` 見下節。
- `LOWEST_LATENCY`：每個 model 的回應標頭時間 EWMA，power of two choices。`VLLM_LOAD`：每秒背景抓取 vLLM `/metrics`（`num_requests_waiting`、KV cache 使用率），power of two choices；抓取失敗或超過 3 秒未更新就退回 in-flight 比較。
- traffic split：依權重選 split，再在 split 內依策略選 model；sticky 依 header／team／key 的固定種子雜湊。重試先留在同一 split，再換其他 split，最後才走 fallback。
- Bench A6（quick ×3，1000 RPS，含認證）：非串流 overhead p99 −0.07 ms（雜訊範圍），串流 TTFT overhead p99 3.3 ms；LiteLLM 同機飽和。
  路由功能完成時在 main 跑 full ×3（含認證）：1000 RPS 非串流 overhead p99 0.20 ms
  （A6 通過），300 RPS 0.28 ms；10000 RPS 時 lumewarp 與 direct 都達到目標速率。
  串流 TTFT overhead p99：並行 100 為 −1.9 ms（雜訊），並行 1000 為 14.5 ms。
  LiteLLM 在 1000 RPS 以上達不到目標速率。

**可觀測性**

- gateway 新指標見上表；control plane 在獨立 listener 輸出 `lumewarp_cp_*`。
- 27 條告警規則（另有 4 條 recording rule）與 promtool 測試、五張 dashboard、Helm 可選 PrometheusRule／dashboard ConfigMap／兩個 ServiceMonitor。

**驗收**

- `scripts/e2e-identity.sh`：builtin 與 openfga 各跑一次 OIDC 登入、建 key、呼叫、用量對帳、撤權（實測 builtin 0.2 s、openfga 1.1 s 內回 401）、IAM 故障不誤停；另含路由 e2e（重試、fallback、熔斷、URI 契約）。
- `scripts/e2e-core.sh`（含路由 e2e）、`scripts/load-e2e.sh` 8/8。
- `scripts/e2e-mcp.sh`：獨立與 CP-backed 兩段。聚合與單一 server 端點、tool grant、rug-pull 後待審與核准 2 秒內恢復、`passthrough`／`static_bearer`／`hmac-signature` 的出站 header、`auth` plugin timeout（504）、usage `kind=mcp`、DB 無明文憑證。
- `scripts/e2e-a2a.sh`：card 改寫與 `strip-artifacts` SSE、以他人 key 的 task id 或竄改 MAC 被拒、push 設定被拒、middleware timeout（504）、usage `kind=a2a`、沒有 `LUMEWARP_GATEWAY_ID_KEY` 時 503。
- `ui/` Playwright 涵蓋 MCP servers、A2A agents 與 grant 編輯。

## 模型目錄已實作

目錄的核心是 model、alias、router。以下先列現況，後面是已定的決定、實作取捨與尚未做的項目。

- **目錄與 API：** `/upstreams`、`/models`、`/models/{id}/deployments/{zone}`、`/aliases`、`/routers`、`/plugins?kind=router-strategy`、`/catalog`。Catalog 名字跨種類全域唯一（`catalog_names`），參照與 grants 都用 id，改名不弄斷關係；舊名字立刻 404，只有 model 可以 `keep_old_name_as_alias`。Upstream、MCP server、A2A agent 與 model deployment 屬於 zone；同名 upstream/target 可分別存在不同 zones。Model 定義、alias、router 與 model group 是全域資源。Model 可在每個 zone 有一個 deployment，指定同 zone upstream、`upstream_model` 和 probe 設定；未部署的 model 不進該 zone snapshot，router member/split/fallback 會以 not-available 警告略過，空 router 和不可用 target 的 alias 也略過。Rollback 是安裝層全域 catalog restore，再為每個 live zone 各自編譯。
- **Model group：** 一組 model、alias、router 的命名集合，當成一個單位授權（`policy_group_grants`，欄位 `allowed_groups`，任何一層都可以授權，包括 key）；group 本身不能被呼叫，也不能包含別的 group。`/model-groups` 任何登入者可讀，寫入需安裝層管理員；改成員立刻影響所有持有者（不重新驗證，上層不允許的成員在該持有者的有效集合中被濾掉）；刪除會移除並稽核每一筆授權。org 與 team 回傳 `effective_models`，Admin UI 的選單以上一層的有效模型為範圍，包含任何超出範圍成員的 group 顯示為 disabled。Rollback 不還原 group。
- **快照：** 每個 zone 的 `Snapshot` 帶該 zone 的 upstreams、model deployments（編譯成可服務的 models）、aliases、routers、MCP servers、A2A agents 與 keys，gateway 在套用時建成名字表，請求路徑仍是一次查表。`Router.strategy` 是 built-in 或 WASM plugin 的名字。`ApiKey.allowed_models` 由 control plane 依全域 grants 和該 zone 可用目錄展開。
- **Rollback：** 由某 zone 的版本啟動安裝層全域 rollback，依 id 還原該版本 capture 的 catalog（upstream、model、model deployments、alias、router、MCP server／A2A agent），然後用每個 zone 自己的資源編譯並發布新版本；不在 `catalog_state` 的 MCP server／A2A agent 會封存而非刪除。授權、API keys 與 policies 不還原，rollback 不重審 MCP 定義。
- **回應改寫：** 不緩衝的掃描器（`rewrite.rs`）把 JSON 與一般 SSE 最外層的 `model`，以及 Responses SSE 的 `response.model`，換成呼叫的名字，大小不設上限。呼叫的名字等於 `upstream_model` 時走快速路徑。需要改寫時移除送往上游的 `Accept-Encoding`；上游仍壓縮就原樣轉發並累加 metric。契約見 [config protocol](config-protocol.md)。
- **Usage：** 每筆 event 保存由已驗證 key 決定的 `zone`、`requested`、`model`、`alias`、`router`、`upstream`、`upstream_model`、`fallback`、`attempts`、`endpoint`、`audio_ms`，加上 control plane 解析的 `model_id`、`alias_id`、`router_id`。`usage_totals` 與 org budget windows 都以 zone 分開。`GET /usage` 可用 `zone` 篩選（省略則跨 zones），也可 `group_by=zone`；`tz`（IANA 時區，預設 UTC）決定 `day` 與 `hour` 的分桶，`hour` 以事件自身的 offset 取整點、範圍最多 31 天；其他維度與篩選如下：`hour`、`day`、`requested`、`model`、`upstream_model`、`alias`、`router`、`team`、`owner`、`key`、`endpoint`、`model_type`。型別由 endpoint 推導，不另外儲存在事件；彙總列加總 `audio_ms` 與成本。`upstream`、`fallback`、`attempts` 只能從資料列讀。改名前後的用量連續。
- **策略：** plugin 的 `register!` 宣告 kind、params 與成員欄位，schema 從 struct 產生，在 `Hello.plugins` 回報，存進 `gateway_plugins`；寫入時依 schema 驗證，內建 plugin 的 descriptor 有 commit 的 manifest（`compiler/builtin_plugins.json`）。見 [router strategies](router-strategies.md) 與 [gateway plugins](plugins.md)。
- **Request log 與 OTel：** `GET /requests` 有一列 metadata（名字鏈、status、outcome、attempts、tokens、cost、duration、key／owner、Playground user），不含 prompt／response 與每次 attempt 的細節。回應帶 `x-lumewarp-request-id`，等於 usage、request log 與 span 的 `request_id`。`request` span 帶呼叫的名字、alias、router、最後的 model 與 attempts，每次嘗試一個 `upstream_attempt`。前提：沒設 OTLP endpoint 就沒有紀錄；取樣低於 100% 或 exporter 佇列滿了會查不到，要留下每個請求的細節建議 100%；保留多久由 OTel 後端決定。Sampler 不聽從用戶端 `traceparent` 的 sampled flag。Helm 的設定是 `gateway.otel.endpoint`、`gateway.otel.samplingRatio`。
- **Model 設定：** `max_input_tokens` 只供顯示；`max_output_tokens` 只適用 LLM：gateway 檢查 chat 的 `max_tokens`／`max_completion_tokens`、completions 的 `max_tokens`、responses 的 `max_output_tokens`，超過回 400。`default_body` 只適用 JSON 端點，transcription 不允許設定；它是合併在請求之下的 JSON 物件：用戶端有的欄位優先，兩邊都是物件時遞迴合併。`reasoning` 只適用 LLM，處理 chat／completions 的 `reasoning_effort` 或 responses 的 `reasoning.effort`（等級名稱可自訂，符合 `^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$`；mapped 可設 1–16 個等級）：`passthrough` 原樣轉發（不檢查值），`strip` 直接移除，`mapped` 換成該等級對應的 JSON 片段（例如 vLLM 的 `chat_template_kwargs.enable_thinking`），沒有對應的等級回 400；`default_level` 在用戶端沒送時套用。Router 另有 `reasoning_default_level`：用戶端沒送時由 router 注入，在 serving member 自己的 transform 之前，成員、split 成員與 fallback 都適用，alias 繼承，用戶端送的值優先；可接受的等級是這些成員等級的交集（passthrough／strip 接受任何等級，mapped 只接受自己的片段鍵），寫入時檢查，model 的 reasoning 或 router 的成員／split／fallback 變更若會把某個 router 的預設等級排除在交集外就回 422 並指出該 router；compiler 對壞掉或已不被全員接受的既有值只警告並略過。優先順序 default_body < reasoning 片段 < 用戶端（router 的預設等級屬於「用戶端沒送」的情況）；`model`、`stream`、`stream_options` 不能設定，`default_body` 也不能設 `reasoning_effort`（改用 `default_level`）；巢狀最多 32 層。只解析最外層，沒動到的值保留原 bytes；沒有設定的 model 完全不經過這段（`transform.rs`）。稽核只記 `default_body_changed`／`reasoning_changed`，不記內容。
- **Metrics：** 標籤 `model` 是實際服務的 model，`requested` 是呼叫的名字，`endpoint` 是 API 路徑。改名後 label 換成新名字，舊名字的時間序列就此結束，這是 Prometheus 的正常行為。
- **驗收：** `tests/e2e/test_catalog.py` 與 `tests/e2e/test_model_types.py`（真實 control plane + gateway），涵蓋所有型別端點、型別拒絕、typed probes、SSE 名字改寫與 token／音訊用量、成本彙總。

**尚未包含：** 在 Models／Routers 顯示「已生效／被 NACK」、`api_key_ref` 錯誤的處理、成員自己 usage 列上的 model／`upstream_model` provenance 是否要再收窄、dropped spans 的 metric（span 遺失目前是無聲的）。

### 已定的決定（Andy，2026-11-02）

1. **名字全域唯一，部署 per zone。** Model 是全域定義，可在每個 zone 各有一個 deployment，綁定該 zone 的 upstream 與 upstream_model；同一模型可以在多區使用不同 upstream，或只部署於部分 zones。Model、alias、router 共用同一個名字空間，要負載平衡一律建 router。
2. **名字可以改。**
3. **Alias 可指向 model 或 router，只有一層，獨立授權。** 有 alias 權限就能呼叫，不需要目標的權限；有目標的權限也不會自動得到 alias。
4. **Router 底下只有 model 名字。** 成員只能是 model，不能放 alias 或其他 router。Fallback 目標也只能是 model，而且可以不設。
5. **權重放在 router 的成員上。** 同一個 model 在不同 router 可以有不同權重。Model 上不再有 weight。
6. **Retry、fallback、traffic split、strategy 都只在 router 上。** 直接呼叫 model 就是一次嘗試。Health check 和熔斷的設定仍在 upstream 上，熔斷狀態依 model 分開。需要重試就建單一成員的 router。
7. **權限依呼叫的名字檢查。** Key 能呼叫 `qwen-coder`，不代表能直接呼叫 `lumewarp/qwen3-a`，直接呼叫 model 要有該 model 的權限。Router 的 fallback 不重新檢查權限，和現在一樣。
8. **權限與配額分層。** Global model grants 走 org → team → member／service account → key 並逐層取交集；rate limit、max_parallel、budget 與 team member defaults 則在每個 zone 分別沿同一 owner hierarchy 計算。每把 API key 綁定一個 zone，key 自己可有額外限額（最多四個 rate-limit scope）。
9. **被參照的名字不能刪。** 被 router、alias 或 fallback 參照時回 409，並列出參照者。
10. **回應的 `model` 一律改寫成呼叫時的名字。** Control plane UI 要能同時用對外名字和上游模型名篩選。
11. **內部命名全部一起改。** 目前沒有遷移成本，不保留舊詞。
12. **Team 設成 All 就包含之後新增的名字。** 需要限制的 team 改用 Selected。
13. **Usage 的 `model` 記最後實際服務的 model，另外記錄來源的 alias 和 router。**
14. **Router 的設定由 plugin 的 manifest 定義。** 內建和自訂策略走同一套宣告，UI 依策略宣告的 schema 產生表單。策略只定義自己的設定，成員清單、retry、fallback、traffic split 由 gateway 統一提供。
15. **Gateway 不寫 access log；request log 只存 usage metadata，單一請求的細節到 OTel 找。**
16. **只有 model 改名有「Keep old name as alias」。** 預設勾選；router 和 alias 沒有。
17. **有 gateway 沒有某個 plugin 時，預設不能儲存。** API 回 422 並列出缺少的 gateway，帶 `force` 才能存。
18. **路由細節只給安裝層管理員。** `/catalog` 的名字與種類任何已登入者都能讀，因為 team admin 設定 allowlist 只需要名字；`/upstreams`、`/models`、`/aliases`、`/routers`、`/plugins`、`/mcp-servers`、`/a2a-agents` 的細節需要安裝層權限。Team member 仍可從自己的 usage 看到實際服務的 model 與 `upstream_model` 名稱，那是用量紀錄，不是路由設定。回應改寫只影響 API 用戶端看到的名字，不算保密。

- **價格放在 model 上。** 費用永遠依實際服務的 model 計算。
- **權限清單驗證名字存在。** Member / service account 的清單必須是 team 範圍的子集，UI 也只列 team 範圍內的名字。
- **Team 的全域 model grant 縮小時，** 不改 member 的 grants，實際權限仍取交集（現有 `narrow` 的行為）；各 zone 的 limits/budget 另行設定。
- **刪除名字時，** 指向它的授權一起刪除並寫入 audit；被 router、alias 或 fallback 參照則依第 9 點回 409。

### 實作取捨

- **讀 body 前被拒絕的請求，`requested` 是空的。** 403、404、429（限流）在讀完 body 之後才決定，有值；已撤銷、停用、過期和超過預算的 key 在讀 body 之前就被拒絕，那時還不知道名字。要補就得為了被拒絕的請求讀 body，所以維持空值。
- **快速路徑可能回傳上游更具體的名字。** 呼叫的名字等於 `upstream_model` 時不走掃描器，例如呼叫 `gpt-4o`，OpenAI 可能回 `gpt-4o-2024-08-06`。這是第 10 點「一律改寫」之外的例外，另外兩個例外是錯誤回應和上游仍然壓縮的回應。
- **Binary 叫 `lumewarp`，** 指令是 `lumewarp plugins --json`。
- **解析 usage 名字有寬限時間。** 依請求開始時間（`started_at`，舊 gateway 用完成時間），查不到精確的區間時，寬限時間內只有一個同種類的擁有者才補 id。預設 300 秒，`LUMEWARP_USAGE_CATALOG_GRACE_SECONDS`，0 是關閉。這容忍時鐘偏差和傳播延遲，不是身分證明：同種類舊名字在寬限時間內被重複使用時，落後的 gateway 仍可能算到新擁有者。
- **非串流回應超過 4 MiB 時，usage 是 `INVALID`。** 回應本身正常轉發和改寫，只是 usage 的 capture 上限 `MAX_JSON_CAPTURE` 讀不到 token。這個上限原本就有；需要改寫的回應仍會送達，所以比較容易遇到。
- **沒有 gateway 回報的 plugin 也算 `config_invalid`。** 已存的 router 設定若在升級後不再符合任何已回報的 schema，會顯示 `Config invalid`，資料不會被修改。
- **內建策略拒絕成員 params。** 填了成員 params 會被拒絕：API 回 422、gateway NACK，因為空 schema 是 `additionalProperties: false`。已存的這種資料會顯示 `config_invalid`。
- **`Configure` trait。** `configure` 透過新的 `Configure<P, M>` trait 收到已解析的 struct，讓驗證的型別和讀取的型別是同一份；`Strategy` 只剩 `pick`。

### 尚未做

- 註冊時的連線測試（確認 upstream 連得上、`upstream_model` 存在）。
- 在 Models、Routers 顯示「已生效／被 NACK」。
- `api_key_ref` 錯誤造成 gateway 收不到新設定的問題。這項尚未實測，應該獨立設計。
- Token 月配額。
- 跨 replica 的 probe broker：已存的 Test now probe 只送到與服務該請求的 control-plane process 有連線的 gateway；其他 replica 上的合格 gateway 不會被選到，會回 409。
- Rollback 不重新驗證 LLM upstream 的 auth plugin：`catalog_state` 會還原欄位，但 `check_stored_targets` 只檢查 MCP server／A2A agent；缺 plugin 的 gateway 以 `UNSUPPORTED_FEATURE` NACK 該版本，而不是 rollback 被拒或寫 `upstream.force_save`。

## 效能目標

| 指標 | 目標 |
| --- | --- |
| gateway 額外延遲 p50（不含上游） | < 100 µs |
| gateway 額外延遲 p99 | < 1 ms |
| 單一 4 vCPU 實例 | ≥ 10,000 RPS（非串流） |
| 同時串流連線 | ≥ 50,000，記憶體 < 2 GB |
| 設定生效時間 | < 2 s |

驗證：固定延遲、可模擬 SSE 的 mock 上游；同機對 LiteLLM 與 Lumewarp 跑 1k / 5k / 10k RPS（oha 或 k6），比較額外延遲、TTFT、CPU、記憶體，結果進 CI 追蹤退化。`bench/` 已量到 gateway 額外延遲 p99 與 10,000 RPS（見上文「路由」與 `bench/README.md`）；p50、50,000 條同時串流與設定生效時間尚未量測。

## Roadmap

MCP 與 A2A 代理、工具級 grant、定義審核（TOFU／manual／off）、Agent Card 改寫、A2A middleware 與三種 WASM plugin 已實作。刻意延後、這一期不做的：

- MCP resources、subscriptions 與 `completion`；聚合端點的 `GET` 串流（回 405）與跨 gateway 的取消／進度路由；人工核准高風險 tool；stdio sidecar（由部署方自行包裝）；MCP OAuth resource server 與代管上游使用者 token。
- A2A gRPC／REST binding、push notification、Agent Card 簽章、上游使用者 token 代管；非 `passthrough` agent 的 `ListTasks`。
- plugin 熱載入、plugin 簽章／來源驗證、跨 gateway 的 plugin 一致性檢查；plugin 沒有延遲預算，只有設定的 timeout。
- MCP／A2A 的限流與預算（只有 `tools/call` 計入 key 的 RPM）。
- 把 alert 轉發給 Slack 等其他 receiver（Lumewarp 只接收 Alertmanager 的 webhook）；Langfuse 整合。

其他尚未實作的項目：

- JWT／mTLS 呼叫 gateway、SCIM、IdP→IAM 人員同步、讀 IdP claims 做 team 授權、比這五個更細的角色。
- Redis Cluster／Sentinel、ClickHouse、設定版本 canary、cache 親和、依 header／token 數條件分流、逐請求外部 hook、LoRA 自動探索。
- control plane 的 OTLP metrics／traces（Prometheus `lumewarp_cp_*` 已有）；gateway 只有選用的 OTLP traces，沒有 metrics exporter。
- `gateway/Dockerfile`、`control-plane/Dockerfile` 可建置映像，但尚未有發佈映像的 CI；Helm chart 需自行指定 repository/tag。
