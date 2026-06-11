"""
MinerU Tianshu - Redis Task Queue
天枢 Redis 任务队列

高性能任务队列实现，用于解决 SQLite 单写锁瓶颈
支持优先级队列、可靠投递、水平扩展

架构:
    - Redis Sorted Set 实现优先级队列
    - Processing Set 跟踪进行中的任务
    - SQLite 仍保留任务元数据存储（历史记录、结果）
"""

import os
import time
import json
from typing import Optional, Dict
from dataclasses import dataclass
from loguru import logger

try:
    import redis

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("⚠️  redis package not installed. Run: pip install redis")


# 原子出队脚本：弹出最高优先级(最小 score)任务并同时写入 processing set，
# 二者在 Redis 服务端单线程内原子执行，杜绝「已弹出但未记录」的丢任务窗口。
_DEQUEUE_LUA = """
local popped = redis.call('ZPOPMIN', KEYS[1], 1)
if (not popped) or (#popped == 0) then
    return nil
end
local task_id = popped[1]
redis.call('HSET', KEYS[2], task_id, ARGV[1])
return task_id
"""


@dataclass
class RedisConfig:
    """Redis 配置"""

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: Optional[str] = None
    socket_timeout: float = 5.0
    socket_connect_timeout: float = 5.0
    retry_on_timeout: bool = True

    # 队列配置
    queue_key: str = "tianshu:task_queue"  # 优先级队列 (Sorted Set)
    processing_key: str = "tianshu:processing"  # 处理中任务 (Set)
    task_data_prefix: str = "tianshu:task:"  # 任务数据前缀 (Hash)

    # 超时配置
    task_timeout_seconds: int = 3600  # 任务超时时间 (1小时)
    claim_visibility_seconds: int = 300  # 任务可见性超时 (5分钟)

    @classmethod
    def from_env(cls) -> "RedisConfig":
        """从环境变量加载配置"""
        return cls(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            password=os.getenv("REDIS_PASSWORD") or None,
            queue_key=os.getenv("REDIS_QUEUE_KEY", "tianshu:task_queue"),
            processing_key=os.getenv("REDIS_PROCESSING_KEY", "tianshu:processing"),
            task_timeout_seconds=int(os.getenv("REDIS_TASK_TIMEOUT", "3600")),
        )


