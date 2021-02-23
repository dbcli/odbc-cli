import sys
import platform
from cyanodbc import Connection
from typing import List, Optional, Callable
from logging import getLogger
from asyncio import get_event_loop
from threading import Thread, Lock
from prompt_toolkit.layout.containers import HSplit, Window, ScrollOffsets, ConditionalContainer, Container
from prompt_toolkit.formatted_text.base import StyleAndTextTuples
from prompt_toolkit.formatted_text import fragment_list_width
from prompt_toolkit.layout.controls import FormattedTextControl, BufferControl, UIContent
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import is_done, renderer_height_is_known
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.mouse_events import MouseEvent
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.widgets import SearchToolbar
from prompt_toolkit.filters import Condition
from .conn import sqlConnection
from .filters import ShowSidebar
from .utils import if_mousedown
from .__init__ import __version__

class myDBObject:
    def __init__(
        self,
        my_app: "sqlApp",
        conn: sqlConnection,
        name: str,
        otype: str,
        level: Optional[int] = 0,
        children: Optional[List["myDBObject"]] = None,
        parent: Optional["myDBObject"] = None,
        next_object: Optional["myDBObject"] = None
    ) -> None:

        self.my_app = my_app
        self.conn = conn
        self.children = children
        self.parent = parent
        self.next_object = next_object
        # Held while modifying children, parent, next_object
        # As some of thes operatins (expand) happen asynchronously
        self._lock = Lock()
        self.name = name
        self.otype = otype
        self.level = level
        self.selected: bool = False

    def _expand_internal(self) -> None:
        """
        Populates children and sets parent for children nodes
        """
        raise NotImplementedError()

    def expand(self) -> None:
        """
        Populates children and sets parent for children nodes
        """
        if self.children is not None:
            return None

        loop = get_event_loop()
        self.my_app.show_expanding_object = True
        self.my_app.application.invalidate()
        def _redraw_after_io():
            """ Callback, scheduled after threaded I/O
                completes """
            self.my_app.show_expanding_object = False
            self.my_app.obj_list_changed = True
            self.my_app.application.invalidate()

        def _run():
            """ Executes in a thread """
            self._expand_internal() # Blocking I/O
            loop.call_soon_threadsafe(_redraw_after_io)

        # (Don't use 'run_in_executor', because daemon is ideal here.
        t = Thread(target = _run, daemon = True)
        t.start()

    def collapse(self) -> None:
        """
        Populates children and sets parent for children nodes
        Note, we don't have to blow up the children; just redirect
        next_object.  This way we re-query the database / force re-fresh
        which may be suboptimal.  TODO: Codify not/refresh path
        """
        if self is not self.my_app.selected_object:
            return
        if self.children is not None:
            obj = self.children[len(self.children) - 1].next_object
            while obj.level > self.level:
                obj = obj.next_object
            with self._lock:
                self.next_object = obj
                self.children = None
        elif self.parent is not None:
            self.my_app.selected_object = self.parent
            self.parent.collapse()

        self.my_app.obj_list_changed = True

    def add_children(self, list_obj: List["myDBObject"]) -> None:
        lst = list(filter(lambda x: x.name != "", list_obj))
        if len(lst):
            with self._lock:
                self.children = lst
                for i in range(len(self.children) - 1):
                    self.children[i].next_object = self.children[i + 1]
                self.children[len(self.children) - 1].next_object = self.next_object
                self.next_object = self.children[0]

class myDBColumn(myDBObject):
    def _expand_internal(self) -> None:
        return None

