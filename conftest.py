"""Run the repository's plain async test functions without another dependency."""

from __future__ import annotations

import asyncio
import inspect


def pytest_pyfunc_call(pyfuncitem) -> bool | None:
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    arguments = {
        name: pyfuncitem.funcargs[name]
        for name in pyfuncitem._fixtureinfo.argnames
    }
    asyncio.run(pyfuncitem.obj(**arguments))
    return True
