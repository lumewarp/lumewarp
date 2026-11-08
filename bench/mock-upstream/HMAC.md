# HMAC request signature (`x-lumewarp-signature`)

The scheme the gateway's `hmac-signature` upstream-auth plugin will produce and the mock
MCP server / A2A agent verify. It signs each HTTP request the gateway sends upstream with a
shared secret, so the mock can prove that the gateway authenticated the call itself rather
than forwarding a client token.

The mock implements this in `src/auth.rs` (`AuthMode::Hmac`); tests cover a good signature,
a wrong secret, a stale timestamp, a wrong path and a missing header.

## Header

```http
x-lumewarp-signature: t=<unix-ms>,v1=<hex>
```

| Part | Meaning |
| - | - |
| `t` | Unix time in **milliseconds**, decimal, no sign, UTC. |
| `v1` | Lowercase hex HMAC-SHA256 of the canonical string below. |

One `t` and at least one `v1` are required. Parts are `key=value`, comma separated; unknown
keys are ignored. The header is case-insensitive on the wire; values are not.

## Canonical string

```text
<unix-ms> "." <HTTP method> "." <request path> "." <sha256hex(body)>
```

- `<HTTP method>` is uppercase, exactly as sent: `POST`, `GET`, `DELETE`.
- `<request path>` is the request target **without** the query string (`/mcp`, `/a2a`),
  taken byte for byte — no percent-decoding, no trailing-slash normalisation.
- `<sha256hex(body)>` is the lowercase hex SHA-256 of the **exact bytes sent as the request
  body**. A request without a body signs the SHA-256 of the empty string.
- The fields are joined with a single ASCII `.` and the whole string is MACed as UTF-8.
- HMAC-SHA256 key = the shared secret's raw UTF-8 bytes. The resulting 32 bytes are
  lower-case hex.

Only the method, path and body are covered. Headers (including `Authorization`,
`Mcp-Session-Id` and `MCP-Protocol-Version`) are not; the signature proves possession of the
secret, not integrity of headers. The gateway must sign exactly the bytes it sends: after
signing, the body must not be re-serialized, and the request must not be redirected to a
different path.

## Verification

The server (`--auth hmac --hmac-secret <SECRET>`) checks, in order:

1. Header present → otherwise `401` with `"reason": "missing_signature"`.
2. `t` and `v1` parse as described → otherwise `"malformed_signature"`.
3. `|server_time - t| <= --hmac-skew-ms` (default **300000** ms = 5 minutes) → otherwise
   `"signature_stale"`. The window bounds replay as well as clock skew; a future timestamp
   beyond the window is stale too.
4. Any `v1` value equals the recomputed MAC (constant-time comparison) → otherwise
   `"signature_mismatch"`.

Failures answer `401 Unauthorized` with a JSON body and a challenge header:

```http
WWW-Authenticate: HmacSignature realm="mock"

{"error":"unauthorized","reason":"signature_mismatch","detail":"the HMAC does not match the request"}
```

The control endpoints (`/_mock/*`) are loopback-only and do **not** require a signature; the
agent card (`GET /.well-known/agent-card.json`) and `GET /health` on the A2A agent are public
as discovery requires.

## Worked example

Secret `lumewarp-dev-secret`, request:

```http
POST /mcp HTTP/1.1
Content-Type: application/json

{"jsonrpc":"2.0","id":1,"method":"ping"}
```

With `t = 1762500000000`:

```text
sha256hex(body) = 98e0961a7c1232f08d2f2187d13c4a1a22a0641e00e5dec0eca645d646077fab
canonical       = 1762500000000.POST./mcp.98e0961a7c1232f08d2f2187d13c4a1a22a0641e00e5dec0eca645d646077fab
v1              = b2f1c5fd7baf3e518a7ddcfaecda5e8fc402fe70c5e144464c6803bd8ce6d47b
```

```http
x-lumewarp-signature: t=1762500000000,v1=b2f1c5fd7baf3e518a7ddcfaecda5e8fc402fe70c5e144464c6803bd8ce6d47b
```

That timestamp is illustrative; a live request needs a `t` inside the skew window.

Verify it by hand:

```sh
SECRET=lumewarp-dev-secret
BODY='{"jsonrpc":"2.0","id":1,"method":"ping"}'
T=$(date +%s%3N)
HASH=$(printf '%s' "$BODY" | sha256sum | cut -d' ' -f1)
SIG=$(printf '%s.%s.%s.%s' "$T" POST /mcp "$HASH" | openssl dgst -sha256 -hmac "$SECRET" -r | cut -d' ' -f1)
curl -sS -H "content-type: application/json" \
  -H "x-lumewarp-signature: t=$T,v1=$SIG" \
  --data-binary "$BODY" http://127.0.0.1:9110/mcp
```

## Gateway plugin notes

- Settings are the secret (from `LUMEWARP_PLUGIN_SECRET_HMAC_SIGNATURE_KEY` through
  `secret()`, never in plugin params) and the path, which is the configured upstream URL's
  path.
- Use `now-unix-ms()` for `t`; the result depends on the request body, so `cache-ms` must be
  `0` (a cached signature would only be valid for the same body).
- Sign the body before any framing: no compression or re-serialization after signing.
- This is a signing scheme, not encryption: the secret must be shared out of band and
  rotated by changing the environment variable and the mock's `--hmac-secret` together.
