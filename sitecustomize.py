from __future__ import annotations

import sys


try:
    from pysqlite3 import dbapi2 as _sqlite3  # type: ignore
except Exception:
    pass
else:
    sys.modules["sqlite3"] = _sqlite3
