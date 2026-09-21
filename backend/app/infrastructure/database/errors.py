"""存储层错误类型：迁移、运行时条件写入与配置存取共用。"""


class StorageError(Exception):
    """存储层错误基类。"""


class FutureSchemaVersion(StorageError):
    """数据库 user_version 高于程序支持的迁移数：拒绝启动，不降级、不重建（07 7.2）。"""


class MigrationError(StorageError):
    """迁移文件组织或执行错误（07 7.2：编号迁移 + user_version）。"""
