from datetime import time, date, datetime
import dateutil
import inspect
import json
import sqlite3
from datetime import date, datetime, time

import dateutil

from ..database import (
    DatabaseCollection,
    DatabaseSession,
    json_decode,
    json_dumps,
    json_encode,
    str_to_type,
    type_to_str,
)
from ..filter import FilterToSQL, filter_parser

"""
SQLite3 implementation of populse_db engine.

A populse_db engine is created when a DatabaseSession object is created
(typically within a "with" statement)
"""


class ParsedFilter(str):
    pass


class SQLiteSession(DatabaseSession):
    @staticmethod
    def parse_url(url):
        if url.path:
            args = (url.path,)
        elif url.netloc:
            args = (url.netloc,)
        return args, {}

    def __init__(self, sqlite_file, exclusive=False, timeout=None):
        self.sqlite = sqlite3.connect(
            sqlite_file, isolation_level=None, check_same_thread=False
        )
        self.exclusive = exclusive
        if timeout:
            self.sqlite.execute(f"PRAGMA busy_timeout={timeout}")
        self.sqlite.executescript(
            "PRAGMA synchronous=OFF;"
            "PRAGMA case_sensitive_like=ON;"
            "PRAGMA foreign_keys=ON;"
            f'BEGIN {("EXCLUSIVE" if self.exclusive else "DEFERRED")};'
        )
        self._collection_cache = {}
        # Iterate on all collections to put them in cache
        all(self)

    def close(self, rollback=False):
        if rollback:
            self.sqlite.rollback()
        else:
            self.sqlite.commit()
        self.sqlite.close()

    def has_collection(self, name):
        return name in self._collection_cache

    def __getitem__(self, collection_name):
        result = self._collection_cache.get(collection_name)
        if result is None:
            result = SQLiteCollection(self, collection_name)
            self._collection_cache[collection_name] = result
        return result

    def execute(self, sql, data=None):
        try:
            if data:
                return self.sqlite.execute(sql, data)
            else:
                return self.sqlite.execute(sql)
        except sqlite3.OperationalError as e:
            raise sqlite3.OperationalError(f"Error in SQL request: {sql}") from e

    def commit(self):
        self.sqlite.commit()
        self.sqlite.execute(f'BEGIN {("EXCLUSIVE" if self.exclusive else "DEFERRED")}')

    def rollback(self):
        self.sqlite.rollback()
        self.sqlite.execute(f'BEGIN {("EXCLUSIVE" if self.exclusive else "DEFERRED")}')

    def settings(self, category, key, default=None):
        try:
            sql = f"SELECT _json FROM [{self.populse_db_table}] WHERE category=? and key=?"
            cur = self.execute(sql, [category, key])
        except sqlite3.OperationalError:
            return default
        j = cur.fetchone()
        if j:
            return json.loads(j[0])
        return default

    def set_settings(self, category, key, value):
        sql = f"INSERT OR REPLACE INTO {self.populse_db_table} (category, key, _json) VALUES (?,?,?)"
        data = [category, key, json_dumps(value)]
        retry = False
        try:
            self.execute(sql, data)
        except sqlite3.OperationalError:
            retry = True
        if retry:
            sql2 = (
                "CREATE TABLE IF NOT EXISTS "
                f"[{self.populse_db_table}] ("
                "category TEXT NOT NULL,"
                "key TEXT NOT NULL,"
                "_json TEXT,"
                "PRIMARY KEY (category, key))"
            )
            self.execute(sql2)
            self.execute(sql, data)

    def clear(self):
        """
        Erase the whole database content.
        """

        sql = "SELECT name FROM sqlite_master"
        tables = [i[0] for i in self.execute(sql)]
        for table in tables:
            sql = f"DROP TABLE {table}"
            self.execute(sql)
        self._collection_cache = {}

    def add_collection(
        self,
        name,
        primary_key=DatabaseSession.default_primary_key,
        catchall_column="_catchall",
    ):
        if isinstance(primary_key, str):
            primary_key = {primary_key: str}
        elif isinstance(primary_key, (list, tuple)):
            primary_key = {i: str for i in primary_key}
        sql = (
            f"CREATE TABLE [{name}] ("
            f'{",".join(f"[{n}] {type_to_str(t)} NOT NULL" for n, t in primary_key.items())},'
            f"{catchall_column} dict,"
            f'PRIMARY KEY ({",".join(f"[{i}]" for i in primary_key.keys())}))'
        )
        self.execute(sql)
        # Accessing the collection to put it in cache
        self[name]

    def remove_collection(self, name):
        sql = f"DROP TABLE [{name}]"
        self.execute(sql)
        self._collection_cache.pop(name, None)

    def __iter__(self):
        sql = "SELECT name FROM sqlite_master WHERE type='table'"
        for row in self.execute(sql):
            table = row[0]
            if table == self.populse_db_table:
                continue
            yield self[table]


