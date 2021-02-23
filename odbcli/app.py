import logging
import os
import sys
from typing import Any, Callable, Dict, Generic, List, Optional, TypeVar
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.key_binding.bindings.auto_suggest import load_auto_suggest_bindings
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding.bindings.focus import focus_next
from prompt_toolkit.filters import Condition, has_focus
from logging.handlers import RotatingFileHandler
from cyanodbc import datasources
from .sidebar import myDBConn, myDBObject
from .conn import sqlConnection
from .completion.mssqlcompleter import MssqlCompleter
from .config import get_config, config_location, ensure_dir_exists
from .odbcstyle import style_factory
from .layout import sqlAppLayout

class ExitEX(Exception):
    pass

class sqlApp:
    def __init__(
        self,
        odbclirc_file = None
    ) -> None:
        c = self.config = get_config(odbclirc_file)
        self.initialize_logging()
        self.set_default_pager(c)
        self.mouse_support: bool = c["main"].as_bool("mouse_support")
        self.fetch_chunk_multiplier = c["main"].as_int("fetch_chunk_multiplier")
        self.preview_limit_rows = c["main"].as_int("preview_limit_rows")
        self.preview_chunk_size = c["main"].as_int("preview_fetch_chunk_size")
        self.pager_reserve_lines = c["main"].as_int("pager_reserve_lines")
        self.table_format = c["main"]["table_format"]
        self.timing_enabled = c["main"].as_bool("timing")
        self.syntax_style = c["main"]["syntax_style"]
        self.cli_style = c["colors"]
        self.multiline: bool = c["main"].as_bool("multi_line")
        self.min_num_menu_lines = c["main"].as_int("min_num_menu_lines")

        self.show_exit_confirmation: bool = False
        self.exit_message: str = "Do you really want to exit?"

        self.show_expanding_object: bool = False

        self.show_sidebar: bool = True
        self.show_login_prompt: bool = False
        self.show_preview: bool = False
        self.show_disconnect_dialog: bool = False
        self._active_conn = None
        self.obj_list = []
        # Flag to signal to some of the prompt toolkit structures that we need
        # to traverse the obj_list anew to list all the objects in the sidebar.
        # Added for efficiency (no need to traverse unless necessary).  Updated
        # from the main thread always, so no need for locking.
        self.obj_list_changed: bool = True
        # This field is a list with two elements.  The first is the index of
        # the currently selected object (0-indexed).  The second is the
        # index of the currently selected object in the
        # list of objects where each object is counted with length of characters
        # in name + 1 multiplicity.  So for example a list of objects
        # A
        # AB
        # ABC
        # ABCD
        # where "ABC" is selected would present the index as 5 ([1+1] + [2+1]).
        # This is used to track the cursor position in the sidebar document
        # It is recorded here, rather than elsewhere because we can track it
        # here far more efficiently (select_next, and select_previous).
        # It is important that all methods of this class that manipulate the
        # currently selected object also update this index.
        self._selected_obj_idx = [0, 0]
        dsns = list(datasources().keys())
        if len(dsns) < 1:
            sys.exit("No datasources found ... exiting.")
        for dsn in dsns:
            self.obj_list.append(myDBConn(
                my_app = self,
                conn = sqlConnection(dsn = dsn),
                name = dsn,
                otype = "Connection"))
        for i in range(len(self.obj_list) - 1):
            self.obj_list[i].next_object = self.obj_list[i + 1]
        # Loop over side-bar when moving past the element on the bottom
        self.obj_list[len(self.obj_list) - 1].next_object = self.obj_list[0]
        self._selected_object = self.obj_list[0]
        self.completer = MssqlCompleter(smart_completion = True, get_conn = lambda: self.active_conn)

        self.application = self._create_application()

    @property
    def active_conn(self) -> sqlConnection:
        return self._active_conn

    @active_conn.setter
    def active_conn(self, conn: sqlConnection) -> None:
        self._active_conn = conn

    @property
    def selected_object(self) -> myDBObject:
        return self._selected_object

    @selected_object.setter
    def selected_object(self, obj) -> None:
        """ Avoid using / computationally expensive.
            Instead try using select_next / select_previous if possible.
            Will update _selected_obj_idx appropriately.
        """
        cursor = 0
        idx = 0
        o = self.obj_list[0]
        self._selected_object = obj
        while o is not self._selected_object:
            if not o.next_object:
                raise IndexError
            cursor += len(o.name) + 1
            idx += 1
            o = o.next_object
        self._selected_obj_idx = [idx, cursor]

    def select(self, idx) -> None:
        """ Select the [i]-th object in the list.  Will also update
            _selected_obj_idx appropriately.
        """
        counter = 0
        cursor = 0
        o = self.obj_list[0]
        while counter < idx:
            if not o.next_object:
                raise IndexError
            counter += 1
            cursor += len(o.name) + 1
            o = o.next_object
        self._selected_object = o
        self._selected_obj_idx = [idx, cursor]

    @property
    def selected_object_idx(self):
        return self._selected_obj_idx

    def select_next(self) -> None:
        self._selected_object = self.selected_object.next_object

    def select_previous(self) -> None:
        obj = self.selected_object.parent if self.selected_object.parent is not None else self.obj_list[0]
        while obj.next_object is not self.selected_object:
            obj = obj.next_object
        self._selected_object = obj

    @property
    def editing_mode(self) -> EditingMode:
        return self.application.editing_mode

    @editing_mode.setter
    def editing_mode(self, value: EditingMode) -> None:
        app = self.application
        app.editing_mode = value

    @property
    def vi_mode(self) -> bool:
        return self.editing_mode == EditingMode.VI

    @vi_mode.setter
    def vi_mode(self, value: bool) -> None:
        if value:
            self.editing_mode = EditingMode.VI
        else:
            self.editing_mode = EditingMode.EMACS

    def set_default_pager(self, config):
        configured_pager = config["main"].get("pager")
        os_environ_pager = os.environ.get("PAGER")

        if configured_pager:
            self.logger.info(
                'Default pager found in config file: "%s"', configured_pager
            )
            os.environ["PAGER"] = configured_pager
        elif os_environ_pager:
            self.logger.info(
                'Default pager found in PAGER environment variable: "%s"',
                os_environ_pager,
            )
            os.environ["PAGER"] = os_environ_pager
        else:
            self.logger.info(
                "No default pager found in environment. Using os default pager"
            )

        # Set default set of less recommended options, if they are not already set.
        # They are ignored if pager is different than less.
        if not os.environ.get("LESS"):
            os.environ["LESS"] = "-SRXF"

    def initialize_logging(self):
        log_file = self.config['main']['log_file']
        if log_file == 'default':
            log_file = config_location() + 'odbcli.log'
        ensure_dir_exists(log_file)
        log_level = self.config['main']['log_level']

        # Disable logging if value is NONE by switching to a no-op handler.
        # Set log level to a high value so it doesn't even waste cycles getting
        # called.
        if log_level.upper() == 'NONE':
            handler = logging.NullHandler()
        else:
            # creates a log buffer with max size of 20 MB and 5 backup files
            handler = RotatingFileHandler(os.path.expanduser(log_file),
                    encoding='utf-8', maxBytes=1024*1024*20, backupCount=5)

        level_map = {'CRITICAL': logging.CRITICAL,
                     'ERROR': logging.ERROR,
                     'WARNING': logging.WARNING,
                     'INFO': logging.INFO,
                     'DEBUG': logging.DEBUG,
                     'NONE': logging.CRITICAL
                     }

        log_level = level_map[log_level.upper()]

        formatter = logging.Formatter(
            '%(asctime)s (%(process)d/%(threadName)s) '
            '%(name)s %(levelname)s - %(message)s')

        handler.setFormatter(formatter)

        root_logger = logging.getLogger('odbcli')
        root_logger.addHandler(handler)
        root_logger.setLevel(log_level)

        root_logger.info('Initializing odbcli logging.')
        root_logger.debug('Log file %r.', log_file)
        self.logger = logging.getLogger(__name__)

    def _create_application(self) -> Application:
        self.sql_layout = sqlAppLayout(my_app = self)
        kb = KeyBindings()

        confirmation_visible = Condition(lambda: self.show_exit_confirmation)
        @kb.add("c-q")
        def _(event):
            " Pressing Ctrl-Q or Ctrl-C will exit the user interface. "
            self.show_exit_confirmation = True

        @kb.add("y", filter=confirmation_visible)
        @kb.add("Y", filter=confirmation_visible)
        @kb.add("enter", filter=confirmation_visible)
        @kb.add("c-q", filter=confirmation_visible)
        def _(event):
            """
            Really quit.
            """
            event.app.exit(exception = ExitEX(), style="class:exiting")

        @kb.add(Keys.Any, filter=confirmation_visible)
        def _(event):
            """
            Cancel exit.
            """
            self.show_exit_confirmation = False

        # Global key bindings.
        @kb.add("tab", filter = Condition(lambda: self.show_preview or self.show_login_prompt))
        def _(event):
            event.app.layout.focus_next()
        @kb.add("f4")
        def _(event):
            " Toggle between Emacs and Vi mode. "
            self.vi_mode = not self.vi_mode
        # apparently ctrls does this
        @kb.add("c-t", filter = Condition(lambda: not self.show_preview))
        def _(event):
            """
            Show/hide sidebar.
            """
            self.show_sidebar = not self.show_sidebar
            if self.show_sidebar:
                event.app.layout.focus("sidebarbuffer")
            else:
                event.app.layout.focus_previous()

        sidebar_visible = Condition(lambda: self.show_sidebar and not self.show_expanding_object and not self.show_login_prompt and not self.show_preview) \
                        & ~has_focus("sidebarsearchbuffer")
        @kb.add("up", filter=sidebar_visible)
        @kb.add("c-p", filter=sidebar_visible)
        @kb.add("k", filter=sidebar_visible)
        def _(event):
            " Go to previous option. "
            obj = self._selected_object
            self.select_previous()
            inc = len(self.selected_object.name) + 1 # newline character
            if obj is self.obj_list[0]:
                idx = 0
                cursor = 0
                while obj is not self._selected_object:
                    if not obj.next_object:
                        raise IndexError
                    cursor += len(obj.name) + 1
                    idx += 1
                    obj = obj.next_object
                self._selected_obj_idx = [idx, cursor]
            else:
                self._selected_obj_idx[0] -= 1
                self._selected_obj_idx[1] -= inc

        @kb.add("down", filter=sidebar_visible)
        @kb.add("c-n", filter=sidebar_visible)
        @kb.add("j", filter=sidebar_visible)
        def _(event):
            " Go to next option. "
            inc = len(self.selected_object.name) + 1 # newline character
            self.select_next()
            if self.selected_object is self.obj_list[0]:
                self._selected_obj_idx = [0, 0]
            else:
                self._selected_obj_idx[0] += 1
                self._selected_obj_idx[1] += inc

        @kb.add("enter", filter = sidebar_visible)
        def _(event):
            " If connection, connect.  If table preview"
            obj = self.selected_object
            if type(obj).__name__ == "myDBConn" and not obj.conn.connected():
                self.show_login_prompt = True
                event.app.layout.focus(self.sql_layout.lprompt)
            if type(obj).__name__ == "myDBConn" and obj.conn.connected():
                # OG: some thread locking may be needed here
                self._active_conn = obj.conn
            elif obj.otype in ["table", "view", "function"]:
                self.show_preview = True
                self.show_sidebar = False
                event.app.layout.focus(self.sql_layout.preview)

        @kb.add("right", filter=sidebar_visible)
        @kb.add("l", filter=sidebar_visible)
        @kb.add(" ", filter=sidebar_visible)
        def _(event):
            " Select next value for current option. "
            obj = self.selected_object
            obj.expand()
            if type(obj).__name__ == "myDBConn" and not obj.conn.connected():
                self.show_login_prompt = True
                event.app.layout.focus(self.sql_layout.lprompt)

        @kb.add("left", filter=sidebar_visible)
        @kb.add("h", filter=sidebar_visible)
        def _(event):
            " Select next value for current option. "
            obj = self.selected_object
            if type(obj).__name__ == "myDBConn" and obj.conn.connected() and obj.children is None:
                self.show_disconnect_dialog = True
                event.app.layout.focus(self.sql_layout.disconnect_dialog)
            else:
                obj.collapse()

        auto_suggest_bindings = load_auto_suggest_bindings()

        return Application(
            layout = self.sql_layout.layout,
            key_bindings = merge_key_bindings([kb, auto_suggest_bindings]),
            enable_page_navigation_bindings = True,
            style = style_factory(self.syntax_style, self.cli_style),
            include_default_pygments_style = False,
            mouse_support = self.mouse_support,
            full_screen = False,
            editing_mode = EditingMode.VI if self.config["main"].as_bool("vi") else EditingMode.EMACS
        )
