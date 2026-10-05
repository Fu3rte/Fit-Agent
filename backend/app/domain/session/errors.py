class SessionError(Exception):
    pass


class SessionNotFound(SessionError):
    pass


class EntryNotFound(SessionNotFound):
    # 会话存在但目标节点不存在；作为 SessionNotFound 子类保持既有捕获行为。
    pass


class RunNotFound(SessionNotFound):
    # 会话存在但目标运行不存在；作为 SessionNotFound 子类保持既有捕获行为。
    pass


class SessionConflict(SessionError):
    pass


class InvalidTargetEntry(SessionConflict):
    pass


class SessionMismatch(SessionConflict):
    pass


class OperationConflict(SessionConflict):
    pass


class OperationExpired(OperationConflict):
    # 旧操作记录已随内容删除失效，禁止重新执行。
    pass


class RunClosed(SessionConflict):
    pass


class SteeringConsumptionConflict(SessionConflict):
    pass


class IncompleteToolChain(SessionConflict):
    pass


class CredentialDetected(SessionError):
    pass
