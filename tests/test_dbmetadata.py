from odbcli.dbmetadata import DbMetadata
import pytest

def test_catalogs():
    db = DbMetadata()
    cats = ["a", "b", "c", ""]
    res = db.get_catalogs()
    assert res is None
    db.extend_catalogs(cats)
    res = db.get_catalogs(obj_type = "table")
    assert res == cats
    res = db.get_catalogs(obj_type = "view")
    assert res == cats
    res = db.get_catalogs(obj_type = "function")
    assert res == cats

def test_schemas():
    db = DbMetadata()
    cats = ["a", "b", "c", ""]
    schemas = ["A", "B", "C", ""]
    db.extend_catalogs(cats)
    res = db.get_schemas(catalog = "d")
    assert res is None
    res = db.get_schemas(catalog = "a")
    assert res == []
    db.extend_schemas(catalog = "d", names = schemas)
    db.extend_schemas(catalog = "", names = schemas)
    res = db.get_catalogs(obj_type = "table")
    assert "d" in res
    res = db.get_schemas(catalog = "d")
    assert res == schemas
    res = db.get_schemas(catalog = "")
    assert res == schemas

def test_objects():
    db = DbMetadata()
    cats = ["a", "b", "c", ""]
    schemas = ["A", "B", "C", ""]
    tables = ["t1", "t2", "t3"]
    views = ["v1", "v2", "v3"]
    db.extend_catalogs(cats)
    db.extend_schemas(catalog = "a", names = schemas)
    res = db.get_objects(catalog = "a", schema = "D")
    assert res is None
    res = db.get_objects(catalog = "a", schema = "A")
    assert res == []
    db.extend_objects(catalog = "a", schema = "A", names = tables, obj_type = "table")
    db.extend_objects(catalog = "a", schema = "D", names = tables, obj_type = "table")
    res = db.get_objects(catalog = "a", schema = "A")
    assert res == tables
    res = db.get_objects(catalog = "a", schema = "D")
    assert res == tables
    res = db.get_objects(catalog = "a", schema = "D", obj_type = "view")
    assert res == []
    db.extend_objects(catalog = "a", schema = "A", names = views, obj_type = "view")
    res = db.get_objects(catalog = "a", schema = "A", obj_type = "view")
    assert res == views
    res = db.get_objects(catalog = "a", schema = "A", obj_type = "table")
    assert res == tables
