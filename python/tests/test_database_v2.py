import pytest

import re
import random
import string
import warnings
import sqlite3

from pathlib import Path, PurePosixPath, PureWindowsPath
from sqlmodel import SQLModel, Field as SQLField, Column, Relationship
from sqlmodel import select

from typing import Optional

from dapper_python.databases_v2.database import BaseDatabase
from dapper_python.databases_v2.database import SQLPath


class UTDatabase(BaseDatabase):

    @property
    def db_path(self) -> Path:
        with self.session() as session:
            db_uri = str(session.get_bind().url)
            db_path = db_uri.removeprefix("sqlite:///")
            return Path(db_path)


@UTDatabase.register_model
class UTModel1(SQLModel, table=True):
    __tablename__ = "test_table_1"

    id: int = SQLField(primary_key=True)
    value: str


@UTDatabase.register_model
class UTModel2(SQLModel, table=True):
    __tablename__ = "test_table_2"

    id: int = SQLField(primary_key=True)

    # Relationships
    T3: "UTModel3" = Relationship(back_populates="T2")


@UTDatabase.register_model
class UTModel3(SQLModel, table=True):
    __tablename__ = "test_table_3"
    id: Optional[int] = SQLField(default=None, primary_key=True,
                                 foreign_key="test_table_2.id", ondelete="CASCADE")
    value: str

    # Relationships
    T2: "UTModel2" = Relationship(back_populates="T3")


@UTDatabase.register_model
class UTModel4(SQLModel, table=True):
    __tablename__ = "test_table_4"

    id: int = SQLField(primary_key=True)
    posix_path: PurePosixPath = SQLField(sa_column=Column(SQLPath(PurePosixPath)))
    windows_path: PureWindowsPath = SQLField(sa_column=Column(SQLPath(PureWindowsPath)))


@pytest.fixture
def database(tmp_path):
    database_path = tmp_path.joinpath("test_database.db")
    return UTDatabase.create_database(database_path)


def generate_test_data(n: int = 1000, strlen: int = 20) -> dict[int, str]:
    return {
        i: "".join(random.choice(string.ascii_letters) for _ in range(strlen))
        for i in range(n)
    }


def test_bulk_insert(database: UTDatabase):
    test_data = generate_test_data()

    with database.session() as session:
        data = (
            UTModel1(
                id=key,
                value=value,
            )
            for key, value in test_data.items()
        )
        with session.begin():
            session.bulk_insert(data, batch_size=95)

    # Check that the values match what we expect by accessing the database directly
    with sqlite3.connect(database.db_path) as conn:
        cursor = conn.cursor()
        query = """
            SELECT id, value
            FROM test_table_1
            WHERE id = ?
        """
        for key, value in test_data.items():
            cursor.execute(query, (int(key),))
            d_key, d_value, *_ = cursor.fetchone()

            assert d_key == key
            assert d_value == value


def test_bulk_insert_warnings(database: UTDatabase):
    test_data = generate_test_data()

    with database.session() as session:
        data = (
            UTModel2(
                id=key,
                T2=UTModel3(
                    value=value,
                ),
            )
            for key, value in test_data.items()
        )

        # We should get a warning when using bulk_insert to add a class that has relationships
        with warnings.catch_warnings(record=True) as w:
            with session.begin():
                session.bulk_insert(data, batch_size=100)

            assert len(w) == 1
            expected_message = "Class {cls} has relationships: bulk_insert will not insert them"
            assert str(w[0].message) == expected_message.format(cls=UTModel2)


def test_bulk_delete(database: UTDatabase):
    test_data = generate_test_data()

    with database.session() as session:
        data = (
            UTModel2(
                id=key,
                T3=UTModel3(
                    value=value,
                ),
            )
            for key, value in test_data.items()
        )
        with session.begin():
            session.add_all(data)

        with session.begin():
            to_remove = ((
                entry
                for entry in session.exec(select(UTModel2))
                if entry.id % 2 == 0
            ))
            session.bulk_delete(to_remove)

    expected_data = {
        key: value
        for key, value in test_data.items()
        if key % 2 != 0
    }

    with sqlite3.connect(database.db_path) as conn:
        cursor = conn.cursor()

        # Ensure deletes worked properly
        query = """
            SELECT id
            FROM test_table_2
            ORDER BY id
        """
        cursor.execute(query)
        values = cursor.fetchall()
        assert len(values) == len(test_data) // 2
        for e_id, (a_id, *_) in zip(expected_data.keys(), values):
            assert e_id == a_id

        # Ensure cascading worked properly to remove values from associated table
        query = """
            SELECT id, value
            FROM test_table_3
            ORDER BY id
        """
        cursor.execute(query)
        values = cursor.fetchall()
        for (a_id, a_value), (e_id, e_value, *_) in zip(expected_data.items(), values):
            assert a_id == e_id
            assert a_value == e_value


def test_path_loader(database: UTDatabase):
    with database.session() as session:
        # Add values to db
        with session.begin():
            session.add(UTModel4(
                id=1,
                posix_path=PurePosixPath("/test/path/posix"),
                windows_path=PureWindowsPath(r"C:\test\path\windows"),
            ))

        # Check that the values are stored in the intended manner in the backend
        with sqlite3.connect(database.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM test_table_4 WHERE id = 1")
            key, posix_path, windows_path, *_ = cursor.fetchone()
            assert posix_path == r"/test/path/posix"
            assert windows_path == r"C:/test/path/windows"

        # Check loading the value into the correct type
        data = session.exec(select(UTModel4).where(UTModel4.id == 1)).first()
        assert isinstance(data.posix_path, PurePosixPath)
        assert data.posix_path == PurePosixPath("/test/path/posix")
        assert isinstance(data.windows_path, PureWindowsPath)
        assert data.windows_path == PureWindowsPath(r"C:\test\path\windows")
