"""
Redis 命令文档数据集。

提供 Redis 常用命令的静态文档（命令名 → 摘要/语法/分组/官网链接）。
用于管理后台查询台 Redis 命令窗口的文档面板展示（对标 Medis）。
数据来自 Redis 官方文档，纯静态、无外部依赖。
"""

REDIS_COMMANDS = {
    # Generic / 通用操作
    "DEL": {
        "summary": "删除一个或多个键",
        "syntax": "DEL key [key ...]",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/del/",
    },
    "EXISTS": {
        "summary": "检查键是否存在",
        "syntax": "EXISTS key [key ...]",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/exists/",
    },
    "EXPIRE": {
        "summary": "设置键的过期时间（秒）",
        "syntax": "EXPIRE key seconds [NX|XX|GT|LT]",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/expire/",
    },
    "EXPIREAT": {
        "summary": "设置键的过期时间（Unix 时间戳，秒）",
        "syntax": "EXPIREAT key unix-time-seconds [NX|XX|GT|LT]",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/expireat/",
    },
    "PEXPIRE": {
        "summary": "设置键的过期时间（毫秒）",
        "syntax": "PEXPIRE key milliseconds [NX|XX|GT|LT]",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/pexpire/",
    },
    "PEXPIREAT": {
        "summary": "设置键的过期时间（Unix 时间戳，毫秒）",
        "syntax": "PEXPIREAT key unix-time-milliseconds [NX|XX|GT|LT]",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/pexpireat/",
    },
    "TTL": {
        "summary": "获取键的剩余过期时间（秒，-1 表示永不过期，-2 表示不存在）",
        "syntax": "TTL key",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/ttl/",
    },
    "PTTL": {
        "summary": "获取键的剩余过期时间（毫秒）",
        "syntax": "PTTL key",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/pttl/",
    },
    "PERSIST": {
        "summary": "移除键的过期时间，使其永不过期",
        "syntax": "PERSIST key",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/persist/",
    },
    "KEYS": {
        "summary": "返回匹配模式的所有键（生产环境不推荐使用）",
        "syntax": "KEYS pattern",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/keys/",
    },
    "SCAN": {
        "summary": "增量式迭代数据库键（游标遍历）",
        "syntax": "SCAN cursor [MATCH pattern] [COUNT count] [TYPE type]",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/scan/",
    },
    "TYPE": {
        "summary": "返回键的数据类型",
        "syntax": "TYPE key",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/type/",
    },
    "RENAME": {
        "summary": "重命名键",
        "syntax": "RENAME key newkey",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/rename/",
    },
    "RENAMENX": {
        "summary": "仅当新键不存在时重命名键",
        "syntax": "RENAMENX key newkey",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/renamenx/",
    },
    "RANDOMKEY": {
        "summary": "从数据库中随机返回一个键",
        "syntax": "RANDOMKEY",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/randomkey/",
    },
    "DUMP": {
        "summary": "序列化键的值",
        "syntax": "DUMP key",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/dump/",
    },
    "RESTORE": {
        "summary": "反序列化 DUMP 返回的值并创建键",
        "syntax": "RESTORE key ttl serialized-value [REPLACE] [ABSTTL] [IDLETIME seconds] [FREQ frequency]",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/restore/",
    },
    "TOUCH": {
        "summary": "更新键的最后访问时间",
        "syntax": "TOUCH key [key ...]",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/touch/",
    },
    "UNLINK": {
        "summary": "异步删除一个或多个键（DEL 的非阻塞版本）",
        "syntax": "UNLINK key [key ...]",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/unlink/",
    },
    "COPY": {
        "summary": "将一个键的值复制到另一个键",
        "syntax": "COPY source destination [DB destination-db] [REPLACE]",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/copy/",
    },
    "OBJECT": {
        "summary": "获取键的对象编码、引用计数、空闲时间等信息",
        "syntax": "OBJECT ENCODING|REFCOUNT|IDLETIME|FREQ key",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/object/",
    },
    "MOVE": {
        "summary": "将键移动到另一个数据库",
        "syntax": "MOVE key db",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/move/",
    },
    "WAIT": {
        "summary": "等待写入被复制到指定数量的副本",
        "syntax": "WAIT numreplicas timeout",
        "group": "generic",
        "url": "https://redis.io/docs/latest/commands/wait/",
    },

    # String / 字符串
    "GET": {
        "summary": "获取字符串值",
        "syntax": "GET key",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/get/",
    },
    "SET": {
        "summary": "设置字符串值",
        "syntax": "SET key value [EX seconds|PX milliseconds|EXAT unix-time-seconds|PXAT unix-time-milliseconds|KEEPTTL] [NX|XX] [GET]",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/set/",
    },
    "SETEX": {
        "summary": "设置字符串值并指定过期时间（秒）",
        "syntax": "SETEX key seconds value",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/setex/",
    },
    "SETNX": {
        "summary": "仅当键不存在时设置字符串值",
        "syntax": "SETNX key value",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/setnx/",
    },
    "PSETEX": {
        "summary": "设置字符串值并指定过期时间（毫秒）",
        "syntax": "PSETEX key milliseconds value",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/psetex/",
    },
    "GETSET": {
        "summary": "设置新值并返回旧值",
        "syntax": "GETSET key value",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/getset/",
    },
    "GETDEL": {
        "summary": "获取值并删除键",
        "syntax": "GETDEL key",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/getdel/",
    },
    "GETEX": {
        "summary": "获取值并设置过期时间选项",
        "syntax": "GETEX key [EX seconds|PX milliseconds|EXAT unix-time-seconds|PXAT unix-time-milliseconds|PERSIST]",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/getex/",
    },
    "APPEND": {
        "summary": "追加字符串值",
        "syntax": "APPEND key value",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/append/",
    },
    "STRLEN": {
        "summary": "获取字符串长度",
        "syntax": "STRLEN key",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/strlen/",
    },
    "INCR": {
        "summary": "将键的值递增 1",
        "syntax": "INCR key",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/incr/",
    },
    "INCRBY": {
        "summary": "将键的值递增指定的数值",
        "syntax": "INCRBY key increment",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/incrby/",
    },
    "INCRBYFLOAT": {
        "summary": "将键的值递增指定的浮点数",
        "syntax": "INCRBYFLOAT key increment",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/incrbyfloat/",
    },
    "DECR": {
        "summary": "将键的值递减 1",
        "syntax": "DECR key",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/decr/",
    },
    "DECRBY": {
        "summary": "将键的值递减指定的数值",
        "syntax": "DECRBY key decrement",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/decrby/",
    },
    "MGET": {
        "summary": "获取多个键的值",
        "syntax": "MGET key [key ...]",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/mget/",
    },
    "MSET": {
        "summary": "设置多个键值对",
        "syntax": "MSET key value [key value ...]",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/mset/",
    },
    "MSETNX": {
        "summary": "仅当所有键不存在时设置多个键值对",
        "syntax": "MSETNX key value [key value ...]",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/msetnx/",
    },
    "SETRANGE": {
        "summary": "从指定偏移量开始覆盖字符串",
        "syntax": "SETRANGE key offset value",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/setrange/",
    },
    "GETRANGE": {
        "summary": "获取字符串的子串",
        "syntax": "GETRANGE key start end",
        "group": "string",
        "url": "https://redis.io/docs/latest/commands/getrange/",
    },

    # Hash / 哈希表
    "HGET": {
        "summary": "获取哈希表字段的值",
        "syntax": "HGET key field",
        "group": "hash",
        "url": "https://redis.io/docs/latest/commands/hget/",
    },
    "HSET": {
        "summary": "设置哈希表字段的值",
        "syntax": "HSET key field value [field value ...]",
        "group": "hash",
        "url": "https://redis.io/docs/latest/commands/hset/",
    },
    "HSETNX": {
        "summary": "仅当哈希表字段不存在时设置其值",
        "syntax": "HSETNX key field value",
        "group": "hash",
        "url": "https://redis.io/docs/latest/commands/hsetnx/",
    },
    "HMGET": {
        "summary": "获取哈希表多个字段的值",
        "syntax": "HMGET key field [field ...]",
        "group": "hash",
        "url": "https://redis.io/docs/latest/commands/hmget/",
    },
    "HMSET": {
        "summary": "设置哈希表多个字段的值（已弃用，用 HSET 替代）",
        "syntax": "HMSET key field value [field value ...]",
        "group": "hash",
        "url": "https://redis.io/docs/latest/commands/hmset/",
    },
    "HGETALL": {
        "summary": "获取哈希表的所有字段和值",
        "syntax": "HGETALL key",
        "group": "hash",
        "url": "https://redis.io/docs/latest/commands/hgetall/",
    },
    "HDEL": {
        "summary": "删除哈希表的字段",
        "syntax": "HDEL key field [field ...]",
        "group": "hash",
        "url": "https://redis.io/docs/latest/commands/hdel/",
    },
    "HEXISTS": {
        "summary": "检查哈希表字段是否存在",
        "syntax": "HEXISTS key field",
        "group": "hash",
        "url": "https://redis.io/docs/latest/commands/hexists/",
    },
    "HKEYS": {
        "summary": "获取哈希表的所有字段名",
        "syntax": "HKEYS key",
        "group": "hash",
        "url": "https://redis.io/docs/latest/commands/hkeys/",
    },
    "HVALS": {
        "summary": "获取哈希表的所有值",
        "syntax": "HVALS key",
        "group": "hash",
        "url": "https://redis.io/docs/latest/commands/hvals/",
    },
    "HLEN": {
        "summary": "获取哈希表的字段数量",
        "syntax": "HLEN key",
        "group": "hash",
        "url": "https://redis.io/docs/latest/commands/hlen/",
    },
    "HINCRBY": {
        "summary": "将哈希表字段的值递增指定的数值",
        "syntax": "HINCRBY key field increment",
        "group": "hash",
        "url": "https://redis.io/docs/latest/commands/hincrby/",
    },
    "HINCRBYFLOAT": {
        "summary": "将哈希表字段的值递增指定的浮点数",
        "syntax": "HINCRBYFLOAT key field increment",
        "group": "hash",
        "url": "https://redis.io/docs/latest/commands/hincrbyfloat/",
    },
    "HSCAN": {
        "summary": "增量式迭代哈希表字段",
        "syntax": "HSCAN key cursor [MATCH pattern] [COUNT count]",
        "group": "hash",
        "url": "https://redis.io/docs/latest/commands/hscan/",
    },
    "HSTRLEN": {
        "summary": "获取哈希表字段值的字符串长度",
        "syntax": "HSTRLEN key field",
        "group": "hash",
        "url": "https://redis.io/docs/latest/commands/hstrlen/",
    },
    "HRANDFIELD": {
        "summary": "从哈希表中随机获取字段",
        "syntax": "HRANDFIELD key [count [WITHVALUES]]",
        "group": "hash",
        "url": "https://redis.io/docs/latest/commands/hrandfield/",
    },

    # List / 列表
    "LPUSH": {
        "summary": "将值推入列表头部",
        "syntax": "LPUSH key value [value ...]",
        "group": "list",
        "url": "https://redis.io/docs/latest/commands/lpush/",
    },
    "RPUSH": {
        "summary": "将值推入列表尾部",
        "syntax": "RPUSH key value [value ...]",
        "group": "list",
        "url": "https://redis.io/docs/latest/commands/rpush/",
    },
    "LPOP": {
        "summary": "弹出列表头部的值",
        "syntax": "LPOP key [count]",
        "group": "list",
        "url": "https://redis.io/docs/latest/commands/lpop/",
    },
    "RPOP": {
        "summary": "弹出列表尾部的值",
        "syntax": "RPOP key [count]",
        "group": "list",
        "url": "https://redis.io/docs/latest/commands/rpop/",
    },
    "LRANGE": {
        "summary": "获取列表指定范围内的值",
        "syntax": "LRANGE key start stop",
        "group": "list",
        "url": "https://redis.io/docs/latest/commands/lrange/",
    },
    "LLEN": {
        "summary": "获取列表长度",
        "syntax": "LLEN key",
        "group": "list",
        "url": "https://redis.io/docs/latest/commands/llen/",
    },
    "LINDEX": {
        "summary": "获取列表指定索引的值",
        "syntax": "LINDEX key index",
        "group": "list",
        "url": "https://redis.io/docs/latest/commands/lindex/",
    },
    "LSET": {
        "summary": "设置列表指定索引的值",
        "syntax": "LSET key index value",
        "group": "list",
        "url": "https://redis.io/docs/latest/commands/lset/",
    },
    "LINSERT": {
        "summary": "在列表元素之前或之后插入值",
        "syntax": "LINSERT key BEFORE|AFTER pivot value",
        "group": "list",
        "url": "https://redis.io/docs/latest/commands/linsert/",
    },
    "LREM": {
        "summary": "删除列表中指定值的元素",
        "syntax": "LREM key count value",
        "group": "list",
        "url": "https://redis.io/docs/latest/commands/lrem/",
    },
    "LTRIM": {
        "summary": "修剪列表保留指定范围内的元素",
        "syntax": "LTRIM key start stop",
        "group": "list",
        "url": "https://redis.io/docs/latest/commands/ltrim/",
    },
    "RPOPLPUSH": {
        "summary": "原子性地弹出一个列表的元素并推入另一个列表",
        "syntax": "RPOPLPUSH source destination",
        "group": "list",
        "url": "https://redis.io/docs/latest/commands/rpoplpush/",
    },
    "LMOVE": {
        "summary": "从一个列表弹出元素并推入另一个列表",
        "syntax": "LMOVE source destination LEFT|RIGHT LEFT|RIGHT",
        "group": "list",
        "url": "https://redis.io/docs/latest/commands/lmove/",
    },
    "BLPOP": {
        "summary": "阻塞式弹出列表头部的值",
        "syntax": "BLPOP key [key ...] timeout",
        "group": "list",
        "url": "https://redis.io/docs/latest/commands/blpop/",
    },
    "BRPOP": {
        "summary": "阻塞式弹出列表尾部的值",
        "syntax": "BRPOP key [key ...] timeout",
        "group": "list",
        "url": "https://redis.io/docs/latest/commands/brpop/",
    },
    "LPOS": {
        "summary": "获取列表中元素的位置",
        "syntax": "LPOS key element [RANK rank] [COUNT num-matches] [MAXLEN len]",
        "group": "list",
        "url": "https://redis.io/docs/latest/commands/lpos/",
    },

    # Set / 集合
    "SADD": {
        "summary": "将值添加到集合",
        "syntax": "SADD key member [member ...]",
        "group": "set",
        "url": "https://redis.io/docs/latest/commands/sadd/",
    },
    "SREM": {
        "summary": "从集合中删除值",
        "syntax": "SREM key member [member ...]",
        "group": "set",
        "url": "https://redis.io/docs/latest/commands/srem/",
    },
    "SMEMBERS": {
        "summary": "获取集合的所有成员",
        "syntax": "SMEMBERS key",
        "group": "set",
        "url": "https://redis.io/docs/latest/commands/smembers/",
    },
    "SISMEMBER": {
        "summary": "检查值是否在集合中",
        "syntax": "SISMEMBER key member",
        "group": "set",
        "url": "https://redis.io/docs/latest/commands/sismember/",
    },
    "SCARD": {
        "summary": "获取集合的成员数量",
        "syntax": "SCARD key",
        "group": "set",
        "url": "https://redis.io/docs/latest/commands/scard/",
    },
    "SPOP": {
        "summary": "从集合中随机弹出成员",
        "syntax": "SPOP key [count]",
        "group": "set",
        "url": "https://redis.io/docs/latest/commands/spop/",
    },
    "SRANDMEMBER": {
        "summary": "从集合中随机获取成员",
        "syntax": "SRANDMEMBER key [count]",
        "group": "set",
        "url": "https://redis.io/docs/latest/commands/srandmember/",
    },
    "SMOVE": {
        "summary": "将成员从一个集合移动到另一个集合",
        "syntax": "SMOVE source destination member",
        "group": "set",
        "url": "https://redis.io/docs/latest/commands/smove/",
    },
    "SDIFF": {
        "summary": "返回多个集合的差集",
        "syntax": "SDIFF key [key ...]",
        "group": "set",
        "url": "https://redis.io/docs/latest/commands/sdiff/",
    },
    "SINTER": {
        "summary": "返回多个集合的交集",
        "syntax": "SINTER key [key ...]",
        "group": "set",
        "url": "https://redis.io/docs/latest/commands/sinter/",
    },
    "SUNION": {
        "summary": "返回多个集合的并集",
        "syntax": "SUNION key [key ...]",
        "group": "set",
        "url": "https://redis.io/docs/latest/commands/sunion/",
    },
    "SDIFFSTORE": {
        "summary": "计算并存储多个集合的差集",
        "syntax": "SDIFFSTORE destination key [key ...]",
        "group": "set",
        "url": "https://redis.io/docs/latest/commands/sdiffstore/",
    },
    "SINTERSTORE": {
        "summary": "计算并存储多个集合的交集",
        "syntax": "SINTERSTORE destination key [key ...]",
        "group": "set",
        "url": "https://redis.io/docs/latest/commands/sinterstore/",
    },
    "SUNIONSTORE": {
        "summary": "计算并存储多个集合的并集",
        "syntax": "SUNIONSTORE destination key [key ...]",
        "group": "set",
        "url": "https://redis.io/docs/latest/commands/sunionstore/",
    },
    "SSCAN": {
        "summary": "增量式迭代集合成员",
        "syntax": "SSCAN key cursor [MATCH pattern] [COUNT count]",
        "group": "set",
        "url": "https://redis.io/docs/latest/commands/sscan/",
    },
    "SMISMEMBER": {
        "summary": "检查多个值是否在集合中",
        "syntax": "SMISMEMBER key member [member ...]",
        "group": "set",
        "url": "https://redis.io/docs/latest/commands/smismember/",
    },

    # Sorted Set / 有序集合
    "ZADD": {
        "summary": "将值添加到有序集合",
        "syntax": "ZADD key [NX|XX] [GT|LT] [CH] [INCR] score member [score member ...]",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zadd/",
    },
    "ZREM": {
        "summary": "从有序集合中删除成员",
        "syntax": "ZREM key member [member ...]",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zrem/",
    },
    "ZRANGE": {
        "summary": "获取有序集合指定范围的成员",
        "syntax": "ZRANGE key min max [BYSCORE|BYLEX] [REV] [LIMIT offset count] [WITHSCORES]",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zrange/",
    },
    "ZREVRANGE": {
        "summary": "获取有序集合指定范围的成员（反序）",
        "syntax": "ZREVRANGE key start stop [WITHSCORES]",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zrevrange/",
    },
    "ZRANGEBYSCORE": {
        "summary": "获取有序集合指定分数范围的成员",
        "syntax": "ZRANGEBYSCORE key min max [WITHSCORES] [LIMIT offset count]",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zrangebyscore/",
    },
    "ZREVRANGEBYSCORE": {
        "summary": "获取有序集合指定分数范围的成员（反序）",
        "syntax": "ZREVRANGEBYSCORE key max min [WITHSCORES] [LIMIT offset count]",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zrevrangebyscore/",
    },
    "ZRANGEBYLEX": {
        "summary": "按字典序获取有序集合的成员",
        "syntax": "ZRANGEBYLEX key min max [LIMIT offset count]",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zrangebylex/",
    },
    "ZREVRANGEBYLEX": {
        "summary": "按字典序获取有序集合的成员（反序）",
        "syntax": "ZREVRANGEBYLEX key max min [LIMIT offset count]",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zrevrangebylex/",
    },
    "ZSCORE": {
        "summary": "获取有序集合成员的分数",
        "syntax": "ZSCORE key member",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zscore/",
    },
    "ZCARD": {
        "summary": "获取有序集合的成员数量",
        "syntax": "ZCARD key",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zcard/",
    },
    "ZCOUNT": {
        "summary": "计算有序集合指定分数范围内的成员数量",
        "syntax": "ZCOUNT key min max",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zcount/",
    },
    "ZRANK": {
        "summary": "获取有序集合成员的排名（从低到高）",
        "syntax": "ZRANK key member",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zrank/",
    },
    "ZREVRANK": {
        "summary": "获取有序集合成员的排名（从高到低）",
        "syntax": "ZREVRANK key member",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zrevrank/",
    },
    "ZINCRBY": {
        "summary": "增加有序集合成员的分数",
        "syntax": "ZINCRBY key increment member",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zincrby/",
    },
    "ZPOPMIN": {
        "summary": "弹出有序集合分数最小的成员",
        "syntax": "ZPOPMIN key [count]",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zpopmin/",
    },
    "ZPOPMAX": {
        "summary": "弹出有序集合分数最大的成员",
        "syntax": "ZPOPMAX key [count]",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zpopmax/",
    },
    "ZSCAN": {
        "summary": "增量式迭代有序集合成员",
        "syntax": "ZSCAN key cursor [MATCH pattern] [COUNT count]",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zscan/",
    },
    "ZMSCORE": {
        "summary": "获取有序集合多个成员的分数",
        "syntax": "ZMSCORE key member [member ...]",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zmscore/",
    },
    "ZUNIONSTORE": {
        "summary": "计算并存储多个有序集合的并集",
        "syntax": "ZUNIONSTORE destination numkeys key [key ...] [WEIGHTS weight [weight ...]] [AGGREGATE SUM|MIN|MAX]",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zunionstore/",
    },
    "ZINTERSTORE": {
        "summary": "计算并存储多个有序集合的交集",
        "syntax": "ZINTERSTORE destination numkeys key [key ...] [WEIGHTS weight [weight ...]] [AGGREGATE SUM|MIN|MAX]",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zinterstore/",
    },
    "ZDIFF": {
        "summary": "计算多个有序集合的差集",
        "syntax": "ZDIFF numkeys key [key ...] [WITHSCORES]",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zdiff/",
    },
    "ZRANGESTORE": {
        "summary": "获取有序集合范围内的成员并存储到目标键",
        "syntax": "ZRANGESTORE destination source min max [BYSCORE|BYLEX] [REV] [LIMIT offset count]",
        "group": "sorted-set",
        "url": "https://redis.io/docs/latest/commands/zrangestore/",
    },

    # Server / 服务器
    "INFO": {
        "summary": "获取服务器信息",
        "syntax": "INFO [section]",
        "group": "server",
        "url": "https://redis.io/docs/latest/commands/info/",
    },
    "DBSIZE": {
        "summary": "获取当前数据库键的数量",
        "syntax": "DBSIZE",
        "group": "server",
        "url": "https://redis.io/docs/latest/commands/dbsize/",
    },
    "FLUSHDB": {
        "summary": "清空当前数据库",
        "syntax": "FLUSHDB [ASYNC|SYNC]",
        "group": "server",
        "url": "https://redis.io/docs/latest/commands/flushdb/",
    },
    "FLUSHALL": {
        "summary": "清空所有数据库",
        "syntax": "FLUSHALL [ASYNC|SYNC]",
        "group": "server",
        "url": "https://redis.io/docs/latest/commands/flushall/",
    },
    "CONFIG": {
        "summary": "管理 Redis 配置参数",
        "syntax": "CONFIG GET|SET|RESETSTAT|REWRITE parameter [value]",
        "group": "server",
        "url": "https://redis.io/docs/latest/commands/config/",
    },
    "CLIENT": {
        "summary": "管理客户端连接",
        "syntax": "CLIENT LIST|INFO|SETNAME|GETNAME|PAUSE|UNPAUSE|REPLY|KILL|UNBLOCK|TRACKING|GETREDIR|CACHING|ID|SETINFO",
        "group": "server",
        "url": "https://redis.io/docs/latest/commands/client/",
    },
    "COMMAND": {
        "summary": "获取命令信息",
        "syntax": "COMMAND [COUNT|GETKEYS|GETKEYSANDFLAGS|LIST|DOCS|INFO command-name [command-name ...]]",
        "group": "server",
        "url": "https://redis.io/docs/latest/commands/command/",
    },
    "SLOWLOG": {
        "summary": "管理慢查询日志",
        "syntax": "SLOWLOG GET|LEN|RESET",
        "group": "server",
        "url": "https://redis.io/docs/latest/commands/slowlog/",
    },
    "MEMORY": {
        "summary": "内存管理和分析",
        "syntax": "MEMORY DOCTOR|MALLOC-STATS|PURGE|STATS|USAGE key",
        "group": "server",
        "url": "https://redis.io/docs/latest/commands/memory/",
    },
    "TIME": {
        "summary": "获取 Redis 服务器当前时间",
        "syntax": "TIME",
        "group": "server",
        "url": "https://redis.io/docs/latest/commands/time/",
    },
    "LASTSAVE": {
        "summary": "获取上次保存的时间戳",
        "syntax": "LASTSAVE",
        "group": "server",
        "url": "https://redis.io/docs/latest/commands/lastsave/",
    },
    "SAVE": {
        "summary": "同步保存数据库快照到磁盘",
        "syntax": "SAVE",
        "group": "server",
        "url": "https://redis.io/docs/latest/commands/save/",
    },
    "BGSAVE": {
        "summary": "后台异步保存数据库快照",
        "syntax": "BGSAVE [SCHEDULE]",
        "group": "server",
        "url": "https://redis.io/docs/latest/commands/bgsave/",
    },
    "BGREWRITEAOF": {
        "summary": "后台重写 AOF 文件",
        "syntax": "BGREWRITEAOF",
        "group": "server",
        "url": "https://redis.io/docs/latest/commands/bgrewriteaof/",
    },
    "ACL": {
        "summary": "管理 Redis 访问控制列表",
        "syntax": "ACL CAT|WHOAMI|LIST|USERS|GETUSER|SETUSER|DELUSER|LOG|HELP|LOAD|SAVE|GENPASS|DRYRUN",
        "group": "server",
        "url": "https://redis.io/docs/latest/commands/acl/",
    },

    # Connection / 连接
    "PING": {
        "summary": "测试连接",
        "syntax": "PING [message]",
        "group": "connection",
        "url": "https://redis.io/docs/latest/commands/ping/",
    },
    "ECHO": {
        "summary": "回显消息",
        "syntax": "ECHO message",
        "group": "connection",
        "url": "https://redis.io/docs/latest/commands/echo/",
    },
    "SELECT": {
        "summary": "选择数据库",
        "syntax": "SELECT index",
        "group": "connection",
        "url": "https://redis.io/docs/latest/commands/select/",
    },
    "AUTH": {
        "summary": "认证到 Redis 服务器",
        "syntax": "AUTH [username] password",
        "group": "connection",
        "url": "https://redis.io/docs/latest/commands/auth/",
    },
    "HELLO": {
        "summary": "切换 Redis 协议版本并进行握手",
        "syntax": "HELLO protover [AUTH username password] [SETNAME clientname]",
        "group": "connection",
        "url": "https://redis.io/docs/latest/commands/hello/",
    },
    "RESET": {
        "summary": "重置连接状态",
        "syntax": "RESET",
        "group": "connection",
        "url": "https://redis.io/docs/latest/commands/reset/",
    },
    "QUIT": {
        "summary": "关闭连接",
        "syntax": "QUIT",
        "group": "connection",
        "url": "https://redis.io/docs/latest/commands/quit/",
    },

    # Scripting / 脚本
    "EVAL": {
        "summary": "执行 Lua 脚本",
        "syntax": "EVAL script numkeys [key [key ...]] [arg [arg ...]]",
        "group": "scripting",
        "url": "https://redis.io/docs/latest/commands/eval/",
    },
    "EVALSHA": {
        "summary": "执行已注册的 Lua 脚本（通过 SHA1 哈希）",
        "syntax": "EVALSHA sha1 numkeys [key [key ...]] [arg [arg ...]]",
        "group": "scripting",
        "url": "https://redis.io/docs/latest/commands/evalsha/",
    },
    "SCRIPT": {
        "summary": "管理 Lua 脚本",
        "syntax": "SCRIPT LOAD|EXISTS|FLUSH|KILL|DEBUG script",
        "group": "scripting",
        "url": "https://redis.io/docs/latest/commands/script/",
    },
    "FUNCTION": {
        "summary": "定义和管理 Redis 函数",
        "syntax": "FUNCTION LOAD|DELETE|FLUSH|KILL|LIST|STATS|HELP",
        "group": "scripting",
        "url": "https://redis.io/docs/latest/commands/function/",
    },
    "FCALL": {
        "summary": "调用 Redis 函数",
        "syntax": "FCALL function numkeys [key [key ...]] [arg [arg ...]]",
        "group": "scripting",
        "url": "https://redis.io/docs/latest/commands/fcall/",
    },

    # Pub/Sub / 发布-订阅
    "SUBSCRIBE": {
        "summary": "订阅指定频道",
        "syntax": "SUBSCRIBE channel [channel ...]",
        "group": "pubsub",
        "url": "https://redis.io/docs/latest/commands/subscribe/",
    },
    "UNSUBSCRIBE": {
        "summary": "取消订阅频道",
        "syntax": "UNSUBSCRIBE [channel [channel ...]]",
        "group": "pubsub",
        "url": "https://redis.io/docs/latest/commands/unsubscribe/",
    },
    "PSUBSCRIBE": {
        "summary": "订阅指定模式的频道",
        "syntax": "PSUBSCRIBE pattern [pattern ...]",
        "group": "pubsub",
        "url": "https://redis.io/docs/latest/commands/psubscribe/",
    },
    "PUNSUBSCRIBE": {
        "summary": "取消订阅指定模式的频道",
        "syntax": "PUNSUBSCRIBE [pattern [pattern ...]]",
        "group": "pubsub",
        "url": "https://redis.io/docs/latest/commands/punsubscribe/",
    },
    "PUBLISH": {
        "summary": "发布消息到频道",
        "syntax": "PUBLISH channel message",
        "group": "pubsub",
        "url": "https://redis.io/docs/latest/commands/publish/",
    },
    "PUBSUB": {
        "summary": "获取发布-订阅系统信息",
        "syntax": "PUBSUB CHANNELS|NUMSUB|NUMPAT",
        "group": "pubsub",
        "url": "https://redis.io/docs/latest/commands/pubsub/",
    },
    "SPUBLISH": {
        "summary": "发布消息到分片频道",
        "syntax": "SPUBLISH shardchannel message",
        "group": "pubsub",
        "url": "https://redis.io/docs/latest/commands/spublish/",
    },
    "SSUBSCRIBE": {
        "summary": "订阅分片频道",
        "syntax": "SSUBSCRIBE shardchannel [shardchannel ...]",
        "group": "pubsub",
        "url": "https://redis.io/docs/latest/commands/ssubscribe/",
    },

    # Transactions / 事务
    "MULTI": {
        "summary": "开始事务",
        "syntax": "MULTI",
        "group": "transactions",
        "url": "https://redis.io/docs/latest/commands/multi/",
    },
    "EXEC": {
        "summary": "执行事务中的所有命令",
        "syntax": "EXEC",
        "group": "transactions",
        "url": "https://redis.io/docs/latest/commands/exec/",
    },
    "DISCARD": {
        "summary": "放弃事务中的所有命令",
        "syntax": "DISCARD",
        "group": "transactions",
        "url": "https://redis.io/docs/latest/commands/discard/",
    },
    "WATCH": {
        "summary": "监视键以用于乐观锁定",
        "syntax": "WATCH key [key ...]",
        "group": "transactions",
        "url": "https://redis.io/docs/latest/commands/watch/",
    },
    "UNWATCH": {
        "summary": "取消所有键的监视",
        "syntax": "UNWATCH",
        "group": "transactions",
        "url": "https://redis.io/docs/latest/commands/unwatch/",
    },

    # Bitmap / 位图
    "SETBIT": {
        "summary": "设置字符串指定偏移量的比特位",
        "syntax": "SETBIT key offset value",
        "group": "bitmap",
        "url": "https://redis.io/docs/latest/commands/setbit/",
    },
    "GETBIT": {
        "summary": "获取字符串指定偏移量的比特位",
        "syntax": "GETBIT key offset",
        "group": "bitmap",
        "url": "https://redis.io/docs/latest/commands/getbit/",
    },
    "BITCOUNT": {
        "summary": "计数字符串中值为 1 的比特位",
        "syntax": "BITCOUNT key [start end [BYTE|BIT]]",
        "group": "bitmap",
        "url": "https://redis.io/docs/latest/commands/bitcount/",
    },
    "BITOP": {
        "summary": "对多个字符串进行位操作（AND/OR/XOR/NOT）",
        "syntax": "BITOP AND|OR|XOR|NOT destkey key [key ...]",
        "group": "bitmap",
        "url": "https://redis.io/docs/latest/commands/bitop/",
    },
    "BITPOS": {
        "summary": "查找字符串中第一个指定比特值的位置",
        "syntax": "BITPOS key bit [start [end [BYTE|BIT]]]",
        "group": "bitmap",
        "url": "https://redis.io/docs/latest/commands/bitpos/",
    },
    "BITFIELD": {
        "summary": "对字符串进行原子性多个比特位域操作",
        "syntax": "BITFIELD key [GET type offset] [SET type offset value] [INCRBY type offset increment]",
        "group": "bitmap",
        "url": "https://redis.io/docs/latest/commands/bitfield/",
    },

    # HyperLogLog
    "PFADD": {
        "summary": "将元素添加到 HyperLogLog",
        "syntax": "PFADD key [element [element ...]]",
        "group": "hyperloglog",
        "url": "https://redis.io/docs/latest/commands/pfadd/",
    },
    "PFCOUNT": {
        "summary": "获取 HyperLogLog 的近似基数",
        "syntax": "PFCOUNT key [key ...]",
        "group": "hyperloglog",
        "url": "https://redis.io/docs/latest/commands/pfcount/",
    },
    "PFMERGE": {
        "summary": "合并多个 HyperLogLog",
        "syntax": "PFMERGE destkey [sourcekey [sourcekey ...]]",
        "group": "hyperloglog",
        "url": "https://redis.io/docs/latest/commands/pfmerge/",
    },

    # Stream / 流
    "XADD": {
        "summary": "将新项添加到流",
        "syntax": "XADD key [NOMKSTREAM] [MAXLEN|MINID [=|~] threshold [LIMIT count]] * field value [field value ...]",
        "group": "stream",
        "url": "https://redis.io/docs/latest/commands/xadd/",
    },
    "XREAD": {
        "summary": "从流中读取项",
        "syntax": "XREAD [COUNT count] [BLOCK milliseconds] STREAMS key [key ...] id [id ...]",
        "group": "stream",
        "url": "https://redis.io/docs/latest/commands/xread/",
    },
    "XRANGE": {
        "summary": "获取流中指定 ID 范围的项",
        "syntax": "XRANGE key start end [COUNT count]",
        "group": "stream",
        "url": "https://redis.io/docs/latest/commands/xrange/",
    },
    "XREVRANGE": {
        "summary": "获取流中指定 ID 范围的项（反序）",
        "syntax": "XREVRANGE key end start [COUNT count]",
        "group": "stream",
        "url": "https://redis.io/docs/latest/commands/xrevrange/",
    },
    "XLEN": {
        "summary": "获取流的长度",
        "syntax": "XLEN key",
        "group": "stream",
        "url": "https://redis.io/docs/latest/commands/xlen/",
    },
    "XDEL": {
        "summary": "从流中删除项",
        "syntax": "XDEL key ID [ID ...]",
        "group": "stream",
        "url": "https://redis.io/docs/latest/commands/xdel/",
    },
    "XACK": {
        "summary": "在消费者组中标记项为已处理",
        "syntax": "XACK key group ID [ID ...]",
        "group": "stream",
        "url": "https://redis.io/docs/latest/commands/xack/",
    },
    "XGROUP": {
        "summary": "管理消费者组",
        "syntax": "XGROUP CREATE|SETID|DESTROY|DELCONSUMER key group id|$ [ENTRIESREAD entries-read]",
        "group": "stream",
        "url": "https://redis.io/docs/latest/commands/xgroup/",
    },
    "XREADGROUP": {
        "summary": "从消费者组中读取流项",
        "syntax": "XREADGROUP GROUP group consumer [COUNT count] [BLOCK milliseconds] [NOACK] STREAMS key [key ...] id [id ...]",
        "group": "stream",
        "url": "https://redis.io/docs/latest/commands/xreadgroup/",
    },

    # Geo / 地理位置
    "GEOADD": {
        "summary": "添加地理位置数据",
        "syntax": "GEOADD key [NX|XX] [CH] longitude latitude member [longitude latitude member ...]",
        "group": "geo",
        "url": "https://redis.io/docs/latest/commands/geoadd/",
    },
    "GEOPOS": {
        "summary": "获取地理位置数据的坐标",
        "syntax": "GEOPOS key member [member ...]",
        "group": "geo",
        "url": "https://redis.io/docs/latest/commands/geopos/",
    },
    "GEODIST": {
        "summary": "计算两个地理位置之间的距离",
        "syntax": "GEODIST key member1 member2 [m|km|ft|mi]",
        "group": "geo",
        "url": "https://redis.io/docs/latest/commands/geodist/",
    },
    "GEOSEARCH": {
        "summary": "查询地理位置数据",
        "syntax": "GEOSEARCH key FROMMEMBER member|FROMLONLAT longitude latitude BYRADIUS radius m|km|ft|mi|BYBOX width height m|km|ft|mi [ASC|DESC] [COUNT count] [WITHCOORD] [WITHDIST] [WITHHASH]",
        "group": "geo",
        "url": "https://redis.io/docs/latest/commands/geosearch/",
    },
    "GEOHASH": {
        "summary": "获取地理位置数据的 Geohash 表示",
        "syntax": "GEOHASH key member [member ...]",
        "group": "geo",
        "url": "https://redis.io/docs/latest/commands/geohash/",
    },
}


def lookup(command: str) -> dict | None:
    """
    按命令名（不区分大小写，取首个 token）返回文档条目；未知命令返回 None。

    Args:
        command: 命令字符串（如 "GET"、"DEL key"、"HSET key field value"）

    Returns:
        该命令的文档 dict，或 None 如果命令不存在或输入为空
    """
    if not command:
        return None

    try:
        cmd = command.strip().split()[0].upper()
    except (IndexError, AttributeError):
        return None

    return REDIS_COMMANDS.get(cmd)
