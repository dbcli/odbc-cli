from enum import Enum
from cyanodbc import connect, Connection, SQLGetInfo, Cursor, DatabaseError, ConnectError
from typing import Optional
from cli_helpers.tabular_output import TabularOutputFormatter
from logging import getLogger
from re import sub
from threading import Lock, Event, Thread, Condition
from asyncio import get_event_loop
from enum import IntEnum
from .dbmetadata import DbMetadata

formatter = TabularOutputFormatter()

class connStatus(Enum):
    DISCONNECTED = 0
    IDLE = 1
    EXECUTING = 2
    FETCHING = 3
    ERROR = 4

class executionStatus(IntEnum):
    OK = 0
    FAIL = 1
    OKWRESULTS = 2

class sqlConnection:
    def __init__(
        self,
        dsn: str,
        conn: Optional[Connection] = Connection(),
        username: Optional[str] = "",
        password: Optional[str] = ""
    ) -> None:
        self.dsn = dsn
        self.conn = conn
        self.cursor: Cursor = None
        self.query: str = None
        self.username = username
        self.password = password
        self.logger = getLogger(__name__)
        self.dbmetadata = DbMetadata()
        self._quotechar = None
        self._search_escapechar = None
        self._search_escapepattern = None
        # Lock to be held by database interaction that happens
        # in the main process.  Recall, main-buffer as well as preview
        # buffer queries get executed in a separate process, however
        # auto-completion, as well as object browser expansion happen
        # in the main process possibly multi-threaded.  Multi threaded is fine
        # we don't want the main process to lock-up while writing a query,
        # however, we don't want to potentially hammer the connection with
        # multiple auto-completion result queries before each has had a chance
        # to return.
        self._lock = Lock()

        # Lock to be held when updating self.status, which can happen from
        # a thread
        self._status_lock = Lock()
        # Lock to be held when updating self._execution_status_lock which
        # can happen from a thread
        self._execution_status_lock = Lock()

        # Lock that protects interaction with _fetch_res
        self._fetch_cv = Condition()
        # This is the list that carries the cache of retrieved rows via the
        # asynchronous fetch operation
        self._fetch_res: list = []
        self._fetch_thread = Thread()
        self._execution_thread = Thread()
        self._cancel_async_event = Event()

        self._status = connStatus.DISCONNECTED
        self._execution_status: executionStatus = executionStatus.OK
        self._execution_err: str = None

    @property
    def execution_status(self) -> executionStatus:
        """ Hold the lock here since it gets assigned in execute
            which can be called in a different thread """
        with self._execution_status_lock:
            res = self._execution_status
        return res

    @property
    def status(self) -> connStatus:
        """ Hold the lock here since it can be assigned in more than one
            thread """
        with self._status_lock:
            res = self._status
        return res

    @property
    def execution_err(self) -> str:
        """ Last execution error: Cleared prior to every execution.
            Hold the lock here since it gets assigned in execute
            which can be called in a different thread """
        with self._lock:
            res = self._execution_err
        return res

    @property
    def quotechar(self) -> str:
        if self._quotechar is None:
            self._quotechar = self.conn.get_info(
                    SQLGetInfo.SQL_IDENTIFIER_QUOTE_CHAR)
            # pyodbc note
            # self._quotechar = self.conn.getinfo(
        return self._quotechar

    @property
    def search_escapechar(self) -> str:
        if self._search_escapechar is None:
            self._search_escapechar = self.conn.get_info(
                    SQLGetInfo.SQL_SEARCH_PATTERN_ESCAPE)
        return self._search_escapechar

    @property
    def search_escapepattern(self) -> str:
        if self._search_escapepattern is None:
            # https://stackoverflow.com/questions/2428117/casting-raw-strings-python
            self._search_escapepattern = \
                (self.search_escapechar).encode("unicode-escape").decode()

        return self._search_escapepattern

    def update_status(self, status: connStatus = connStatus.IDLE) -> None:
        """ Thread safe way of updating the connection status
        """
        with self._status_lock:
            self._status = status

    def update_execution_status(self, status: executionStatus = executionStatus.OK) -> None:
        """ Thread safe way of updating the execution status
        """
        with self._execution_status_lock:
            self._execution_status = status

    def sanitize_search_string(self, term) -> str:
        if term is not None and len(term):
            res = sub("(_|%)", self.search_escapepattern + "\\1", term)
        else:
            res = term
        return res

    def unsanitize_search_string(self, term) -> str:
        if term is not None and len(term):
            res = sub(self.search_escapepattern, "", term)
        else:
            res = term
        return res

    def escape_name(self, name):
        if name:
            qtchar = self.quotechar
            name = (qtchar + "%s" + qtchar) % name
        return name

    def escape_names(self, names):
        return [self.escape_name(name) for name in names]

    def unescape_name(self, name):
        """ Unquote a string."""
        if name:
            qtchar = self.quotechar
            if name and name[0] == qtchar and name[-1] == qtchar:
                name = name[1:-1]

        return name

    def connect(
            self,
            username: str = "",
            password: str = "",
            force: bool = False) -> None:
        uid = username or self.username
        pwd = password or self.password
        conn_str = "DSN=" + self.dsn + ";"
        if len(uid):
            self.username = uid
            conn_str = conn_str + "UID=" + uid + ";"
        if len(pwd):
            self.password = pwd
            conn_str = conn_str + "PWD=" + pwd + ";"
        if force or not self.conn.connected():
            try:
                self.conn = connect(dsn = conn_str, timeout = 5)
                self.update_status(connStatus.IDLE)
            except ConnectError as e:
                self.logger.error("Error while connecting: %s", str(e))
                raise ConnectError(e)

    def fetchmany(self, size) -> list:
        with self._lock:
            if self.cursor:
                # This gets called in a thread / so exceptions can get lost
                # Make sure to recover after cyanodbc errors so that we can
                # complete the tail part of the worker (status/notify)
                try:
                    res = self.cursor.fetchmany(size)
                except DatabaseError as e:
                    self.logger.warning("Error while fetching: %s", str(e))
                    res = []
            else:
                res = []

        if len(res) < 1:
            self.update_status(connStatus.IDLE)
        with self._fetch_cv:
            self._fetch_res.extend(res)
            self._fetch_cv.notify()

        return res

    def async_fetchall(self, size, app) -> None:
        """ True asynchronous fetch.  Will start a fetch, that will fetch
            *all* results (in chunks of size = size) in a background operation
            until the result set is depleted or we signal a stop via
            _cancel_async_event.  After thread operation is completed,
            it asks the running event loop to redraw the app to pick up the
            new connection status (IDLE).
            """
        self._cancel_async_event.clear()
        self.update_status(connStatus.FETCHING)
        loop = get_event_loop()
        def _run():
            while True:
                res = self.fetchmany(size)
                if len(res) < 1 or self._cancel_async_event.is_set():
                    self.update_status(connStatus.IDLE)
                    self._cancel_async_event.clear()
                    # Should we try close cursor here? Problem is that
                    # close_curor attempts to call cancel async which would
                    # block until this thread is over
                    break
            loop.call_soon_threadsafe(app.invalidate)
            return

        self._fetch_thread = Thread(target = _run, daemon = True)
        self._fetch_thread.start()
        return

    def fetch_from_cache(self, size, wait = False) -> list:
        """ Will grab the first size elements from self._fetch_res.  Recall
            self._fetch_res is the result cache that is built up via an async
            fetch that grabs rows in chunks.  Here, in a threadsafe manner we
            wait for a the asynchronous method to grab enough elements or
            finish the fetch operation altogether, then we 'pop' from the
            fetch result cache.
            """
        with self._fetch_cv:
            if wait:
                self._fetch_cv.wait_for(
                        lambda: len(self._fetch_res) > size or self.status == connStatus.IDLE)
            res = self._fetch_res[:size]
            del self._fetch_res[:size]
        return res


    def cancel_async_fetchall(self) -> None:
        """ Signal fetching thread to terminate operation, then wait / block
            until thread terminates.  Also clear the fetch result cache.
            """
        self.logger.debug("cancel_async_fetchall ...")
        self._cancel_async_event.set()
        if self._fetch_thread.is_alive():
            self._fetch_thread.join()
        with self._fetch_cv:
            self._fetch_res = []

    def execute(self, query, parameters = None, event: Event = None) -> Cursor:
        self.logger.debug("Execute: %s", query)
        with self._lock:
            self.cursor = self.conn.cursor()
            try:
                self._execution_err = None
                self.update_status(connStatus.EXECUTING)
                self.cursor.execute(query, parameters)
                self.update_status(connStatus.IDLE)
                self.update_execution_status(executionStatus.OK)
                self.query = query
            except DatabaseError as e:
                self.update_status(connStatus.IDLE)
                self.update_execution_status(executionStatus.FAIL)
                self._execution_err = str(e)
                self.logger.warning("Execution error: %s", str(e))
            if event is not None:
                event.set()
        return self.cursor

    def async_execute(self, query) -> Cursor:
        """ async_ is a misnomer here.  It does execute in a new thread
            however it will also wait for execution to complete. At this time
            this helps us with registering KeyboardInterrupt during cyanodbc.
            execute only; it may evolve to have more true async-like behavior.
            """
        self.close_cursor()
        exec_event = Event()
        self._execution_thread = Thread(
                target = self.execute,
                kwargs = {"query": query, "parameters": None, "event": exec_event},
                daemon = True)
        self._execution_thread.start()
        # Will block but can be interrupted
        exec_event.wait()
        return self.cursor

    def list_catalogs(self) -> list:
        # pyodbc note
        # return conn.cursor().tables(catalog = "%").fetchall()
        res = []
        if self.status != connStatus.IDLE:
            return res
        try:
            if self.conn.connected():
                self.logger.debug("Calling list_catalogs...")
                with self._lock:
                    res = self.conn.list_catalogs()
                self.logger.debug("list_catalogs: done")
        except DatabaseError as e:
            self.update_status(connStatus.ERROR)
            self.logger.warning("list_catalogs: %s", str(e))

        return res

    def list_schemas(self, catalog = None) -> list:
        res = []

        # We only trust this generic implementation if attempting to list
        # schemata in curent catalog (or catalog argument is None)
        if catalog is not None and not catalog == self.current_catalog():
            return res

        if self.status != connStatus.IDLE:
            return res
        try:
            if self.conn.connected():
                self.logger.debug("Calling list_schemas...")
                with self._lock:
                    res = self.conn.list_schemas()
                self.logger.debug("list_schemas: done")
        except DatabaseError as e:
            self.update_status(connStatus.ERROR)
            self.logger.warning("list_schemas: %s", str(e))

        return res

    def find_tables(
            self,
            catalog = "",
            schema = "",
            table = "",
            type = "") -> list:
        res = []

        if self.status != connStatus.IDLE:
            return res
        try:
            if self.conn.connected():
                self.logger.debug("Calling find_tables: %s, %s, %s, %s",
                        catalog, schema, table, type)
                with self._lock:
                    res = self.conn.find_tables(
                        catalog = catalog,
                        schema = schema,
                        table = table,
                        type = type)
                self.logger.debug("find_tables: done")
        except DatabaseError as e:
            self.logger.warning("find_tables: %s.%s.%s, type %s: %s", catalog, schema, table, type, str(e))

        return res

    def find_columns(
            self,
            catalog = "",
            schema = "",
            table = "",
            column = "") -> list:
        res = []
        if self.status != connStatus.IDLE:
            return res

        try:
            if self.conn.connected():
                self.logger.debug("Calling find_columns: %s, %s, %s, %s",
                        catalog, schema, table, column)
                with self._lock:
                    res = self.conn.find_columns(
                            catalog = catalog,
                            schema = schema,
                            table = table,
                            column = column)
                self.logger.debug("find_columns: done")
        except DatabaseError as e:
            self.logger.warning("find_columns: %s.%s.%s, column %s: %s", catalog, schema, table, column, str(e))

        return res

    def find_procedures(
            self,
            catalog = "",
            schema = "",
            procedure = "") -> list:
        res = []

        if self.status != connStatus.IDLE:
            return res

        try:
            if self.conn.connected():
                self.logger.debug("Calling find_procedures: %s, %s, %s",
                        catalog, schema, procedure)
                with self._lock:
                    res = self.conn.find_procedures(
                        catalog = catalog,
                        schema = schema,
                        procedure = procedure)
                self.logger.debug("find_procedures: done")
        except DatabaseError as e:
            self.logger.warning("find_procedures: %s.%s.%s: %s", catalog, schema, procedure, str(e))

        return res

    def find_procedure_columns(
            self,
            catalog = "",
            schema = "",
            procedure = "",
            column = "") -> list:
        res = []

        if self.status != connStatus.IDLE:
            return res

        try:
            if self.conn.connected():
                self.logger.debug("Calling find_procedure_columns: %s, %s, %s, %s",
                        catalog, schema, procedure, column)
                with self._lock:
                    res = self.conn.find_procedure_columns(
                            catalog = catalog,
                            schema = schema,
                            procedure = procedure,
                            column = column)
                self.logger.debug("find_procedure_columns: done")
        except DatabaseError as e:
            self.logger.warning("find_procedure_columns: %s.%s.%s, column %s: %s", catalog, schema, procedure, column, str(e))

        return res

    def current_catalog(self) -> str:
        if self.conn.connected():
            return self.conn.catalog_name
        return None

    def connected(self) -> bool:
        return self.conn.connected()

    def catalog_support(self) -> bool:
        res = self.conn.get_info(SQLGetInfo.SQL_CATALOG_NAME)
        return res == True or res == 'Y'
        # pyodbc note
        # return self.conn.getinfo(pyodbc.SQL_CATALOG_NAME) == True or self.conn.getinfo(pyodbc.SQL_CATALOG_NAME) == 'Y'

    def get_info(self, code: int) -> str:
        return self.conn.get_info(code)

    def close(self) -> None:
        self.logger.debug("close ...")
        # TODO: When disconnecting
        # We likely don't want to allow any exception to
        # propagate.  Catch DatabaseError?
        if self.conn.connected():
            self.conn.close()

    def close_cursor(self) -> None:
        self.logger.debug("Close cursor ...")
        self.cancel_async_fetchall()
        if self.cursor:
            with self._lock:
                self.cursor.close()
                self.cursor = None
        self.query = None
        self.update_status(connStatus.IDLE)

    def cancel(self) -> None:
        self.logger.debug("cancel ...")
        self.cancel_async_fetchall()
        if self.cursor:
            # Should not hold _lock here.  Point here is to cancel execution
            # that might be taking place in a separate thread where the execution
            # lock is being held
            self.cursor.cancel()
        if self._execution_thread.is_alive():
            self._execution_thread.join()
        self.query = None
        self.update_status(connStatus.IDLE)

    def preview_query(
            self,
            name,
            obj_type = "table",
            filter_query = "",
            limit = -1) -> str:
        """ Currently we only have a generic implementation for tables and
            views.  Otherwise (functions) we return None
            """
        if obj_type == "table" or obj_type == "view":
            qry = "SELECT * FROM " + name + " " + filter_query
            if limit > 0:
                qry = qry + " LIMIT " + str(limit)
        else:
            qry = None
        return qry

    def formatted_fetch(self, size, cols, format_name = "psql"):
        while True:
            res = self.fetch_from_cache(size, wait = True)
            if len(res) < 1 and self.status != connStatus.FETCHING:
                break
            if len(res) > 0:
                yield "\n".join(
                        formatter.format_output(
                            res,
                            cols,
                            format_name = format_name))

connWrappers = {}

class MSSQL(sqlConnection):
    def find_tables(
            self,
            catalog = "",
            schema = "",
            table = "",
            type = "") -> list:
        """ FreeTDS does not allow us to query catalog == '', and
            schema = '' which, according to the ODBC spec for SQLTables should
            return tables outside of any catalog/schema.  In the case of FreeTDS
            what gets passed to the sp_tables sproc is null, which in turn
            is interpreted as a wildcard.  For the time being intercept
            these queries here (used in auto completion) and return empty
            set.  """

        if catalog == "\x00" and schema == "\x00":
            return []

        return super().find_tables(
                catalog = catalog,
                schema = schema,
                table = table,
                type = type)

    def list_schemas(self, catalog = None) -> list:
        """ Optimization for listing out-of-database schemas by
            always querying catalog.sys.schemas. """
        res = []
        if self.status != connStatus.IDLE:
            return res

        qry = "SELECT name FROM {catalog}.sys.schemas " \
              "WHERE name NOT IN ('db_owner', 'db_accessadmin', " \
              "'db_securityadmin', 'db_ddladmin', 'db_backupoperator', " \
              "'db_datareader', 'db_datawriter', 'db_denydatareader', " \
              "'db_denydatawriter')"

        if catalog is None and self.current_catalog():
            catalog_local = self.current_catalog()
        else:
            # We are going to be outright executing, versus
            # using the ODBC API.
            # let's make sure there is nothing escaped here
            catalog_local = self.unsanitize_search_string(catalog)

        if catalog_local:
            try:
                self.logger.debug("Calling list_schemas...")
                crsr = self.execute(qry.format(catalog = catalog_local))
                res = crsr.fetchall()
                crsr.close()
                self.logger.debug("Calling list_schemas: done")
                schemas = [r[0] for r in res]
                if len(schemas):
                    return schemas
            except DatabaseError as e:
                # execute has an exception handler, but the cursor calls may
                # throw
                self.close_cursor()
                self.logger.warning("MSSQL list_schemas: %s", str(e))

        return super().list_schemas(catalog = catalog)

    def preview_query(
            self,
            name,
            obj_type = "table",
            filter_query = "",
            limit = -1) -> str:

        if obj_type == "table" or obj_type == "view":
            qry = " * FROM " + name + " " + filter_query
            if limit > 0:
                qry = "SELECT TOP " + str(limit) + qry
            else:
                qry = "SELECT" + qry
        elif obj_type == "function":
            # Sproc names in SQLServer come back with
            # catalog.schema.name;INT with the trailing suffix
            # not useful
            name_sanitized = sub("(;\\d{0,})(\")$", "\\2", name)
            catalog_local = sub("(.*)\\.(.*)\\.(.*)", "\\1", name)
            qry = "SELECT definition FROM {catalog}.sys.sql_modules " \
                  "WHERE object_id = (OBJECT_ID(N'{name}'))"
            qry = qry.format(catalog = catalog_local, name = name_sanitized)
        else:
            qry = None

        return qry

class PSSQL(sqlConnection):
    def find_tables(
            self,
            catalog = "",
            schema = "",
            table = "",
            type = "") -> list:
        """ At least the psql odbc driver I am using has an annoying habbit
            of treating the catalog and schema fields interchangible, which
            in turn screws up with completion"""

        if not catalog in [self.current_catalog(), self.sanitize_search_string(self.current_catalog())]:
            return []

        return super().find_tables(
                catalog = catalog,
                schema = schema,
                table = table,
                type = type)

    def find_procedures(
            self,
            catalog = "",
            schema = "",
            procedure = "") -> list:
        """ At least the psql odbc driver I am using has an annoying habbit
            of treating the catalog and schema fields interchangible, which
            in turn screws up with completion"""

        if not catalog in [self.current_catalog(), self.sanitize_search_string(self.current_catalog())]:
            return []

        return super().find_procedures(
                catalog = catalog,
                schema = schema,
                procedure = procedure)

    def find_columns(
            self,
            catalog = "",
            schema = "",
            table = "",
            column = "") -> list:
        """ At least the psql odbc driver I am using has an annoying habbit
            of treating the catalog and schema fields interchangible, which
            in turn screws up with completion"""

        if not catalog in [self.current_catalog(), self.sanitize_search_string(self.current_catalog())]:
            return []

        return super().find_columns(
                catalog = catalog,
                schema = schema,
                table = table,
                column = column)

    def find_procedure_columns(
            self,
            catalog = "",
            schema = "",
            procedure = "",
            column = "") -> list:
        """ At least the psql odbc driver I am using has an annoying habbit
            of treating the catalog and schema fields interchangible, which
            in turn screws up with completion.  In addition wildcards in the column
            field, seem to not work - but an empty string does."""

        if not catalog in [self.current_catalog(), self.sanitize_search_string(self.current_catalog())]:
            return []

        if column == "%":
            column = ""

        return super().find_procedure_columns(
                catalog = catalog,
                schema = schema,
                procedure = procedure,
                column = column)

class MySQL(sqlConnection):

    def list_schemas(self, catalog = None) -> list:
        """ Only catalogs for MySQL, it seems,
            however, list_schemas returns [""] which
            causes blank entries to show up in auto
            completion.  Also confuses some of the checks we have
            that look for len(list_schemas) < 1 to decide whether
            to fall-back to find_tables.  Make sure that for MySQL
            we do, in-fact fall-back to find_tables"""
        return []

    def current_catalog(self) -> str:
        if self.conn.connected():
            res = self.conn.catalog_name
        if res == "null":
            res = ""
        return res

class Snowflake(sqlConnection):

    def find_tables(
            self,
            catalog = "",
            schema = "",
            table = "",
            type = "") -> list:

        type = type.upper()
        return super().find_tables(
                catalog = catalog,
                schema = schema,
                table = table,
                type = type)

connWrappers["MySQL"] = MySQL
connWrappers["Microsoft SQL Server"] = MSSQL
connWrappers["PostgreSQL"] = PSSQL
connWrappers["Snowflake"] = Snowflake
