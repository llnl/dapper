# Using __future__ annotations breaks SQLModel ORM relationships, don't use it here
# See https://github.com/fastapi/sqlmodel/discussions/900 for issue discussion

from pathlib import PurePosixPath
from sqlmodel import SQLModel, Field as SQLField, Column
from sqlalchemy import Engine, text

from typing import Optional

from dapper_python.databases_v2.database import BaseDatabase
from dapper_python.databases_v2.database import SQLPath
from dapper_python.normalize import normalize_file_name


# This needs to be placed before the models in order for the registration decorator to work
class LinuxDatabase(BaseDatabase):
    def __init__(self, engine: Engine) -> None:
        super().__init__(engine)

        # Need to create views manually since SQLModel does not have native support for views
        # TODO: See if there's some better way to create this without writing raw SQL
        with self._engine.connect() as conn:
            with conn.begin():
                create_view_cmd = """
                    CREATE VIEW
                    IF NOT EXISTS v_package_files
                    AS
                        SELECT file_name, normalized_file_name, file_path, package_files.package_name AS package_name, full_package_name, package_sources.package_name AS source_package_name
                        FROM package_files
                        LEFT OUTER JOIN package_sources
                        ON package_files.package_name = package_sources.bin_package
                """
                conn.execute(text(create_view_cmd))


# Database Tables
@LinuxDatabase.register_model
class PackageFile(SQLModel, table=True):
    __tablename__ = "package_files"

    id: Optional[int] = SQLField(default=None, nullable=False, primary_key=True)
    file_name: str = SQLField(default=None, index=True)
    normalized_file_name: str = SQLField(index=True, default=None)
    file_path: PurePosixPath = SQLField(sa_column=Column(SQLPath(PurePosixPath)))
    package_name: str
    full_package_name: str

    # Normalized file name and file name automatically constructed from file_path if not provided
    def model_post_init(self, __context) -> None:
        # Automatically get and normalize the filename
        self.file_path = PurePosixPath(self.file_path)
        if self.file_name is None:
            self.file_name = self.file_path.name
        if self.normalized_file_name is None:
            self.normalized_file_name = str(normalize_file_name(self.file_name))


@LinuxDatabase.register_model
class PackageSource(SQLModel, table=True):
    __tablename__ = "package_sources"

    id: Optional[int] = SQLField(default=None, nullable=False, primary_key=True)
    package_name: str
    bin_package: str = SQLField(index=True)
