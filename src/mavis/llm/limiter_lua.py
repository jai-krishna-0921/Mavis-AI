"""Lua for the shared limiter. A line-for-line mirror of mavis.llm.policy (the policy lives there; the parity
test in tests/llm/test_limiter_parity.py drives both with the same scenarios). Every key besides KEYS starts
with the ARGV prefix `mavis:llm:<provider>:` (single Redis, no cluster)."""

# KEYS[1] holders zset (member = lease id, score = expiry ms), KEYS[2] queue zset (member = waiter id,
#       score = enqueue ms), KEYS[3] last_used (ms of the last non-best_effort start or finish).
# ARGV: now_ms, waiter_id, uid, rank (0 interactive, 1 background, 2 best_effort), since_ms, slots, bg_max,
#       user_max, hold_ttl_ms, prefix, waiter_ttl_ms, share, grace_ms, quiet_ms, bg_aging_ms, be_aging_ms
# Returns {code, queue length}: code 1 granted, 0 keep waiting, 2 yield (single slot, other work is active:
# fail fast, the caller's job layer retries).
TRY_ACQUIRE = r"""
local now = tonumber(ARGV[1]); local me = ARGV[2]; local uid = ARGV[3]; local rank = tonumber(ARGV[4])
local since = tonumber(ARGV[5]); local size = tonumber(ARGV[6]); local bg_max = tonumber(ARGV[7])
local user_max = tonumber(ARGV[8]); local ttl = tonumber(ARGV[9]); local p = ARGV[10]
local wttl = tonumber(ARGV[11]); local share = tonumber(ARGV[12]); local grace = tonumber(ARGV[13])
local quiet = tonumber(ARGV[14]); local bg_aging = tonumber(ARGV[15]); local be_aging = tonumber(ARGV[16])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
redis.call('ZADD', KEYS[2], 'NX', since, me)
redis.call('HSET', p .. 'waiter:' .. me, 'uid', uid, 'rank', rank, 'since', since)
redis.call('PEXPIRE', p .. 'waiter:' .. me, wttl)

local holders = redis.call('ZRANGE', KEYS[1], 0, -1)
local held = #holders
local lane = {0, 0, 0}
local per_user = {}
for _, h in ipairs(holders) do
  local m = redis.call('HMGET', p .. 'holder:' .. h, 'uid', 'rank')
  if m[1] then
    per_user[m[1]] = (per_user[m[1]] or 0) + 1
    local r = tonumber(m[2]) + 1
    lane[r] = lane[r] + 1
  end
end
local free = size - held
local be_inflight = lane[3]
local last_used = tonumber(redis.call('GET', KEYS[3]) or '')
local since_last = 1e18
if last_used then since_last = now - last_used end

local ids = redis.call('ZRANGE', KEYS[2], 0, -1)
local live = {}
local higher_waiting = false
for _, w in ipairs(ids) do
  local m = redis.call('HMGET', p .. 'waiter:' .. w, 'uid', 'rank', 'since')
  if not m[1] then
    redis.call('ZREM', KEYS[2], w)
  else
    live[#live + 1] = {id = w, uid = m[1], rank = tonumber(m[2]), since = tonumber(m[3])}
    if tonumber(m[2]) < 2 then higher_waiting = true end
  end
end
local qlen = #live

-- policy.work_active / single_slot_blocked
local work_active = higher_waiting or since_last < grace
if size == 1 and rank == 2 and work_active then
  redis.call('ZREM', KEYS[2], me); redis.call('DEL', p .. 'waiter:' .. me)
  return {2, qlen - 1}
end

-- policy.best_effort_may_start
local function be_may_start()
  local others = size - free - be_inflight
  local cap = math.max(1, math.floor(size / share))
  if others <= 0 then cap = math.max(cap, size - 1) end
  if free <= 0 or be_inflight >= cap then return false end
  if size == 1 then return not work_active end
  local q = since_last >= quiet and not higher_waiting
  return free >= 2 and others == 0 and q
end

if free <= 0 then return {0, qlen} end
local be_ok = be_may_start()
local contested = qlen > 1
local best = nil
local best_key = nil
for _, w in ipairs(live) do
  -- policy.effective_rank
  local r = w.rank
  if r == 1 and now - w.since >= bg_aging then r = 0
  elseif r == 2 and now - w.since >= be_aging then r = 1 end
  local ok = true
  if w.rank == 2 then ok = be_ok end
  if w.rank == 1 and lane[2] >= bg_max then ok = false end
  if w.rank < 2 and contested and (per_user[w.uid] or 0) >= user_max then ok = false end
  if ok then
    local k2 = per_user[w.uid] or 0
    if best == nil or r < best_key[1]
       or (r == best_key[1] and (k2 < best_key[2] or (k2 == best_key[2] and w.since < best_key[3]))) then
      best, best_key = w.id, {r, k2, w.since}
    end
  end
end
if best ~= me then return {0, qlen} end
redis.call('ZREM', KEYS[2], me)
redis.call('DEL', p .. 'waiter:' .. me)
redis.call('ZADD', KEYS[1], now + ttl, me)
redis.call('HSET', p .. 'holder:' .. me, 'uid', uid, 'rank', rank)
redis.call('PEXPIRE', p .. 'holder:' .. me, ttl)
if rank < 2 then redis.call('SET', KEYS[3], now, 'PX', 600000) end
return {1, qlen - 1}
"""

# KEYS[1] holders, KEYS[2] queue; ARGV: lease id, prefix. Wakes the oldest waiter early (they also poll).
RELEASE = r"""
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('DEL', ARGV[2] .. 'holder:' .. ARGV[1])
local w = redis.call('ZRANGE', KEYS[2], 0, 0)
if w[1] then
  redis.call('RPUSH', ARGV[2] .. 'wake:' .. w[1], '1')
  redis.call('PEXPIRE', ARGV[2] .. 'wake:' .. w[1], 5000)
end
return 1
"""

# KEYS[1] queue; ARGV: waiter id, prefix. Drops a waiter that gave up.
CANCEL = r"""
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('DEL', ARGV[2] .. 'waiter:' .. ARGV[1])
redis.call('DEL', ARGV[2] .. 'wake:' .. ARGV[1])
return 1
"""
