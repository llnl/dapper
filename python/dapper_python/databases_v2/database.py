from __future__ import annotations

import re
import sqlite3
import warnings
import more_itertools

from pathlib import Path, PurePath
from enum import Flag, auto
from abc import ABC
from functools import cached_property
from contextlib import suppress

from sqlmodel import SQLModel, Field as SQLField, Session as BaseSession
from sqlmodel import create_engine, text, delete, bindparam
from sqlalchemy import Engine, Connection, PoolProxiedConnection
from sqlalchemy import event
from sqlalchemy.types import TypeDecorator, String
from sqlalchemy.inspection import inspect

from collections.abc import Iterable, Iterator, Generator
from typing import ClassVar, TypeVar, Type, Generic, Any
from typing import Union, Optional
from typing_extensions import Self

from dapper_python.utils import yet_more_itertools

ModelType = TypeVar('ModelType', bound=Type[SQLModel])
PathType = TypeVar("PathType", bound=PurePath)


class RelationshipWarning(UserWarning):
    ...


class Session(BaseSession):
    """Subclass of SQLModel's Session which provides a convenience function for fast bulk insertion"""

    class AutoCommitFlags(Flag):
        NONE = auto()
        FLUSH_BATCH = auto()
        COMMIT_BATCH = auto()
        FLUSH_END = auto()
        COMMIT_END = auto()

    @cached_property
    def _max_params(self) -> int:
        """Determine the maximum number of parameters for a prepared statement based on the sql engine + dialect

        Used to calculate maximum batching size for certain operations
        """
        engine = self.get_bind()
        match engine.dialect.name:
            case "sqlite":
                # Need to get raw connection/cursor since using session or connection starts a transaction
                cursor = engine.raw_connection().cursor()
                cursor.execute("SELECT sqlite_version()")
                version, *_ = cursor.fetchmany()[0]
                version = tuple(int(x) for x in re.split(r"[._\-]", version) if x.isdigit())

                # SQLite versions older than 3.32.0 have a maximum parameter limit of 999
                # Whereas newer versions have a maximum limit of 32766
                # See for further detail: https://www.sqlite.org/limits.html#max_variable_number
                if version < (3, 32, 0):
                    return 999
                else:
                    return 32_766

            case _:
                # Seems doable for most SQL backends. Revisit if this ever encounters problems
                return 32_766

    # From testing, there doesn't seem to be much difference between flushing each batch vs doing it all at the end
    # So we might was well periodically flush it while batching
    def bulk_insert(self, items: Iterable[SQLModel], *,
                    batch_size: int = 50_000,
                    auto_commit: AutoCommitFlags = AutoCommitFlags.FLUSH_BATCH,
                    ) -> None:
        """Convenience function for faster insertion of bulk data

        IMPORTANT: Will only insert data for the model table itself, but not any relationships/linked tables
        So only usable if the data is stored in a single table

        Takes an iterable of SQLModel objects and inserts them in batches of size batch_size
        Faster than inserting single objects at a time individually
        However, comes with the caveat that all the models in the iterable must be of the same type
        """
        first, items = more_itertools.spy(items, n=1)
        if not first:
            return
        model_type = type(first[0])

        with suppress(AttributeError):
            if model_type.__mapper__.relationships:
                warnings.warn(
                    f"Class {model_type} has relationships: bulk_insert will not insert them",
                    category=RelationshipWarning,
                    stacklevel=2,
                )

        items = yet_more_itertools.enforce_single_type(items)
        for batch in yet_more_itertools.chunked_iter(items, batch_size):
            mappings = (x.model_dump() for x in batch)
            # noinspection PyTypeChecker
            self.bulk_insert_mappings(model_type, mappings)

            if self.AutoCommitFlags.FLUSH_BATCH in auto_commit:
                self.flush()
            if self.AutoCommitFlags.COMMIT_BATCH in auto_commit:
                self.commit()

        if self.AutoCommitFlags.FLUSH_END in auto_commit:
            self.flush()
        if self.AutoCommitFlags.COMMIT_END in auto_commit:
            self.commit()

    def bulk_delete(self, items: Iterable[SQLModel], *,
                    batch_size: int = 1000,
                    auto_commit: AutoCommitFlags = AutoCommitFlags.FLUSH_BATCH,
                    ) -> None:
        """Convenience function for faster removal of bulk data

        Takes an iterable of SQLModel objects and removes them in batches of size batch_size
        Faster than removing single objects at a time individually.
        However, comes with the caveat that all the models in the iterable must be of the same type
        """
        batch_size = min(batch_size, self._max_params)

        first, items = more_itertools.spy(items, n=1)
        if not first:
            return
        model_type = type(first[0])

        # SQLModel provides bulk_insert_mappings for adding items, but there's no equivalent for bulk-removing items
        # So we'll need a workaround to bulk-remove
        primary_key = inspect(model_type).primary_key
        if len(primary_key) != 1:
            raise ValueError(f"Only supports bulk removal for non-compound primary keys {primary_key}")
        primary_key = primary_key[0]
        primary_key_name = primary_key.name

        items = yet_more_itertools.enforce_single_type(items)
        for batch in yet_more_itertools.chunked_iter(items, batch_size):
            values_to_remove = [getattr(obj, primary_key_name) for obj in batch]
            stmt = delete(model_type).where(primary_key.in_(bindparam("pks", expanding=True)))
            self.exec(stmt, params={"pks": values_to_remove})

            if self.AutoCommitFlags.FLUSH_BATCH in auto_commit:
                self.flush()
            if self.AutoCommitFlags.COMMIT_BATCH in auto_commit:
                self.commit()

        if self.AutoCommitFlags.FLUSH_END in auto_commit:
            self.flush()
        if self.AutoCommitFlags.COMMIT_END in auto_commit:
            self.commit()