class myDBFunction(myDBObject):
    def _expand_internal(self) -> None:
        cat = "%"
        schema = "%"
        # https://docs.microsoft.com/en-us/sql/odbc/reference/syntax/sqlprocedurecolumns-function?view=sql-server-ver15
        # CatalogName cannot contain a string search pattern

        if self.parent is not None:
            if type(self.parent).__name__ == "myDBSchema":
                schema = self.conn.sanitize_search_string(self.parent.name)
            elif type(self.parent).__name__ == "myDBCatalog":
                cat = self.parent.name
            if self.parent.parent is not None:
                if type(self.parent.parent).__name__ == "myDBCatalog":
                    cat = self.parent.parent.name

        res = self.conn.find_procedure_columns(
                catalog = cat,
                schema = schema,
                procedure = self.conn.sanitize_search_string(self.name),
                column = "%")

        lst = [myDBColumn(
            my_app = self.my_app,
            conn = self.conn,
            name = col.column,
            otype = col.type_name,
            parent = self,
            level = self.level + 1) for col in res]

        self.add_children(list_obj = lst)
        return None

class myDBTable(myDBObject):
    def _expand_internal(self) -> None:
        cat = "%"
        schema = "%"
        # https://docs.microsoft.com/en-us/sql/odbc/reference/syntax/sqlcolumns-function?view=sql-server-ver15
        # CatalogName cannot contain a string search pattern

        if self.parent is not None:
            if type(self.parent).__name__ == "myDBSchema":
                schema = self.conn.sanitize_search_string(self.parent.name)
            elif type(self.parent).__name__ == "myDBCatalog":
                cat = self.parent.name
            if self.parent.parent is not None:
                if type(self.parent.parent).__name__ == "myDBCatalog":
                    cat = self.parent.parent.name

        res = self.conn.find_columns(
                catalog = cat,
                schema = schema,
                table = self.name,
                column = "%")

        lst = [myDBColumn(
            my_app = self.my_app,
            conn = self.conn,
            name = col.column,
            otype = col.type_name,
            parent = self,
            level = self.level + 1) for col in res]

        self.add_children(list_obj = lst)
        return None

class myDBSchema(myDBObject):
    def _expand_internal(self) -> None:

        cat = self.conn.sanitize_search_string(self.parent.name) if self.parent is not None else "%"
        res = self.conn.find_tables(
                catalog = cat,
                schema = self.conn.sanitize_search_string(self.name),
                table = "",
                type = "")
        resf = self.conn.find_procedures(
                catalog = cat,
                schema = self.conn.sanitize_search_string(self.name),
                procedure = "")
        tables = []
        views = []
        functions = []
        lst = []
        for table in res:
            if table.type.lower() == 'table':
                tables.append(table.name)
            if table.type.lower() == 'view':
                views.append(table.name)
            lst.append(myDBTable(
                my_app = self.my_app,
                conn = self.conn,
                name = table.name,
                otype = table.type.lower(),
                parent = self,
                level = self.level + 1))
        for func in resf:
            functions.append(func.name)
            lst.append(myDBFunction(
                my_app = self.my_app,
                conn = self.conn,
                name = func.name,
                otype = "function",
                parent = self,
                level = self.level + 1))

        self.conn.dbmetadata.extend_objects(
                catalog = self.conn.escape_name(self.parent.name) if self.parent else "",
                schema = self.conn.escape_name(self.name),
                names = self.conn.escape_names(tables),
                obj_type = "table")
        self.conn.dbmetadata.extend_objects(
                catalog = self.conn.escape_name(self.parent.name) if self.parent else "",
                schema = self.conn.escape_name(self.name),
                names = self.conn.escape_names(views),
                obj_type = "view")
        self.conn.dbmetadata.extend_objects(
                catalog = self.conn.escape_name(self.parent.name) if self.parent else "",
                schema = self.conn.escape_name(self.name),
                names = self.conn.escape_names(functions),
                obj_type = "function")
        self.add_children(list_obj = lst)
        return None

