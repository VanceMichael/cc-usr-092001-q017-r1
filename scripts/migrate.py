"""初始化项目的本地 SQLite 数据文件。"""

from src.db import migrate


def main() -> None:
    database_path = migrate()
    print(f"数据库初始化完成：{database_path}")


if __name__ == "__main__":
    main()
