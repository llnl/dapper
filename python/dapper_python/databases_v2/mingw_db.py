# Using __future__ annotations breaks SQLModel ORM relationships, don't use it here
# See https://github.com/fastapi/sqlmodel/discussions/900 for issue discussion

from pathlib import PurePosixPath
from pydantic import ConfigDict
from sqlmodel import SQLModel, Field as SQLField, Relationship, Column

from typing import Optional

from dapper_python.databases_v2.database import BaseDatabase
from dapper_python.databases_v2.database import SQLPath
from dapper_python.normalize import normalize_file_name


# This needs to be placed before the models in order for the registration decorator to work
class MinGWDatabase(BaseDatabase):
    ...


# Database Tables
@MinGWDatabase.register_model
class Package(SQLModel, table=True):
    model_config = ConfigDict(extra="allow")
    __tablename__ = "packages"

    id: Optional[int] = SQLField(default=None, nullable=False, primary_key=True)
    package_name: str = SQLField(index=True)
    package_version: str

    # Relationships
    source_files: list["SourceFile"] = Relationship(back_populates="package")
    package_files: list["PackageFile"] = Relationship(back_populates="package")


# TODO: Looking at the number of times this pattern for XYZ_File is used, could be useful to make a common base class
# But there's also benefits to keeping them separate, changing one database doesn't impact another
# E.G. If we change the MinGW database, we aren't forced to change the Python or Linux database due to match
@MinGWDatabase.register_model
class PackageFile(SQLModel, table=True):
    """File in the downloaded package (ie. what would actually appear on the system once installed)

    Not to be confused with source files we build the symbol database from
    """
    model_config = ConfigDict(extra="allow")
    __tablename__ = "package_files"

    id: Optional[int] = SQLField(default=None, nullable=False, primary_key=True)
    package_id: Optional[int] = SQLField(default=None, nullable=False, foreign_key="packages.id", ondelete="CASCADE", index=True)

    file_name: str = SQLField(default=None)
    normalized_file_name: str = SQLField(default=None)
    file_path: PurePosixPath = SQLField(sa_column=Column(SQLPath(PurePosixPath)))
    mime_type: str
    magic_string: str

    # Relationships
    package: "Package" = Relationship(back_populates="package_files")

    # Normalized file name and file name automatically constructed from file_path if not provided
    def model_post_init(self, __context) -> None:
        # Automatically get and normalize the filename
        self.file_path = PurePosixPath(self.file_path)
        if self.file_name is None:
            self.file_name = self.file_path.name
        if self.normalized_file_name is None:
            self.normalized_file_name = str(normalize_file_name(self.file_name))


@MinGWDatabase.register_model
class SourceFile(SQLModel, table=True):
    """File in the source code of a package (ie. in the source tarball)

    Not to be confused with files that are part of the package itself and installed on a system when in use
    """
    model_config = ConfigDict(extra="allow")
    __tablename__ = "source_files"

    id: Optional[int] = SQLField(default=None, nullable=False, primary_key=True)
    package_id: Optional[int] = SQLField(default=None, nullable=False, foreign_key="packages.id", ondelete="CASCADE", index=True)
    file_name: str = SQLField(default=None)
    normalized_file_name: str = SQLField(default=None)
    file_path: PurePosixPath = SQLField(sa_column=Column(SQLPath(PurePosixPath)))

    # Relationships
    package: "Package" = Relationship(back_populates="source_files")
    functions: list["FunctionSymbol"] = Relationship(back_populates="file")

    # Normalized file name and file name automatically constructed from file_path if not provided
    def model_post_init(self, __context) -> None:
        # Automatically get and normalize the filename
        self.file_path = PurePosixPath(self.file_path)
        if self.file_name is None:
            self.file_name = self.file_path.name
        if self.normalized_file_name is None:
            self.normalized_file_name = str(normalize_file_name(self.file_name))


# Not currently implemented/used, but may want to add in the future
# Are not registered (and therefore no table created) with the database
class ClassSymbol(SQLModel, table=False):
    __tablename__ = "class_symbols"

    id: Optional[int] = SQLField(default=None, nullable=False, primary_key=True)
    file_id: Optional[int] = SQLField(default=None, nullable=False, foreign_key="source_files.id", ondelete="CASCADE", index=True)


class StructSymbol(SQLModel, table=False):
    __tablename__ = "struct_symbols"

    id: Optional[int] = SQLField(default=None, nullable=False, primary_key=True)
    file_id: Optional[int] = SQLField(default=None, nullable=False, foreign_key="source_files.id", ondelete="CASCADE", index=True)


@MinGWDatabase.register_model
class FunctionSymbol(SQLModel, table=True):
    __tablename__ = "function_symbols"

    id: Optional[int] = SQLField(default=None, nullable=False, primary_key=True)
    file_id: Optional[int] = SQLField(default=None, nullable=False, foreign_key="source_files.id", ondelete="CASCADE", index=True)

    return_type: str
    symbol_name: str = SQLField(index=True)
    qualified_symbol_name: str = SQLField(index=True)
    params: str
    full_signature: str = SQLField(index=True)
    source_text: str

    in_binary: Optional[bool] = SQLField(default=None)

    # Relationships
    file: "SourceFile" = Relationship(back_populates="functions")


# These are used for analysis, but not currently saved to the database, instead being dumped to JSON
# They are set up so that they could be added to the database, but are currently not for space reasons and lack of uses
# Are not registered (and therefore no table created) with the database
class PreprocessDefine(SQLModel, table=False):
    __tablename__ = "preproc_defs"

    id: Optional[int] = SQLField(default=None, nullable=False, primary_key=True)
    file_id: Optional[int] = SQLField(default=None, nullable=False, foreign_key="source_files.id", ondelete="CASCADE", index=True)
    name: str
    value: str


class StringLiteral(SQLModel, table=False):
    __tablename__ = "string_literals"

    id: Optional[int] = SQLField(default=None, nullable=False, primary_key=True)
    file_id: Optional[int] = SQLField(default=None, nullable=False, foreign_key="source_files.id", ondelete="CASCADE", index=True)
    value: str