class myDBCatalog(myDBObject):
    def _expand_internal(self) -> None:
        schemas = lst = []
        schemas = self.conn.list_schemas(
                catalog = self.conn.sanitize_search_string(self.name))

        if len(schemas) < 1 or all([s == "" for s in schemas]):
            res = self.conn.find_tables(
                    catalog = self.conn.sanitize_search_string(self.name),
                    schema = "",
                    table = "",
                    type = "")
            schemas = [r.schema for r in res]

        self.conn.dbmetadata.extend_schemas(
                catalog = self.conn.escape_name(self.name),
                names = self.conn.escape_names(schemas))

        if not all([s == "" for s in schemas]):
            # Schemas were found either having called list_schemas
            # or via the find_tables call
            lst = [myDBSchema(
                my_app = self.my_app,
                conn = self.conn,
                name = schema,
                otype = "schema",
                parent = self,
                level = self.level + 1) for schema in sorted(set(schemas))]
        elif len(schemas):
            # No schemas found; but if there are tables then these are direct
            # descendents, i.e. MySQL
            tables = []
            views = []
            lst = []
            for table in res:
                if table.type.lower() == 'table':
                    tables.append(table.name)
                if table.type.lower() == 'view':
                    views.append(table.name)
                lst.append(myDBTable(
                    my_app = self.my_app,
                    conn = self.conn,
                    name = table.name,
                    otype = table.type.lower(),
                    parent = self,
                    level = self.level + 1))
            self.conn.dbmetadata.extend_objects(
                    catalog = self.conn.escape_name(self.name),
                    schema = "", names = self.conn.escape_names(tables),
                    obj_type = "table")
            self.conn.dbmetadata.extend_objects(
                    catalog = self.conn.escape_name(self.name),
                    schema = "", names = self.conn.escape_names(views),
                    obj_type = "view")

        self.add_children(list_obj = lst)
        return None


class myDBConn(myDBObject):
    def _expand_internal(self) -> None:
        if not self.conn.connected():
            return None

        lst = []
        cat_support = self.conn.catalog_support()
        if cat_support:
            rows = self.conn.list_catalogs()
            if len(rows):
                lst = [myDBCatalog(
                    my_app = self.my_app,
                    conn = self.conn,
                    name = row,
                    otype = "catalog",
                    parent = self,
                    level = self.level + 1) for row in rows]
                self.conn.dbmetadata.extend_catalogs(
                        self.conn.escape_names(rows))
        else:
            res = self.conn.find_tables(
                    catalog = "%",
                    schema = "",
                    table = "",
                    type = "")
            schemas = [r.schema for r in res]
            self.conn.dbmetadata.extend_schemas(catalog = "",
                    names = self.conn.escape_names(schemas))
            if not all([s == "" for s in schemas]):
                lst = [myDBSchema(
                    my_app = self.my_app,
                    conn = self.conn,
                    name = schema,
                    otype = "schema",
                    parent = self,
                    level = self.level + 1) for schema in sorted(set(schemas))]
            elif len(schemas):
                tables = []
                views = []
                lst = []
                for table in res:
                    if table.type.lower() == 'table':
                        tables.append(table.name)
                    if table.type.lower() == 'view':
                        views.append(table.name)
                    lst.append(myDBTable(
                    my_app = self.my_app,
                    conn = self.conn,
                    name = table.name,
                    otype = table.type.lower(),
                    parent = self,
                    level = self.level + 1))
                self.conn.dbmetadata.extend_objects(catalog = "",
                        schema = "", names = self.conn.escape_names(tables),
                        obj_type = "table")
                self.conn.dbmetadata.extend_objects(catalog = "",
                        schema = "", names = self.conn.escape_names(views),
                        obj_type = "view")
        self.add_children(list_obj = lst)
        return None

def sql_sidebar_help(my_app: "sqlApp"):
    """
    Create the `Layout` for the help text for the current item in the sidebar.
    """
    token = "class:sidebar.helptext"

    def get_current_description():
        """
        Return the description of the selected option.
        """
        obj = my_app.selected_object
        if obj is not None:
            return obj.name
        return ""

    def get_help_text():
        return [(token, get_current_description())]

    return ConditionalContainer(
        content=Window(
            FormattedTextControl(get_help_text), style=token, height=Dimension(min=3)
            ),
        filter = ~is_done
        & ShowSidebar(my_app)
        & Condition(
            lambda: not my_app.show_exit_confirmation
        ))

