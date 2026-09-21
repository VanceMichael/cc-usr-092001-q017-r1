"""白玉蟹联合品控结算链服务入口。"""

import os

from .api import health_payload, make_server

__all__ = ["health_payload", "make_server", "main"]


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    make_server(port).serve_forever()


if __name__ == "__main__":
    main()
