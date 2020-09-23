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

    def extend_schemas(self, catalog, names: list) -> None:
        if len(names):
            with self._lock:
                for metadata in self._dbmetadata.values():
                    metadata[catalog] = {}
                    for schema in names:
                        metadata[catalog].update(dict.fromkeys(names, {}))
        return

    def extend_objects(self, catalog, schema, names: list, obj_type: str) -> None:
        if len(names):
            with self._lock:
                metadata = self._dbmetadata[obj_type]
                if catalog in metadata.keys() and schema in metadata[catalog].keys():
                    metadata[catalog][schema] = {}
                    metadata[catalog][schema].update(dict.fromkeys(names, {}))
        return

    def reset_metadata(self) -> None:
        with self._lock:
            self._dbmetadata = {'table': {}, 'view': {}, 'function': {},
                               'datatype': {}}

    @property
    def data(self) -> dict:
        return self._dbmetadata
