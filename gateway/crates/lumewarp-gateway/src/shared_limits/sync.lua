-- Push this gateway's absolute per-minute counts and return the view of the
-- other gateways.
--
-- KEYS: five per scope, in order: rpm previous-minute hash, rpm current-minute
--       hash, tpm previous-minute hash, tpm current-minute hash, in-flight hash.
-- ARGV: gateway id, scope count, then nine per scope:
--       write (1/0), refresh_ttl (1/0), use_rpm (1/0), use_tpm (1/0),
--       rpm_prev, rpm_cur, tpm_prev, tpm_cur, in_flight.
--
-- A dimension the scope has no limit in is not read or written; its returned
-- counts are zero, which is what apply_sync expects for that dimension.
--
-- The counters are absolute counts, not deltas: HSET overwrites this
-- gateway's field, so retrying a batch whose reply was lost can never
-- double-count. `write` is 0 for a read-only entry (the scope only needs a
-- fresh view). A field (or key) created by a write always gets its TTL
-- immediately; the caller additionally asks for a periodic refresh at most
-- every five seconds. A zero count is an HDEL, never a zero-valued field.
--
-- `in_flight` is always written (even 0) for every scope in the batch, so the
-- gateway count covers every live gateway that knows the scope. Stamps and
-- the 3 s staleness check use Redis's own clock, not the gateway's. A crashed
-- gateway's slots free within that window.
--
-- Returns eight integers per scope: others' rpm_prev, rpm_cur, tpm_prev,
-- tpm_cur, rpm_gateways, tpm_gateways, others' in_flight,
-- in_flight_gateways.

local gateway = ARGV[1]
local scopes = tonumber(ARGV[2])
local out = {}
local arg = 3

local clock = redis.call('TIME')
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)

-- One HGETALL per hash: the other gateways' sum and the set of gateways
-- with a positive count.
local function scan(hash)
  local total = 0
  local active = {}
  local fields = redis.call('HGETALL', hash)
  for i = 1, #fields, 2 do
    local field = fields[i]
    local value = tonumber(fields[i + 1])
    if value and value > 0 then
      active[field] = true
      if field ~= gateway then
        total = total + value
      end
    end
  end
  return total, active
end

local function union_count(left, right)
  local count = 0
  for field in pairs(left) do
    count = count + 1
  end
  for field in pairs(right) do
    if not left[field] then
      count = count + 1
    end
  end
  return count
end

-- HSET returns 1 when it created the field (and possibly the key); a key
-- created here must never live without an expiry.
local function set_count(hash, value)
  if value == 0 then
    redis.call('HDEL', hash, gateway)
  elseif redis.call('HSET', hash, gateway, value) == 1 then
    redis.call('PEXPIRE', hash, 150000)
  end
end

for s = 0, scopes - 1 do
  local k = s * 5
  local r_prev = KEYS[k + 1]
  local r_cur = KEYS[k + 2]
  local t_prev = KEYS[k + 3]
  local t_cur = KEYS[k + 4]
  local parallel = KEYS[k + 5]

  local write = tonumber(ARGV[arg]); arg = arg + 1
  local refresh_ttl = tonumber(ARGV[arg]); arg = arg + 1
  local use_rpm = tonumber(ARGV[arg]); arg = arg + 1
  local use_tpm = tonumber(ARGV[arg]); arg = arg + 1
  local rpm_prev = tonumber(ARGV[arg]); arg = arg + 1
  local rpm_cur = tonumber(ARGV[arg]); arg = arg + 1
  local tpm_prev = tonumber(ARGV[arg]); arg = arg + 1
  local tpm_cur = tonumber(ARGV[arg]); arg = arg + 1
  local in_flight = tonumber(ARGV[arg]); arg = arg + 1

  if write == 1 then
    if use_rpm == 1 then
      set_count(r_prev, rpm_prev)
      set_count(r_cur, rpm_cur)
    end
    if use_tpm == 1 then
      set_count(t_prev, tpm_prev)
      set_count(t_cur, tpm_cur)
    end
  end
  redis.call('HSET', parallel, gateway, tostring(in_flight) .. ':' .. tostring(now_ms))
  -- A live gateway writes its in-flight field on every sync; keeping the
  -- hash alive this way means one slow refresh can never make every
  -- gateway's slots disappear for an interval.
  redis.call('PEXPIRE', parallel, 10000)

  if refresh_ttl == 1 then
    redis.call('PEXPIRE', r_prev, 150000)
    redis.call('PEXPIRE', r_cur, 150000)
    redis.call('PEXPIRE', t_prev, 150000)
    redis.call('PEXPIRE', t_cur, 150000)
  end

  local others_parallel = 0
  local gateways = 0
  local fields = redis.call('HGETALL', parallel)
  for i = 1, #fields, 2 do
    local field = fields[i]
    local value = fields[i + 1]
    local colon = string.find(value, ':', 1, true)
    local n = nil
    local stamped = nil
    if colon then
      n = tonumber(string.sub(value, 1, colon - 1))
      stamped = tonumber(string.sub(value, colon + 1))
    end
    if not n or not stamped or (now_ms - stamped) > 3000 then
      redis.call('HDEL', parallel, field)
    else
      if field ~= gateway then
        others_parallel = others_parallel + n
      end
      gateways = gateways + 1
    end
  end

  local rpm_prev_total, rpm_cur_total, rpm_gateways = 0, 0, 0
  if use_rpm == 1 then
    local prev_active, cur_active
    rpm_prev_total, prev_active = scan(r_prev)
    rpm_cur_total, cur_active = scan(r_cur)
    rpm_gateways = union_count(prev_active, cur_active)
  end
  local tpm_prev_total, tpm_cur_total, tpm_gateways = 0, 0, 0
  if use_tpm == 1 then
    local prev_active, cur_active
    tpm_prev_total, prev_active = scan(t_prev)
    tpm_cur_total, cur_active = scan(t_cur)
    tpm_gateways = union_count(prev_active, cur_active)
  end

  out[#out + 1] = rpm_prev_total
  out[#out + 1] = rpm_cur_total
  out[#out + 1] = tpm_prev_total
  out[#out + 1] = tpm_cur_total
  out[#out + 1] = rpm_gateways
  out[#out + 1] = tpm_gateways
  out[#out + 1] = others_parallel
  out[#out + 1] = gateways
end

return out
