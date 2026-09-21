"""初始化项目的本地 SQLite 数据文件。"""

from src.db import connect, initialize


def main() -> None:
    conn = connect()
    try:
        initialize(conn)
        version = conn.execute(
            "SELECT value FROM service_meta WHERE key='schema_version'"
        ).fetchone()["value"]
    finally:
        conn.close()
    print(f"数据库初始化完成（schema_version={version}）")


if __name__ == "__main__":
    main()
