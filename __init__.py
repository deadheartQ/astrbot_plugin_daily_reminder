"""定时提醒插件包。

AstrBot 用 ``__import__("data.plugins.<插件名>.main")`` 加载插件，靠的是**隐式命名空间包**。
这类包在 pip_installer 补依赖期间被改过 sys.path / sys.modules 之后可能失效，
于是延迟执行的相对导入会报 ``No module named 'data.plugins.<插件名>'``。

这里把父级命名空间包显式登记进 ``sys.modules`` 兜住这个情况；
已存在时不覆盖，避免破坏框架自己的登记。
"""

import sys as _sys
import types as _types

_parent = __name__.rsplit(".", 1)[0] if "." in __name__ else ""
if _parent and _parent not in _sys.modules:
    try:
        _mod = _types.ModuleType(_parent)
        _mod.__path__ = list(__path__)  # type: ignore[name-defined]
        _sys.modules[_parent] = _mod
    except Exception:  # noqa: BLE001 - 兜底失败不该影响插件加载
        pass
