from prompt_toolkit.layout.processors import ConditionalProcessor, HighlightIncrementalSearchProcessor, HighlightSelectionProcessor, AppendAutoSuggestion
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.document import Document
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.filters import has_focus, is_done
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.widgets import Button, TextArea, SearchToolbar, Box, Shadow, Frame
from prompt_toolkit.layout.containers import Window, VSplit, HSplit, ConditionalContainer, FloatContainer, Float
from prompt_toolkit.filters import Condition, is_done
from prompt_toolkit.completion import Completer, Completion, ThreadedCompleter, CompleteEvent
from cyanodbc import ConnectError, DatabaseError
from cli_helpers.tabular_output import TabularOutputFormatter
from functools import partial
from typing import Callable, Iterable
from .completion.mssqlcompleter import MssqlCompleter
from .filters import ShowPreview
from .conn import connWrappers, connStatus, executionStatus
from logging import getLogger

class PreviewCompleter(Completer):
    """ Wraps prompt_toolkit.Completer.  The buffer that this completer is
        attached to only carries part of of the query: for example 'WHERE ...'.
        To complete the query effectively we need the complete preview query
        and this completer constructs a document object that carries the full
        query and feeds it to the wrapped completer.
        Rather than wrapping, probably should extend the class - however
        at this time as the completer class is fairly hacked up and not
        in a steady state, let's stay with the wrap.
    """
    def __init__(self, my_app: "sqlApp", completer: Completer) -> None:
        self.completer = completer
        self.my_app = my_app

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        obj = self.my_app.selected_object
        sql_conn = obj.conn
        catalog = None
        schema = None
        # TODO: Verify connected
        if obj.parent is not None:
            if type(obj.parent).__name__ == "myDBSchema":
                schema = obj.parent.name
            elif type(obj.parent).__name__ == "myDBCatalog":
                catalog = obj.parent.name
            if obj.parent.parent is not None:
                if type(obj.parent.parent).__name__ == "myDBCatalog":
                    catalog = obj.parent.parent.name

        if catalog:
            catalog =  (sql_conn.quotechar + "%s" + sql_conn.quotechar) % catalog
        if schema:
            schema =  (sql_conn.quotechar + "%s" + sql_conn.quotechar) % schema
        name = (sql_conn.quotechar + "%s" + sql_conn.quotechar) % obj.name
        identifier = ".".join(list(filter(None, [catalog, schema, name])))
        query = sql_conn.preview_query(
                table = identifier,
                filter_query = document.text,
                limit = self.my_app.preview_limit_rows)

        new_document = Document(text = query,
                cursor_position = query.find(document.text) + document.cursor_position)
        return self.completer.get_completions(new_document, complete_event)

