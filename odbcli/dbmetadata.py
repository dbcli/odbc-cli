from threading import Lock, Event, Thread

class DbMetadata():
    def __init__(self) -> None:
        self._lock = Lock()
        self._dbmetadata = {'table': {}, 'view': {}, 'function': {},
                'datatype': {}}

    def extend_catalogs(self, names: list) -> None:
        with self._lock:
            for metadata in self._dbmetadata.values():
                metadata.update(dict.fromkeys(names, {}))
        return

    def get_catalogs(self, obj_type: str = "table") -> list:
        """ Retrieve catalogs as the keys for _dbmetadata[obj_type]
            If no keys are found it returns None.
        """
        with self._lock:
            res = self._dbmetadata[obj_type].keys()

        if len(res) == 0:
            return None

        return list(res)

    def extend_schemas(self, catalog, names: list) -> None:
        """ This method will force/create [catalog] dictionary
            in the event that len(names) > 0, and overwrite if
            anything was there to begin with.
        """
        if len(names):
            with self._lock:
                for metadata in self._dbmetadata.values():
                    metadata[catalog] = {}
                    for schema in names:
                        metadata[catalog].update(dict.fromkeys(names, {}))
        return

    def get_schemas(self, catalog: str, obj_type: str = "table") -> list:
        """ Retrieve schemas as the keys for _dbmetadata[obj_type][catalog]
            If catalog is not part of the _dbmetadata[obj_type] keys will return
            None.
        """

        cats = self.get_catalogs(obj_type = obj_type)
        if cats is None or catalog not in cats:
            return None

        with self._lock:
            res = self._dbmetadata[obj_type][catalog].keys()

        return list(res)

    def extend_objects(self, catalog, schema, names: list, obj_type: str) -> None:
        if len(names):
            with self._lock:
                for metadata in self._dbmetadata.values():
                    if catalog not in metadata.keys():
                        metadata[catalog] = {}
                    if schema not in metadata[catalog].keys():
                        metadata[catalog][schema] = {}
                self._dbmetadata[obj_type][catalog][schema].update(dict.fromkeys(names, {}))
        return

    def get_objects(self, catalog: str, schema: str, obj_type: str = "table") -> list:
        """ Retrieve objects as the keys for _dbmetadata[obj_type][catalog][schema]
            If catalog is not part of the _dbmetadata[obj_type] keys, or schema
            not one of the keys in _dbmetadata[obj_type][catalog] will return None
        """

        schemas = self.get_schemas(catalog = catalog, obj_type = obj_type)
        if schemas is None or schema not in schemas:
            return None

        with self._lock:
            res = self._dbmetadata[obj_type][catalog][schema].keys()

        return list(res)

    def reset_metadata(self) -> None:
        with self._lock:
            self._dbmetadata = {'table': {}, 'view': {}, 'function': {},
                               'datatype': {}}

    @property
    def data(self) -> dict:
        return self._dbmetadata
