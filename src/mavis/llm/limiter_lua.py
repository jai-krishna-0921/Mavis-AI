"""Lua for the shared limiter. Every key besides the KEYS below starts with the ARGV prefix
`mavis:llm:<provider>:` (single Redis, no cluster)."""

# KEYS[1] holders zset (member = lease id, score = expiry ms), KEYS[2] queue zset (member = waiter id,
# score = enqueue ms).
# ARGV: now_ms, waiter_id, uid, rank(0 interactive, 1 background, 2 best_effort), since_ms, slots, bg_max,
#       be_max, user_max, hold_ttl_ms, prefix, waiter_ttl_ms
# Returns {granted (0/1), queue length}.
# The waiter is granted a slot only when it is the best eligible waiter: lowest effective rank (a background
# waiter older than 30 s ranks with interactive), then the user holding the fewest slots, then the oldest.
TRY_ACQUIRE = r"""
local now = tonumber(ARGV[1]); local me = ARGV[2]; local uid = ARGV[3]; local rank = tonumber(ARGV[4])
local since = tonumber(ARGV[5]); local slots = tonumber(ARGV[6]); local bg_max = tonumber(ARGV[7])
local be_max = tonumber(ARGV[8]); local user_max = tonumber(ARGV[9]); local ttl = tonumber(ARGV[10])
local p = ARGV[11]; local wttl = tonumber(ARGV[12])
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
local waiters = redis.call('ZRANGE', KEYS[2], 0, -1)
local qlen = #waiters
if held >= slots then return {0, qlen} end
local contested = qlen > 1
local best = nil
local best_key = nil
for _, w in ipairs(waiters) do
  local m = redis.call('HMGET', p .. 'waiter:' .. w, 'uid', 'rank', 'since')
  if not m[1] then
    redis.call('ZREM', KEYS[2], w)
  else
    local wr = tonumber(m[2]); local s = tonumber(m[3])
    local r = wr
    if r == 1 and now - s >= 30000 then r = 0 end
    local ok = true
    if wr >= 1 and (lane[2] + lane[3]) >= bg_max then ok = false end
    if wr == 2 and lane[3] >= be_max then ok = false end
    if contested and (per_user[m[1]] or 0) >= user_max then ok = false end
    if ok then
      local k1, k2, k3 = r, (per_user[m[1]] or 0), s
      if best == nil or k1 < best_key[1]
         or (k1 == best_key[1] and (k2 < best_key[2] or (k2 == best_key[2] and k3 < best_key[3]))) then
        best, best_key = w, {k1, k2, k3}
      end
    end
  end
end
if best ~= me then return {0, qlen} end
redis.call('ZREM', KEYS[2], me)
redis.call('DEL', p .. 'waiter:' .. me)
redis.call('ZADD', KEYS[1], now + ttl, me)
redis.call('HSET', p .. 'holder:' .. me, 'uid', uid, 'rank', rank)
redis.call('PEXPIRE', p .. 'holder:' .. me, ttl)
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