class SQLiteCollection(DatabaseCollection):
    _column_encodings = {
        datetime: (
            lambda d: (None if d is None else d.isoformat()),
            lambda s: (None if s is None else dateutil.parser.parse(s)),
        ),
        date: (
            lambda d: (None if d is None else d.isoformat()),
            lambda s: (None if s is None else dateutil.parser.parse(s).date()),
        ),
        time: (
            lambda d: (None if d is None else d.isoformat()),
            lambda s: (None if s is None else dateutil.parser.parse(s).time()),
        ),
        list: (
            lambda l: (None if l is None else json_dumps(l)),  # noqa: E741
            lambda l: (None if l is None else json.loads(l)),  # noqa: E741
        ),
        dict: (
            lambda d: (None if d is None else json_dumps(d)),
            lambda d: (None if d is None else json.loads(d)),
        ),
    }

    def __init__(self, session, name):
        super().__init__(session, name)
        settings = self.session.settings("collection", name, {})
        sql = f"pragma table_info({self.name})"
        bad_table = True
        catchall_column_found = False
        for row in self.session.execute(sql):
            bad_table = False
            if row[1] == self.catchall_column:
                catchall_column_found = True
                continue
            column_type_str = row[2].lower()
            column_type = str_to_type(column_type_str)
            main_type = getattr(column_type, "__origin__", None) or column_type
            encoding = self._column_encodings.get(main_type)
            if row[5]:
                self.primary_key[row[1]] = column_type
            field = {
                "collection": self.name,
                "name": row[1],
                "primary_key": bool(row[5]),
                "type": column_type,
                "encoding": encoding,
            }
            field_settings = settings.get("fields", {}).get(row[1], {})
            field.update(field_settings)
            if field_settings.get("bad_json", False):
                self.bad_json_fields.add(row[1])
            self.fields[row[1]] = field
        if bad_table:
            raise ValueError(f"No such database table: {name}")
        if self.catchall_column and not catchall_column_found:
            raise ValueError(f"table {name} must have a column {self.catchall_column}")

    def add_field(
        self, name, field_type, description=None, index=False, bad_json=False
    ):
        sql = f"ALTER TABLE [{self.name}] ADD COLUMN [{name}] {type_to_str(field_type)}"
        self.session.execute(sql)
        if index:
            sql = f"CREATE INDEX [{self.name}_{name}] ON [{self.name}] ([{name}])"
            self.session.execute(sql)
        settings = self.settings()
        settings.setdefault("fields", {})[name] = {
            "description": description,
            "index": index,
            "bad_json": bad_json,
        }
        self.set_settings(settings)
        field = {
            "collection": self.name,
            "name": name,
            "primary_key": False,
            "type": field_type,
            "description": description,
            "index": index,
            "bad_json": bad_json,
            "encoding": self._column_encodings.get(
                getattr(field_type, "__origin__", None) or field_type
            ),
        }
        self.fields[name] = field
        if bad_json:
            self.bad_json_fields.add(name)

    def remove_field(self, name):
        if name in self.primary_key:
            raise ValueError("Cannot remove a key field")
        raise NotImplementedError("SQLite does not support removing a column")
        # sql = f'ALTER TABLE [{self.name}] DROP COLUMN [{name}]'
        # self.session.execute(sql)
        # settings = self.settings()
        # settings.setdefault('fields', {}).pop(name, None)
        # self.set_settings(settings)
        # self.fields.pop(name, None)
        # self.bad_json_fields.discard(name)

    def has_document(self, document_id):
        document_id = self.document_id(document_id)
        sql = f'SELECT count(*) FROM [{self.name}] WHERE {" AND ".join(f"[{i}] = ?" for i in self.primary_key)}'
        return next(self.session.execute(sql, document_id))[0] != 0

    def _documents(self, where, where_data, fields, as_list):
        if fields:
            columns = []
            catchall_fields = set()
            for field in fields:
                if field in self.fields:
                    columns.append(field)
                else:
                    catchall_fields.add(field)
        else:
            columns = list(self.fields)
            catchall_fields = bool(self.catchall_column)
        if catchall_fields:
            if not self.catchall_column and isinstance(catchall_fields, set):
                raise ValueError(
                    f'Collection {self.name} do not have the following fields: {",".join(catchall_fields)}'
                )
            columns.append(self.catchall_column)

        sql = f'SELECT {",".join(f"[{i}]" for i in columns)} FROM [{self.name}]'
        if where:
            sql += f" WHERE {where}"
        cur = self.session.execute(sql, where_data)
        for row in cur:
            if catchall_fields:
                if as_list and catchall_fields is True:
                    raise ValueError(
                        f"as_list=True cannot be used on {self.name} without a fields list because two documents can have different fields"
                    )
                if row[-1] is not None:
                    catchall = json.loads(row[-1])
                    if isinstance(catchall_fields, set):
                        catchall = {i: catchall[i] for i in catchall_fields}
                else:
                    catchall = {}
                row = row[:-1]
            else:
                catchall = {}
            if columns[-1] == self.catchall_column:
                columns = columns[:-1]
            document = catchall
            document.update(zip(columns, row))
            for field, value in document.items():
                encoding = self.fields.get(field, {}).get("encoding")
                if encoding:
                    encode, decode = encoding
                    value = decode(value)
                if field in self.bad_json_fields:
                    value = json_decode(value)
                document[field] = value
            if as_list:
                yield [document[i] for i in fields]
            else:
                yield document

    def document(self, document_id, fields=None, as_list=False):
        document_id = self.document_id(document_id)
        where = f'{" AND ".join(f"[{i}] = ?" for i in self.primary_key)}'
        try:
            return next(self._documents(where, document_id, fields, as_list))
        except StopIteration:
            return None

    def documents(self, fields=None, as_list=False):
        yield from self._documents(None, None, fields, as_list)

    def add(self, document, replace=False):
        document_id = tuple(document.get(i) for i in self.primary_key)
        self._set_document(document_id, document, replace=replace)

    def __setitem__(self, document_id, document):
        document_id = self.document_id(document_id)
        self._set_document(document_id, document, replace=True)

    def _dict_to_sql_update(self, document):
        columns = []
        data = []
        catchall_column = None
        catchall_data = None
        catchall = {}
        for field, value in document.items():
            if field in self.primary_key:
                continue
            if field in self.bad_json_fields:
                value = json_encode(value)
            if field in self.fields:
                columns.append(field)
                data.append(self._encode_column_value(field, value))
            else:
                catchall[field] = value
        if catchall:
            if not self.catchall_column:
                raise ValueError(
                    f'Collection {self.name} cannot store the following unknown fields: {", ".join(catchall)}'
                )
            bad_json = False
            try:
                catchall_data = json_dumps(catchall)
            except TypeError:
                bad_json = True
            if bad_json:
                jsons = []
                for field, value in catchall.items():
                    bad_json = False
                    try:
                        j = json_dumps(value)
                    except TypeError:
                        bad_json = True
                    if bad_json:
                        t = type(value)
                        self.add_field(
                            field, t, bad_json=t not in (time, date, datetime)
                        )
                        column_value = self._encode_column_value(field, value)
                        columns.append(field)
                        data.append(column_value)
                    else:
                        jsons.append((f'"{field}"', j))
                if jsons:
                    catchall_column = self.catchall_column
                    catchall_data = f'{{{",".join(f"{i}:{j}" for i, j in jsons)}}}'

            else:
                catchall_column = self.catchall_column
        return columns, data, catchall_column, catchall_data

    def _set_document(self, document_id, document, replace):
        columns, data, catchall_column, catchall_data = self._dict_to_sql_update(
            document
        )

        columns = [i for i in self.primary_key] + columns
        data = [i for i in document_id] + data
        if catchall_column:
            columns.append(catchall_column)
            data.append(catchall_data)
        if replace:
            replace = " OR REPLACE"
        else:
            replace = ""
        sql = f'INSERT{replace} INTO [{self.name}] ({",".join(f"[{i}]" for i in columns)}) values ({",".join("?" for i in data)})'
        self.session.execute(sql, data)

    def update_document(self, document_id, partial_document):
        document_id = self.document_id(document_id)
        if not all(
            y is None or x == y
            for x, y in zip(
                document_id, (partial_document.get(i) for i in self.primary_key)
            )
        ):
            raise ValueError("Modification of a document's primary key is not allowed")
        columns, data, catchall_column, catchall_data = self._dict_to_sql_update(
            partial_document
        )

        if catchall_column:
            catchall_update = [
                f'[{catchall_column}]=json_patch(IFNULL([{catchall_column}],"{{}}"),?)'
            ]
            data.append(catchall_data)
        else:
            catchall_update = []
        where = " AND ".join(f"[{i}]=?" for i in self.primary_key)
        data = data + [i for i in document_id]
        affectations = [f"[{i}]=?" for i in columns] + catchall_update
        if not affectations:
            return
        sql = f'UPDATE [{self.name}] SET {",".join(affectations)} WHERE {where}'
        cur = self.session.execute(sql, data)
        if not cur.rowcount:
            raise ValueError(f"Document with key {document_id} does not exist")

    def __delitem__(self, document_id):
        document_id = self.document_id(document_id)
        sql = f'DELETE FROM [{self.name}] WHERE {" AND ".join(f"[{i}] = ?" for i in self.primary_key)}'
        self.session.execute(sql, document_id)

    def parse_filter(self, filter):
        if filter is None or isinstance(filter, ParsedFilter):
            return filter
        if inspect.isfunction(filter):
            result = ParsedFilter(lambda_to_sql(self, filter))
            return result
        tree = filter_parser().parse(filter)
        where_filter = FilterToSQL(self).transform(tree)
        if where_filter is None:
            return None
        else:
            return ParsedFilter(" ".join(where_filter))

    def filter(self, filter, fields=None, as_list=False):
        parsed_filter = self.parse_filter(filter)
        yield from self._documents(parsed_filter, None, fields=fields, as_list=as_list)

    def delete(self, filter):
        where = self.parse_filter(filter)
        sql = f"DELETE FROM [{self.name}]"
        if where:
            sql += f" WHERE {where}"
        cur = self.session.execute(sql)
        return cur.rowcount
