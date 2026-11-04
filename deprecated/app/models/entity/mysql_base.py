from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """
    SQLAlchemy 共享基类

    所有 ORM 模型共享此 Base 的 metadata，确保 create_all 能一次性创建所有表。
    """

    pass