def expanding_object_notification(my_app: "sqlApp"):
    """
    Create the `Layout` for the 'Expanding object' notification.
    """

    def get_text_fragments():
        # Show navigation info.
        return [("fg:red", "Expanding object ...")]

    return ConditionalContainer(
        content = Window(
            FormattedTextControl(get_text_fragments),
            style = "class:sidebar",
            width=Dimension.exact( 45 ),
            height=Dimension(max = 1),
        ),
        filter = ~is_done
        & ShowSidebar(my_app)
        & Condition(
            lambda: my_app.show_expanding_object
        ))

def sql_sidebar_navigation():
    """
    Create the `Layout` showing the navigation information for the sidebar.
    """

    def get_text_fragments():
        # Show navigation info.
        return [
            ("class:sidebar.navigation", "   "),
            ("class:sidebar.navigation.key", "[Up/Dn]"),
            ("class:sidebar.navigation", " "),
            ("class:sidebar.navigation.description", "Navigate"),
            ("class:sidebar.navigation", " "),
            ("class:sidebar.navigation.key", "[L/R]"),
            ("class:sidebar.navigation", " "),
            ("class:sidebar.navigation.description", "Expand/Collapse"),
            ("class:sidebar.navigation", "\n   "),
            ("class:sidebar.navigation.key", "[Enter]"),
            ("class:sidebar.navigation", " "),
            ("class:sidebar.navigation.description", "Connect/Preview"),
        ]

    return Window(
        FormattedTextControl(get_text_fragments),
        style = "class:sidebar.navigation",
        width=Dimension.exact( 45 ),
        height=Dimension(max = 2),
    )

def show_sidebar_button_info(my_app: "sqlApp") -> Container:
    """
    Create `Layout` for the information in the right-bottom corner.
    (The right part of the status bar.)
    """

    @if_mousedown
    def toggle_sidebar(mouse_event: MouseEvent) -> None:
        " Click handler for the menu. "
        my_app.show_sidebar = not my_app.show_sidebar

    # TO DO: app version rather than python
    version = sys.version_info
    tokens: StyleAndTextTuples = [
            ("class:status-toolbar.key", "[C-t]", toggle_sidebar),
            ("class:status-toolbar", " Object Browser", toggle_sidebar),
            ("class:status-toolbar", " - "),
            ("class:status-toolbar.cli-version", "odbcli %s" % __version__),
            ("class:status-toolbar", " "),
            ]
    width = fragment_list_width(tokens)

    def get_text_fragments() -> StyleAndTextTuples:
        # Python version
        return tokens

    return ConditionalContainer(
            content=Window(
                FormattedTextControl(get_text_fragments),
                style="class:status-toolbar",
                height=Dimension.exact(1),
                width=Dimension.exact(width),
                ),
            filter=~is_done
            & Condition(
                lambda: not my_app.show_exit_confirmation
            )
            & renderer_height_is_known
            )