class RedisTaskQueue:
    """
    Redis 任务队列

    使用 Sorted Set 实现优先级队列:
        - score = -priority * 1e10 + timestamp (优先级高的先出，同优先级按时间)
        - BZPOPMIN 阻塞获取最高优先级任务

    可靠投递:
        - 任务从 queue 移动到 processing set
        - Worker 心跳更新处理时间
        - 超时任务自动重新入队
    """

    def __init__(self, config: Optional[RedisConfig] = None):
        """
        初始化 Redis 队列

        Args:
            config: Redis 配置，默认从环境变量加载
        """
        if not REDIS_AVAILABLE:
            raise RuntimeError("redis package not installed. Run: pip install redis")

        self.config = config or RedisConfig.from_env()
        self._client: Optional[redis.Redis] = None
        self._connected = False
        self._dequeue_script = None  # 延迟注册的原子出队 Lua 脚本

    @property
    def client(self) -> redis.Redis:
        """获取 Redis 客户端（延迟连接）"""
        if self._client is None:
            self._client = redis.Redis(
                host=self.config.host,
                port=self.config.port,
                db=self.config.db,
                password=self.config.password,
                socket_timeout=self.config.socket_timeout,
                socket_connect_timeout=self.config.socket_connect_timeout,
                retry_on_timeout=self.config.retry_on_timeout,
                decode_responses=True,
            )
        return self._client

    def is_available(self) -> bool:
        """检查 Redis 是否可用"""
        try:
            self.client.ping()
            self._connected = True
            return True
        except Exception as e:
            logger.warning(f"Redis not available: {e}")
            self._connected = False
            return False

    def enqueue(
        self,
        task_id: str,
        priority: int = 0,
        task_data: Optional[Dict] = None,
    ) -> bool:
        """
        将任务加入队列

        Args:
            task_id: 任务ID
            priority: 优先级（数字越大越优先）
            task_data: 任务数据（可选，用于快速访问）

        Returns:
            bool: 是否成功入队
        """
        try:
            # 计算分数：优先级高的先出，同优先级按时间先后
            # score = -priority * 1e10 + timestamp
            timestamp = time.time()
            score = -priority * 1e10 + timestamp

            pipe = self.client.pipeline()

            # 添加到优先级队列
            pipe.zadd(self.config.queue_key, {task_id: score})

            # 存储任务数据（可选，用于快速访问）
            if task_data:
                task_key = f"{self.config.task_data_prefix}{task_id}"
                pipe.hset(
                    task_key,
                    mapping={
                        "task_id": task_id,
                        "priority": str(priority),
                        "enqueued_at": str(timestamp),
                        "data": json.dumps(task_data),
                    },
                )
                # 设置过期时间（任务超时后自动清理）
                pipe.expire(task_key, self.config.task_timeout_seconds)

            pipe.execute()
            logger.debug(f"📥 Task {task_id} enqueued with priority {priority}")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to enqueue task {task_id}: {e}")
            return False

    def dequeue(
        self,
        worker_id: str,
        timeout: float = 1.0,
    ) -> Optional[str]:
        """
        从队列获取任务

        使用 Lua 脚本把「弹出最高优先级任务」与「写入 processing set」合并为单条原子操作，
        消除原先 BZPOPMIN + HSET 两步之间 Worker 崩溃导致任务丢失的窗口。
        非阻塞实现：单次脚本调用；在 timeout 内以短轮询等待，避免忙等。

        Args:
            worker_id: Worker ID
            timeout: 最长等待时间（秒）

        Returns:
            task_id: 任务ID，如果没有任务返回 None
        """
        try:
            if self._dequeue_script is None:
                self._dequeue_script = self.client.register_script(_DEQUEUE_LUA)

            deadline = time.time() + max(0.0, timeout)
            while True:
                processing_data = json.dumps({"worker_id": worker_id, "claimed_at": time.time()})
                task_id = self._dequeue_script(
                    keys=[self.config.queue_key, self.config.processing_key],
                    args=[processing_data],
                )
                if task_id:
                    logger.debug(f"📤 Task {task_id} atomically claimed by worker {worker_id}")
                    return task_id
                if time.time() >= deadline:
                    return None
                time.sleep(0.05)

        except Exception as e:
            logger.error(f"❌ Failed to dequeue task: {e}")
            return None

    def complete(self, task_id: str, worker_id: str) -> bool:
        """
        标记任务完成

        从 processing set 中移除任务

        Args:
            task_id: 任务ID
            worker_id: Worker ID

        Returns:
            bool: 是否成功
        """
        try:
            # 从 processing set 移除
            self.client.hdel(self.config.processing_key, task_id)

            # 删除任务数据缓存
            task_key = f"{self.config.task_data_prefix}{task_id}"
            self.client.delete(task_key)

            logger.debug(f"✅ Task {task_id} completed by worker {worker_id}")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to complete task {task_id}: {e}")
            return False

    def fail(self, task_id: str, worker_id: str, requeue: bool = False) -> bool:
        """
        标记任务失败

        Args:
            task_id: 任务ID
            worker_id: Worker ID
            requeue: 是否重新入队

        Returns:
            bool: 是否成功
        """
        try:
            # 从 processing set 移除
            self.client.hdel(self.config.processing_key, task_id)

            if requeue:
                # 重新入队（保持原优先级）
                task_key = f"{self.config.task_data_prefix}{task_id}"
                task_info = self.client.hgetall(task_key)
                priority = int(task_info.get("priority", "0"))

                timestamp = time.time()
                score = -priority * 1e10 + timestamp
                self.client.zadd(self.config.queue_key, {task_id: score})
                logger.info(f"🔄 Task {task_id} requeued after failure")
            else:
                # 删除任务数据缓存
                task_key = f"{self.config.task_data_prefix}{task_id}"
                self.client.delete(task_key)
                logger.debug(f"❌ Task {task_id} failed (not requeued)")

            return True

        except Exception as e:
            logger.error(f"❌ Failed to mark task {task_id} as failed: {e}")
            return False

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        """
        更新任务心跳

        防止任务因超时被重新分配

        Args:
            task_id: 任务ID
            worker_id: Worker ID

        Returns:
            bool: 是否成功
        """
        try:
            processing_data = json.dumps(
                {
                    "worker_id": worker_id,
                    "claimed_at": time.time(),  # 更新时间
                }
            )
            self.client.hset(self.config.processing_key, task_id, processing_data)
            return True
        except Exception as e:
            logger.error(f"❌ Failed to update heartbeat for task {task_id}: {e}")
            return False

    def recover_stale_tasks(self, timeout_seconds: Optional[int] = None) -> int:
        """
        恢复超时任务

        检查 processing set 中超时的任务，重新入队

        Args:
            timeout_seconds: 超时时间（秒），默认使用配置值

        Returns:
            int: 恢复的任务数量
        """
        timeout = timeout_seconds or self.config.claim_visibility_seconds
        recovered_count = 0
        now = time.time()

        try:
            # 获取所有处理中的任务
            processing_tasks = self.client.hgetall(self.config.processing_key)

            for task_id, data_str in processing_tasks.items():
                try:
                    data = json.loads(data_str)
                    claimed_at = data.get("claimed_at", 0)

                    if now - claimed_at > timeout:
                        # 任务超时，重新入队
                        worker_id = data.get("worker_id", "unknown")
                        logger.warning(f"⚠️  Task {task_id} timed out (worker: {worker_id}), requeuing...")

                        # 从 processing 移除
                        self.client.hdel(self.config.processing_key, task_id)

                        # 重新入队（默认优先级）
                        task_key = f"{self.config.task_data_prefix}{task_id}"
                        task_info = self.client.hgetall(task_key)
                        priority = int(task_info.get("priority", "0"))

                        score = -priority * 1e10 + now
                        self.client.zadd(self.config.queue_key, {task_id: score})
                        recovered_count += 1

                except json.JSONDecodeError:
                    logger.error(f"Invalid processing data for task {task_id}")

            if recovered_count > 0:
                logger.info(f"🔄 Recovered {recovered_count} stale tasks")

            return recovered_count

        except Exception as e:
            logger.error(f"❌ Failed to recover stale tasks: {e}")
            return 0

    def get_stats(self) -> Dict:
        """
        获取队列统计信息

        Returns:
            dict: 队列统计
        """
        try:
            pending_count = self.client.zcard(self.config.queue_key)
            processing_count = self.client.hlen(self.config.processing_key)

            return {
                "pending": pending_count,
                "processing": processing_count,
                "redis_connected": True,
            }
        except Exception as e:
            logger.error(f"❌ Failed to get queue stats: {e}")
            return {
                "pending": 0,
                "processing": 0,
                "redis_connected": False,
                "error": str(e),
            }

    def clear_queue(self) -> bool:
        """
        清空队列（危险操作，仅用于测试/重置）

        Returns:
            bool: 是否成功
        """
        try:
            pipe = self.client.pipeline()
            pipe.delete(self.config.queue_key)
            pipe.delete(self.config.processing_key)
            # 清理所有任务数据
            keys = self.client.keys(f"{self.config.task_data_prefix}*")
            if keys:
                pipe.delete(*keys)
            pipe.execute()
            logger.warning("⚠️  Queue cleared!")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to clear queue: {e}")
            return False


