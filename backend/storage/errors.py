"""存储层错误类型：迁移、运行时条件写入与配置存取共用。"""


class StorageError(Exception):
    """存储层错误基类。"""


class FutureSchemaVersion(StorageError):
    """数据库 user_version 高于程序支持的迁移数：拒绝启动，不降级、不重建（07 7.2）。"""


class MigrationError(StorageError):
    """迁移文件组织或执行错误（07 7.2：编号迁移 + user_version）。"""


class RunStateConflict(StorageError):
    """条件写入失败：Run 当前状态不允许该操作（07 7.5 条件更新不命中）。"""


class ConversationBusy(StorageError):
    """已有 pending/running Run：拒绝创建新 Run 与用户消息（08 8.2，HTTP 409）：
    请求未被接受，不留下 Run 或消息（创建事务内判定，与幂等查重同一事务）。"""


class NotFound(StorageError):
    """目标行不存在（会话 / Run / Provider 配置）。"""


class InvalidInput(StorageError):
    """调用方输入不满足存储层可判定的最小约束。"""
