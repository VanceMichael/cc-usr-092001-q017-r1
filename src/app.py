"""服务启动入口。

启动时确保本地数据库结构就绪，默认监听 8080，健康检查为 /health，
业务接口统一在 /v1/ 前缀下。
"""

import os
from http.server import ThreadingHTTPServer

from .api import Handler
from .db import connect, initialize


def main() -> None:
    conn = connect()
    try:
        initialize(conn)
    finally:
        conn.close()
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