class PreviewElement:
    """ Class to create the preview element.  It contains two main methods:
        create_container: creates the main preview container.  Intention is
        for this to land in a float.
        create_completion_float: creates the completion float in the preview
        container.  Intention is for this to appear in the FloatContainer that
        hosts the main preview container float.
    """
    def __init__(self, my_app: "sqlApp"):
        self.my_app = my_app
        help_text = """
        Press Enter in the input box to page through the table.
        Alternatively, enter a filtering SQL statement and then press Enter
        to page through the results.
        """
        self.formatter = TabularOutputFormatter()
        self.completer = PreviewCompleter(
                my_app = self.my_app,
                completer = MssqlCompleter(
                    smart_completion = True,
                    get_conn = lambda: self.my_app.selected_object.conn))

        self.input_buffer = Buffer(
                name = "previewbuffer",
                tempfile_suffix = ".sql",
#                completer = ThreadedCompleter(self.completer),
                completer = self.completer,
                complete_while_typing = Condition(
                    lambda: self.my_app.selected_object is not None and self.my_app.selected_object.conn.connected()
                ),
                multiline = False)

        input_control = BufferControl(
                buffer = self.input_buffer,
                include_default_input_processors = False,
                input_processors = [AppendAutoSuggestion()],
                preview_search = False)

        self.input_window = Window(input_control)

        search_buffer = Buffer(name = "previewsearchbuffer")
        self.search_field = SearchToolbar(search_buffer)
        self.output_field = TextArea(style = "class:preview-output-field",
                text = help_text,
                height = D(preferred = 50),
                search_field = self.search_field,
                wrap_lines = False,
                focusable = True,
                read_only = True,
                preview_search = True,
                input_processors = [
                    ConditionalProcessor(
                        processor=HighlightIncrementalSearchProcessor(),
                        filter=has_focus("previewsearchbuffer")
                        | has_focus(self.search_field.control),
                        ),
                    HighlightSelectionProcessor(),
                ])

        def refresh_results(window_height) -> bool:
            """ This method gets called when the app restarts after
                exiting for execution of preview query.  It populates
                the output buffer with results from the fetch/query.
            """
            sql_conn = self.my_app.selected_object.conn

            if sql_conn.execution_status == executionStatus.FAIL:
                # Let's display the error message to the user
                output = sql_conn.execution_err
            else:
                crsr = sql_conn.cursor
                if crsr.description:
                    cols = [col.name for col in crsr.description]
                else:
                    cols = []
                if len(cols):
                    sql_conn.status = connStatus.FETCHING
                    res = sql_conn.async_fetchmany(size = window_height - 4)
                    output = self.formatter.format_output(res, cols, format_name = "psql")
                    output = "\n".join(output)
                else:
                    sql_conn.status = connStatus.IDLE
                    output = "No rows returned\n"

            # Add text to output buffer.
            self.output_field.buffer.set_document(Document(
                text = output, cursor_position = 0), True)

            return True

        def accept(buff: Buffer) -> bool:
            """ This method gets called when the user presses enter/return
                in the filter box.  It is interpreted as either 'execute query'
                or 'fetch next page of results' if filter query hasn't changed.
            """
            obj = self.my_app.selected_object
            sql_conn = obj.conn
            catalog = None
            schema = None
            # TODO: Verify connected
            if obj.parent is not None:
                if type(obj.parent).__name__ == "myDBSchema":
                    schema = obj.parent.name
                elif type(obj.parent).__name__ == "myDBCatalog":
                    catalog = obj.parent.name
                if obj.parent.parent is not None:
                    if type(obj.parent.parent).__name__ == "myDBCatalog":
                        catalog = obj.parent.parent.name

            if catalog:
                catalog =  (sql_conn.quotechar + "%s" + sql_conn.quotechar) % catalog
            if schema:
                schema =  (sql_conn.quotechar + "%s" + sql_conn.quotechar) % schema
            name = (sql_conn.quotechar + "%s" + sql_conn.quotechar) % obj.name
            identifier = ".".join(list(filter(None, [catalog, schema, name])))
            query = sql_conn.preview_query(table = identifier, filter_query = buff.text,
                    limit = self.my_app.preview_limit_rows)

            func = partial(refresh_results,
                    window_height = self.output_field.window.render_info.window_height)
            # If status is IDLE, this is the first time we are executing.
            if sql_conn.query != query or sql_conn.status == connStatus.IDLE:
                # Exit the app to execute the query
                self.my_app.application.exit(result = ["preview", query])
                self.my_app.application.pre_run_callables.append(func)
            else:
                # No need to exit let's just go and fetch
                func()
            return True # Keep filter text

        def cancel_handler() -> None:
            sql_conn = self.my_app.selected_object.conn
            sql_conn.close_cursor()
            sql_conn.status = connStatus.IDLE
            self.input_buffer.text = ""
            self.output_field.buffer.set_document(Document(
                text = help_text, cursor_position = 0
            ), True)
            self.my_app.show_preview = False
            self.my_app.show_sidebar = True
            self.my_app.application.layout.focus(self.input_buffer)
            self.my_app.application.layout.focus("sidebarbuffer")
            return None

        self.input_buffer.accept_handler = accept
        self.cancel_button = Button(text = "Done", handler = cancel_handler)

    def create_completion_float(self) -> Float:
        return Float(
                xcursor = True,
                ycursor = True,
                transparent = True,
                attach_to_window = self.input_window,
                content = CompletionsMenu(
                    scroll_offset = 1,
                    max_height = 16,
                    extra_filter = has_focus(self.input_buffer)))

    def create_container(self):

        container = HSplit(
                [
                    Box(
                        body = VSplit(
                            [self.input_window, self.cancel_button],
                            padding=1
                            ),
                        padding=1,
                        style="class:preview-input-field"
                    ),
                    Window(height=1, char="-", style="class:preview-divider-line"),
                    self.output_field,
                    self.search_field,
                    ])

        frame = Shadow(
                body = Frame(
                    title = "Table Preview",
                    body = container,
                    style="class:dialog.body",
                    width = D(preferred = 180, min = 30),
                    modal = True))

        return ConditionalContainer(
                content = frame,
                filter = ShowPreview(self.my_app) & ~is_done)