# 全局队列实例（延迟初始化）
# 使用特殊值 False 表示"已检查,确认禁用",避免重复检查和日志
# None: 未初始化
# False: 已确认禁用
# RedisTaskQueue: 已初始化的队列实例
_queue_instance: Optional[RedisTaskQueue] | bool | None = None


def get_redis_queue() -> Optional[RedisTaskQueue]:
    """
    获取 Redis 队列实例（单例模式）

    状态语义：
        - None: 未初始化
        - False: 配置上确认禁用（REDIS_QUEUE_ENABLED != true），永久缓存
        - RedisTaskQueue: 已创建实例（即使当前 ping 不通也保留，允许后续自动重连）

    与旧实现的区别：当 Redis「已启用但暂时连不上」时，不再把单例永久置 False，
    而是保留实例并在每次调用时重新探测，使 Redis 晚于服务启动也能恢复使用。

    Returns:
        RedisTaskQueue 或 None
    """
    global _queue_instance

    if not REDIS_AVAILABLE:
        return None

    # 配置层面确认禁用：永久返回 None
    if _queue_instance is False:
        return None

    # 首次：判断是否启用并尝试创建实例
    if _queue_instance is None:
        try:
            from config import get_settings

            enabled = get_settings().redis_queue_enabled
        except Exception:
            enabled = os.getenv("REDIS_QUEUE_ENABLED", "false").lower() == "true"

        if not enabled:
            logger.info("ℹ️  Redis queue disabled (REDIS_QUEUE_ENABLED != true)")
            _queue_instance = False  # 配置禁用，永久缓存
            return None

        try:
            _queue_instance = RedisTaskQueue()
            # 创建时探测一次仅用于日志；无论通断都保留实例，
            # 后续由 redis-py 的按命令自动重连处理瞬时断连。
            if _queue_instance.is_available():
                logger.info(f"✅ Redis queue connected: {_queue_instance.config.host}:{_queue_instance.config.port}")
            else:
                logger.warning("⚠️  Redis enabled but not reachable yet; will retry on demand (SQLite fallback)")
        except Exception as e:
            logger.error(f"❌ Failed to initialize Redis queue: {e}")
            # 创建失败可能是瞬时问题，不永久禁用；下次调用再试
            _queue_instance = None
            return None

    # 已有实例：直接返回。单条命令失败由各方法 try/except 兜底并回退 SQLite，
    # redis-py 会在下一条命令自动重连，无需每次 ping（避免给轮询增加开销）。
    return _queue_instance if isinstance(_queue_instance, RedisTaskQueue) else None


if __name__ == "__main__":
    # 测试代码
    import os

    os.environ["REDIS_QUEUE_ENABLED"] = "true"

    queue = get_redis_queue()
    if queue and queue.is_available():
        print("✅ Redis connected!")

        # 测试入队
        queue.enqueue("test-task-1", priority=10)
        queue.enqueue("test-task-2", priority=5)
        queue.enqueue("test-task-3", priority=10)  # 同优先级，后入队

        # 测试出队（应该按优先级顺序）
        task1 = queue.dequeue("worker-1", timeout=1)
        print(f"Dequeued: {task1}")  # 应该是 test-task-1

        task2 = queue.dequeue("worker-1", timeout=1)
        print(f"Dequeued: {task2}")  # 应该是 test-task-3

        # 获取统计
        stats = queue.get_stats()
        print(f"Stats: {stats}")

        # 清理
        queue.clear_queue()
        print("✅ Test completed!")
    else:
        print("⚠️  Redis not available")
