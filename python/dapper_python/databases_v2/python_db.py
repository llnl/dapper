# Using __future__ annotations breaks SQLModel ORM relationships, don't use it here
# See https://github.com/fastapi/sqlmodel/discussions/900 for issue discussion

from pathlib import PurePosixPath
from sqlalchemy import Engine, text
from sqlmodel import SQLModel, Field as SQLField, Relationship, Column

from typing import Optional

from dapper_python.databases_v2.database import BaseDatabase
from dapper_python.databases_v2.database import SQLPath
from dapper_python.normalize import normalize_file_name


class PyPIDatabase(BaseDatabase):
    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)

        # Need to create views manually since SQLModel does not have native support for views
        # TODO: See if there's some better way to create this without writing raw SQL
        with self._engine.connect() as conn:
            with conn.begin():
                # User-facing view for imports which hides the backend tracking logic
                create_view_cmd = """
                    CREATE VIEW
                    IF NOT EXISTS v_package_imports
                    AS 
                        SELECT package_name, import_as
                        FROM packages
                        JOIN package_imports
                        ON packages.id = package_imports.package_id
                """
                conn.execute(text(create_view_cmd))

                # User-facing view for files which hides the backend tracking logic
                create_view_cmd = """
                    CREATE VIEW
                    IF NOT EXISTS v_package_files
                    AS 
                        SELECT package_name, normalized_file_name, file_name, file_path, mime_type, magic_string
                        FROM packages
                        JOIN package_files
                        ON packages.id = package_files.package_id
                """
                conn.execute(text(create_view_cmd))


# Database Tables
@PyPIDatabase.register_model
class Package(SQLModel, table=True):
    __tablename__ = "packages"

    id: Optional[int] = SQLField(default=None, nullable=False, primary_key=True)
    package_name: str
    last_serial: int

    # Relationships
    imports: list["PackageImport"] = Relationship(back_populates="package")
    files: list["PackageFile"] = Relationship(back_populates="package")


@PyPIDatabase.register_model
class PackageImport(SQLModel, table=True):
    __tablename__ = "package_imports"

    id: Optional[int] = SQLField(default=None, nullable=False, primary_key=True)
    package_id: Optional[int] = SQLField(default=None, nullable=False, foreign_key="packages.id", index=True, ondelete="CASCADE")
    import_as: str = SQLField(index=True)

    # Relationships
    package: "Package" = Relationship(back_populates="imports")


@PyPIDatabase.register_model
class PackageFile(SQLModel, table=True):
    __tablename__ = "package_files"

    id: Optional[int] = SQLField(default=None, nullable=False, primary_key=True)
    package_id: Optional[int] = SQLField(default=None, nullable=False, foreign_key="packages.id", index=True, ondelete="CASCADE")
    file_name: str = SQLField(default=None)
    normalized_file_name: str = SQLField(default=None)
    file_path: PurePosixPath = SQLField(sa_column=Column(SQLPath(PurePosixPath)))
    mime_type: str
    magic_string: str

    # Relationships
    package: "Package" = Relationship(back_populates="files")

    # Normalized file name and file name automatically constructed from file_path if not provided
    def model_post_init(self, __context) -> None:
        # Automatically get and normalize the filename
        self.file_path = PurePosixPath(self.file_path)
        if self.file_name is None:
            self.file_name = self.file_path.name
        if self.normalized_file_name is None:
            self.normalized_file_name = str(normalize_file_name(self.file_name))