class BaseDatabase(ABC):
    __registered_models: ClassVar[list[Type[SQLModel]]] = []

    @classmethod
    def register_model(cls, _model: ModelType) -> ModelType:
        """Registers an SQLModel class to be used with the database of the subclass"""
        if _model not in cls.__registered_models:
            cls.__registered_models.append(_model)
        return _model

    def __init_subclass__(cls):
        """Each subclass needs its own registered models, ensure the list is separate"""
        super().__init_subclass__()
        cls.__registered_models = []

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

        # Initialize the database
        for model in self.__registered_models:
            model.metadata.create_all(self._engine)

        # Any models registered to the base class will be set for all derived classes
        for model in BaseDatabase.__registered_models:
            model.metadata.create_all(self._engine)

    @classmethod
    def create_database(cls, db: Union[Path, str], *, exist_ok: bool = False) -> Self:
        """Create a new database file at the provided path and connects to it

        If the file already exists, it will raise a FileExistsError unless exist_ok is True
        """
        if not isinstance(db, Path):
            db = Path(db)
        if db.exists() and not exist_ok:
            raise FileExistsError(f"Database file already exists at {db}")

        db_uri = f"sqlite:///{db.absolute().as_posix()}"
        engine = create_engine(db_uri, echo=False)
        event.listen(engine, "connect", cls._sqlite_pragma_on_connect)
        return cls(engine)

    @classmethod
    def open_database(cls, db: Union[Path, str]) -> Self:
        """Connect to an existing database file at the provided path

        If the file does not exist, it will raise a FileNotFoundError
        """
        if not isinstance(db, Path):
            db = Path(db)
        if not db.exists():
            raise FileNotFoundError(f"No database file exists at {db}")

        db_uri = f"sqlite:///{db.absolute().as_posix()}"
        engine = create_engine(db_uri, echo=False)
        event.listen(engine, "connect", cls._sqlite_pragma_on_connect)
        return cls(engine)

    @staticmethod
    def _sqlite_pragma_on_connect(dbapi_conn: sqlite3.Connection, conn_record: Any) -> None:
        """Enables certain features of SQL when connecting"""
        cursor = dbapi_conn.cursor()
        # We need to enable foreign_keys to ensure cascading deletes work properly
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def session(self) -> Session:
        return Session(self._engine)

    def connection(self) -> Connection:
        return self._engine.connect()

    def raw_connection(self) -> PoolProxiedConnection:
        return self._engine.raw_connection()


@BaseDatabase.register_model
class Metadata(SQLModel, table=True):
    """Should only have a single row to store metadata about the database"""
    __tablename__ = "dataset_version"

    version: int = SQLField(primary_key=True)
    format: str
    timestamp: int


class SQLPath(TypeDecorator, Generic[PathType]):
    """Mapper to allow storing and retrieving path objects in SQL databases via ORM

    Can provide type (i.e PurePath, PurePosixPath, etc.) to control how paths are constructed when retrieved
    Paths will always be stored as posix paths in the database

    Sample Usage:
    from sqlmodel import Column
    Class MyModel(SQLModel, table=True):
        ...
        path: PurePosixPath = SQLField(sa_column=Column(SQLPath(PurePosixPath)))
    """
    impl = String()

    def __init__(self, path_cls: type[PathType] = PurePath, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if not isinstance(path_cls, type) or not issubclass(path_cls, PurePath):
            raise TypeError("path_cls must be a subclass of PurePath")
        self._path_cls = path_cls

    def process_bind_param(self, value, dialect) -> Optional[str]:
        if value is None:
            return None
        elif isinstance(value, str):
            value = self._path_cls(value)

        if not isinstance(value, PurePath):
            raise TypeError(f"Expected PurePath or subclass, got {type(value)}")
        return value.as_posix()

    def process_result_value(self, value, dialect) -> Optional[PathType]:
        if value is None:
            return None
        return self._path_cls(value)
