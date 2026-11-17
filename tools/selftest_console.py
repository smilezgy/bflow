"""控制台阻塞自测 —— 验证 QuickEdit 会被关掉，避免误点终端把服务卡死。

运行：python tools/selftest_console.py
非 Windows 上退化为「调用不报错」的冒烟检查。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import _disable_console_quick_edit  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


print("=== 控制台 QuickEdit 自测 ===")

try:
    _disable_console_quick_edit()
    check("关闭 QuickEdit 不抛异常", True)
except Exception as exc:
    check("关闭 QuickEdit 不抛异常", False, repr(exc))

if sys.platform == "win32":
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.argtypes = [ctypes.c_uint32]
        k32.GetStdHandle.restype = ctypes.c_void_p
        k32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        k32.GetConsoleMode.restype = ctypes.c_int
        h = k32.GetStdHandle(0xFFFFFFF6)
        mode = ctypes.c_uint32()
        ok = bool(h) and bool(k32.GetConsoleMode(h, ctypes.byref(mode)))
        if ok:
            check("调用后 QuickEdit 位已清除", not (mode.value & 0x0040),
                  hex(mode.value))
        else:
            check("输出重定向场景安全跳过（无控制台句柄）", True)
    except Exception as exc:
        check("读取控制台模式不抛异常", False, repr(exc))
else:
    check("非 Windows 平台为空操作", True)

print("\n" + "=" * 56)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
