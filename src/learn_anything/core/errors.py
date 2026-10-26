class StepError(Exception):
    """业务前置条件不满足（如依赖步骤未完成），API 层转成 409。"""


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass
