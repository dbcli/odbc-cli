from threading import Lock, Event, Thread

class DbMetadata():
    """
        Internal representation of a database.  The structure is that of a nested
        dictionary, with the top nodes being "table", "view", "function", and
        "datatype".   From there the structure is:
        "table" = {..., "catalog.lower" = (catalog, {...., "schema".lower = (...)})
        Some notes:
          * All identifiers (catalog, schema, table, column) are quoted using
          the quoting character for the connection.
          * The name of a node is *.lower to allow for case insensitive search
          through the connection metadata.
    """
    def __init__(self, conn: "sqlConnection") -> None:
        self._conn = conn
        self._lock = Lock()
        self._dbmetadata = {'table': {}, 'view': {}, 'function': {},
                'datatype': {}}

    def escape_name(self, name):
        if self._conn.connected():
            name = self._conn.escape_name(name)

        return name

    def escape_names(self, names):
        if self._conn.connected():
            names = self._conn.escape_names(names)

        return names

    def unescape_name(self, name):
        """ Unquote a string."""
        if self._conn.connected():
            name = self._conn.unescape_name(name)
        return name

    def current_catalog(self):
        if self._conn.connected():
            return self._conn.current_catalog()

        return None

    def extend_catalogs(self, names: list) -> None:
        if len(names):
            # Add "" catalog to house tables without catalog / schema
            names.append("")
            with self._lock:
                for metadata in self._dbmetadata.values():
                    for catalog in names:
                        metadata[catalog.lower()] = (catalog, {})
        return

    def get_catalogs(self, obj_type: str = "table", cased: bool = True) -> list:
        """ Retrieve catalogs as the keys for _dbmetadata[obj_type]
            If no keys are found it returns None.
        """
        with self._lock:
            if cased:
                res = [casedkey for casedkey, mappedvalue in self._dbmetadata[obj_type].values()]
            else:
                res = list(self._dbmetadata[obj_type].keys())

        if len(res) == 0 and self._conn.connected():
            res = self.escape_names(self._conn.list_catalogs())
            self.extend_catalogs(res)
            # TODO: Should we recursively call get_catalogs here so as to be
            # able to respect the cased argument?

        return res

    def extend_schemas(self, catalog, names: list) -> None:
        """ This method will force/create [catalog] dictionary
            in the event that len(names) > 0, and overwrite if
            anything was there to begin with.
        """
        catlower = catalog.lower()
        cat_cased = catalog
        if len(names):
            # Add "" schema to house tables without schema
            names.append("")
            with self._lock:
                for metadata in self._dbmetadata.values():
                    # Preserve casing if an entry already there
                    if catlower in metadata.keys() and len(metadata[catlower]):
                        cat_cased = metadata[catlower][0]
                    metadata[catlower] = (cat_cased, {})
                    for schema in names:
                        metadata[catlower][1][schema.lower()] = (schema, {})
        # If we passed nothing then take out that element entirely out
        # of the dict
        else:
            with self._lock:
                for otype in self._dbmetadata.keys():
                    try:
                        del self._dbmetadata[otype][catlower]
                    except KeyError:
                        pass
        return

    def get_schemas(self, catalog: str, obj_type: str = "table", cased: bool = True) -> list:
        """ Retrieve schemas as the keys for _dbmetadata[obj_type][catalog]
            If catalog is not part of the _dbmetadata[obj_type] keys will return
            None.
        """

        catlower = catalog.lower()
        cats = self.get_catalogs(obj_type = obj_type, cased = False)
        if cats is None or catlower not in cats:
            return None

        with self._lock:
            if cased:
                res = [casedkey for casedkey, mappedvalue in self._dbmetadata[obj_type][catlower][1].values()]
            else:
                res = list(self._dbmetadata[obj_type][catlower][1].keys())

        if len(res) == 0 and self._conn.connected():
            # Looking for schemas in a specified catalog
            res_u = []
            catalog_u = self.unescape_name(catalog)
            # Attempt list_schemas
            res_u = self._conn.list_schemas(
                    catalog = self._conn.sanitize_search_string(catalog_u))

            if len(res_u) < 1:
                res_u = self._conn.find_tables(
                        catalog = self._conn.sanitize_search_string(catalog_u),
                        schema = "",
                        table = "",
                        type = "")
                res_u = [r.schema for r in res_u]

            res = self.escape_names(res_u)
            self.extend_schemas(catalog = catalog, names = res)
            # TODO: Should we recursively call get_schemas here so as to be
            # able to respect the cased argument?

        return res

    def extend_objects(self, catalog, schema, names: list, obj_type: str) -> None:
        catlower = catalog.lower()
        schlower = schema.lower()
        if len(names):
            with self._lock:
                for otype in self._dbmetadata.keys():
                    # Loop over tables, views, functions
                    if catlower not in self._dbmetadata[otype].keys():
                        self._dbmetadata[otype][catlower] = (catalog, {})
                    if schlower not in self._dbmetadata[otype][catlower][1].keys():
                        self._dbmetadata[otype][catlower][1][schlower] = (schema, {})
                for obj in names:
                    self._dbmetadata[obj_type][catlower][1][schlower][1][obj.lower()] = (obj, {})
        # If we passed nothing then take out that element entirely out
        # of the dict
        else:
            with self._lock:
                try:
                    del self._dbmetadata[obj_type][catlower][1][schlower]
                except KeyError:
                    pass

        return

    def get_objects(self, catalog: str, schema: str, obj_type: str = "table", cased: bool = True) -> list:
        """ Retrieve objects as the keys for _dbmetadata[obj_type][catalog][schema]
            If catalog is not part of the _dbmetadata[obj_type] keys, or schema
            not one of the keys in _dbmetadata[obj_type][catalog] will return None
        """

        catlower = catalog.lower()
        schlower = schema.lower()
        schemas = self.get_schemas(catalog = catalog, obj_type = obj_type, cased = False)
        if (schemas is None or schlower not in schemas):
            return None

        res = []
        with self._lock:
            if cased:
                res = [casedkey for casedkey, mappedvalue in self._dbmetadata[obj_type][catlower][1][schlower][1].values()]
            else:
                res = list(self._dbmetadata[obj_type][catlower][1][schlower][1].keys())

        if len(res) == 0 and self._conn.connected():
            # Special case: Look for tables without catalog/schema
            res = []
            if catalog == "" and schema == "":
                res_u = self._conn.find_tables(
                        catalog = "\x00",
                        schema = "\x00",
                        table = "",
                        type = obj_type)
            else:
                res_u = self._conn.find_tables(
                        catalog = self._conn.sanitize_search_string(
                            self.unescape_name(catalog)),
                        schema = self._conn.sanitize_search_string(
                            self.unescape_name(schema)),
                        table = "",
                        type = obj_type)
            res_u = [r.name for r in res_u]
            res = self.escape_names(res_u)

            self.extend_objects(
                    catalog = catalog, schema = schema,
                    names = res, obj_type = obj_type)
            # TODO: Should we recursively call get_objects here so as to make
            # sure we are returning the correct spec

        return res

    def extend_columns(self, catalog, schema, name, cols: list, obj_type: str) -> None:
        catlower = catalog.lower()
        schlower = schema.lower()
        nmlower = name.lower()
        if len(cols):
            with self._lock:
                for otype in self._dbmetadata.keys():
                    # Loop over tables, views, functions
                    if catlower not in self._dbmetadata[otype].keys():
                        self._dbmetadata[otype][catlower] = (catalog, {})
                    if schlower not in self._dbmetadata[otype][catlower][1].keys():
                        self._dbmetadata[otype][catlower][1][schlower] = (schema, {})
                for col in cols:
                    try:
                        self._dbmetadata[obj_type][catlower][1][schlower][1][nmlower][1][col.column.lower()] = (col.column, col)
                    except KeyError:
                        pass
        # If we passed nothing then take out that element entirely out
        # of the dict
        else:
            with self._lock:
                try:
                    del self._dbmetadata[obj_type][catlower][1][schlower]
                except KeyError:
                    pass

        return

    def get_columns(self, catalog: str, schema: str, name: str, obj_type: str = "table") -> list:
        """
            Returns a list of named tuples.  See cyanodbc.connection.find_columns
        """
        catlower = catalog.lower()
        schlower = schema.lower()
        nmlower = name.lower()
        objs = self.get_objects(catalog = catalog, schema = schema,
                obj_type = obj_type, cased = False) # TODO add cased argument to get_objects
        if (objs is None or nmlower not in objs):
            return None

        res = []
        with self._lock:
            res = [mappedvalue for casedkey, mappedvalue in self._dbmetadata[obj_type][catlower][1][schlower][1][nmlower][1].values()]

        if len(res) == 0 and self._conn.connected():
            # res is a collections.namedtuple object
            res = self._conn.find_columns(
                    # Per SQLColumns spec: CatalogName cannot contain a
                    # string search pattern.  But should we sanitize
                    # regardless?
                    catalog = self.unescape_name(catalog),
                    schema = self._conn.sanitize_search_string(
                        self.unescape_name(schema)),
                    table = self._conn.sanitize_search_string(
                        self.unescape_name(name)),
                    column = "%")

            self.extend_columns(
                    catalog = catalog, schema = schema, name = name,
                    cols = res, obj_type = obj_type)
            # TODO: Should we recursively call get_columns here so as to make
            # sure we are returning the correct spec

        return res

    def reset_metadata(self) -> None:
        with self._lock:
            self._dbmetadata = {'table': {}, 'view': {}, 'function': {},
                               'datatype': {}}

    @property
    def data(self) -> dict:
        return self._dbmetadata