def sql_sidebar(my_app: "sqlApp") -> Window:
    """
    Create the `Layout` for the sidebar with the configurable objects.
    """

    @if_mousedown
    def expand_item(obj: "myDBObject") -> None:
        obj.expand()

    def tokenize_obj(obj: "myDBObject") -> StyleAndTextTuples:
        " Recursively build the token list "
        tokens: StyleAndTextTuples = []
        selected = obj is my_app.selected_object
        expanded = obj.children is not None
        connected = obj.otype == "Connection" and obj.conn.connected()
        active = my_app.active_conn is not None and my_app.active_conn is obj.conn and obj.level == 0

        act = ",active" if active else ""
        sel = ",selected" if selected else ""
        if len(obj.name) > 24 -  2 * obj.level:
            name_trim = obj.name[:24 - 2 * obj.level - 3] + "..."
        else:
            name_trim = ("%-" + str(24 - 2 * obj.level) + "s") % obj.name

        tokens.append(("class:sidebar.label" + sel + act, " >" if connected else "  "))
        tokens.append(
            ("class:sidebar.label" + sel, " " * 2 * obj.level, expand_item)
        )
        tokens.append(
            ("class:sidebar.label" + sel + act,
            name_trim,
            expand_item)
        )
        tokens.append(("class:sidebar.status" + sel + act, " ", expand_item))
        tokens.append(("class:sidebar.status" + sel + act, "%+12s" % obj.otype, expand_item))

        if selected:
            tokens.append(("[SetCursorPosition]", ""))

        if expanded:
            tokens.append(("class:sidebar.status" + sel + act, "\/"))
        else:
            tokens.append(("class:sidebar.status" + sel + act, " <" if selected else "  "))

        # Expand past the edge of the visible buffer to get an even panel
        tokens.append(("class:sidebar.status" + sel + act, " " * 10))
        return tokens

    search_buffer = Buffer(name = "sidebarsearchbuffer")
    search_field = SearchToolbar(
        search_buffer = search_buffer,
        ignore_case = True
    )
    def _buffer_pos_changed(buff):
        """ This callback gets executed after cursor position changes.  Most
            of the time we register a key-press (up / down), we change the
            selected object and as a result of that the cursor changes.  By that
            time we don't need to updat the selected object (cursor changed as
            a result of the selected object being updated).  The one exception
            is when searching the sidebar buffer.  When this happens the cursor
            moves ahead of the selected object.  When that happens, here we
            update the selected object to follow suit.
        """
        if buff.document.cursor_position_row != my_app.selected_object_idx[0]:
            my_app.select(buff.document.cursor_position_row)

    sidebar_buffer = Buffer(
        name = "sidebarbuffer",
        read_only = True,
        on_cursor_position_changed = _buffer_pos_changed
    )

    class myLexer(Lexer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._obj_list = []

        def add_objects(self, objects: List):
            self._obj_list = objects

        def lex_document(self, document: Document) -> Callable[[int], StyleAndTextTuples]:
            def get_line(lineno: int) -> StyleAndTextTuples:
                # TODO: raise out-of-range exception
                return tokenize_obj(self._obj_list[lineno])
            return get_line


    sidebar_lexer = myLexer()

    class myControl(BufferControl):

        def move_cursor_down(self):
            my_app.select_next()
        # Need to figure out what do do here
        # AFAICT these are only called for the mouse handler
        # when events are otherwise not handled
        def move_cursor_up(self):
            my_app.select_previous()

        def mouse_handler(self, mouse_event: MouseEvent) -> "NotImplementedOrNone":
            """
                There is an intricate relationship between the cursor position
                in the sidebar document and which object is market as 'selected'
                in the linked list.  Let's not muck that up by allowing the user
                to change the cursor position in the sidebar document with the mouse.
            """
            return NotImplemented

        def create_content(self, width: int, height: Optional[int]) -> UIContent:
            # Only traverse the obj_list if it has been expanded / collapsed
            if not my_app.obj_list_changed:
                self.buffer.cursor_position = my_app.selected_object_idx[1]
                return super().create_content(width, height)

            res = []
            obj = my_app.obj_list[0]
            res.append(obj)
            while obj.next_object is not my_app.obj_list[0]:
                res.append(obj.next_object)
                obj = obj.next_object

            self.lexer.add_objects(res)
            self.buffer.set_document(Document(
                text = "\n".join([a.name for a in res]), cursor_position = my_app.selected_object_idx[1]), True)
            # Reset obj_list_changed flag, now that we have had a chance to
            # regenerate the sidebar document content
            my_app.obj_list_changed = False
            return super().create_content(width, height)



    sidebar_control = myControl(
            buffer = sidebar_buffer,
            lexer = sidebar_lexer,
            search_buffer_control = search_field.control,
            focusable = True,
            )

    return HSplit([
        search_field,
        Window(
            sidebar_control,
            right_margins = [ScrollbarMargin(display_arrows = True)],
            style = "class:sidebar",
            width = Dimension.exact( 45 ),
            height = Dimension(min = 7, preferred = 33),
            scroll_offsets = ScrollOffsets(top = 1, bottom = 1)),
        Window(
            height = Dimension.exact(1),
            char = "\u2500",
            style = "class:sidebar,separator",
            ),
        expanding_object_notification(my_app),
        sql_sidebar_navigation()])
