# Using __future__ annotations breaks SQLModel ORM relationships, don't use it here
# See https://github.com/fastapi/sqlmodel/discussions/900 for issue discussion

from sqlalchemy import Engine, text
from sqlmodel import SQLModel, Field as SQLField, Relationship

from typing import Optional

from dapper_python.databases_v2.database import BaseDatabase


class MavenDatabase(BaseDatabase):
    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)

        # Need to create views manually since SQLModel does not have native support for views
        # TODO: See if there's some better way to create this without writing raw SQL
        with self._engine.connect() as conn:
            with conn.begin():
                # User-facing view for files which hides the backend tracking logic
                create_view_cmd = """
                    CREATE VIEW
                    IF NOT EXISTS v_package_files
                    AS 
                        SELECT package_name, file_name
                        FROM packages
                        JOIN package_files
                        ON packages.id = package_files.package_id
                """
                conn.execute(text(create_view_cmd))


# Database Tables
@MavenDatabase.register_model
class Package(SQLModel, table=True):
    __tablename__ = "packages"

    id: Optional[int] = SQLField(default=None, nullable=False, primary_key=True)
    package_name: str = SQLField(index=True)
    group_id: str
    timestamp: int

    # Relationships
    files: list["PackageFile"] = Relationship(back_populates="package")


@MavenDatabase.register_model
class PackageFile(SQLModel, table=True):
    __tablename__ = "package_files"

    id: Optional[int] = SQLField(default=None, nullable=False, primary_key=True)
    package_id: Optional[int] = SQLField(default=None, nullable=False, foreign_key="packages.id", ondelete="CASCADE", index=True)
    file_name: str = SQLField(index=True)

    # Relationships
    package: "Package" = Relationship(back_populates="files")
