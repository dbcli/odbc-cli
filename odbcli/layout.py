from functools import partial
from prompt_toolkit.layout.processors import AppendAutoSuggestion
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout.containers import VSplit, HSplit, Window, ConditionalContainer, FloatContainer, Container, Float, ScrollOffsets
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition, has_focus, is_done, renderer_height_is_known
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.completion import DynamicCompleter, ThreadedCompleter
from prompt_toolkit.history import History, InMemoryHistory, FileHistory, ThreadedHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory, ThreadedAutoSuggest
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import SearchToolbar
from prompt_toolkit.formatted_text import StyleAndTextTuples, to_formatted_text
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.application import get_app
from prompt_toolkit.mouse_events import MouseEvent
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.selection import SelectionType
from pygments.lexers.sql import SqlLexer
from os.path import expanduser
from .sidebar import sql_sidebar, sql_sidebar_help, show_sidebar_button_info, sql_sidebar_navigation
from .loginprompt import login_prompt
from .disconnect_dialog import disconnect_dialog
from .preview import PreviewElement
from .filters import ShowLoginPrompt, ShowSidebar, MultilineFilter
from .utils import if_mousedown
from .conn import connStatus
from .config import config_location, ensure_dir_exists

def get_inputmode_fragments(my_app: "sqlApp") -> StyleAndTextTuples:
    """
    Return current input mode as a list of (token, text) tuples for use in a
    toolbar.
    """
    app = get_app()

    @if_mousedown
    def toggle_vi_mode(mouse_event: MouseEvent) -> None:
        my_app.vi_mode = not my_app.vi_mode

    token = "class:status-toolbar"
    input_mode_t = "class:status-toolbar.input-mode"

    mode = app.vi_state.input_mode
    result: StyleAndTextTuples = []
    append = result.append

#    if my_app.title:
    if False:
        result.extend(to_formatted_text(my_app.title))

    append((input_mode_t, "[F-4] ", toggle_vi_mode))

    # InputMode
    if my_app.vi_mode:
        recording_register = app.vi_state.recording_register
        if recording_register:
            append((token, " "))
            append((token + " class:record", "RECORD({})".format(recording_register)))
            append((token, " - "))

        if app.current_buffer.selection_state is not None:
            if app.current_buffer.selection_state.type == SelectionType.LINES:
                append((input_mode_t, "Vi (VISUAL LINE)", toggle_vi_mode))
            elif app.current_buffer.selection_state.type == SelectionType.CHARACTERS:
                append((input_mode_t, "Vi (VISUAL)", toggle_vi_mode))
                append((token, " "))
            elif app.current_buffer.selection_state.type == SelectionType.BLOCK:
                append((input_mode_t, "Vi (VISUAL BLOCK)", toggle_vi_mode))
                append((token, " "))
        elif mode in (InputMode.INSERT, "vi-insert-multiple"):
            append((input_mode_t, "Vi (INSERT)", toggle_vi_mode))
            append((token, "  "))
        elif mode == InputMode.NAVIGATION:
            append((input_mode_t, "Vi (NAV)", toggle_vi_mode))
            append((token, "     "))
        elif mode == InputMode.REPLACE:
            append((input_mode_t, "Vi (REPLACE)", toggle_vi_mode))
            append((token, " "))
    else:
        if app.emacs_state.is_recording:
            append((token, " "))
            append((token + " class:record", "RECORD"))
            append((token, " - "))

        append((input_mode_t, "Emacs", toggle_vi_mode))
        append((token, " "))

    append((input_mode_t, "[C-q] Exit Client", ))

    return result

def get_connection_fragments(my_app: "sqlApp") -> StyleAndTextTuples:
    """
    Return current input mode as a list of (token, text) tuples for use in a
    toolbar.
    """
    app = get_app()
    status = my_app.active_conn.status if my_app.active_conn else connStatus.DISCONNECTED
    if status == connStatus.FETCHING:
        token = "class:status-toolbar.conn-fetching"
        status_text = "Fetching"
    elif status == connStatus.EXECUTING:
        token = "class:status-toolbar.conn-executing"
        status_text = "Executing"
    elif status == connStatus.ERROR:
        token = "class:status-toolbar.conn-executing"
        status_text = "Unexpected Error"
    elif status == connStatus.DISCONNECTED:
        token = "class:status-toolbar.conn-fetching"
        status_text = "Disconnected"
    else:
        token = "class:status-toolbar.conn-idle"
        status_text = "Idle"

    result: StyleAndTextTuples = []
    append = result.append

    append((token, " " + status_text))
    return result

def exit_confirmation(
    my_app: "sqlApp", style = "class:exit-confirmation"
) -> Container:
    """
    Create `Layout` for the exit message.
    """

    def get_text_fragments() -> StyleAndTextTuples:
        # Show "Do you really want to exit?"
        return [
            (style, "\n %s ([y]/n)" % my_app.exit_message),
            ("[SetCursorPosition]", ""),
            (style, "  \n"),
        ]

    visible = ~is_done & Condition(lambda: my_app.show_exit_confirmation)

    return ConditionalContainer(
        content=Window(
            FormattedTextControl(get_text_fragments), style=style
        ),
        filter=visible,
    )



def status_bar(my_app: "sqlApp") -> Container:
    """
    Create the `Layout` for the status bar.
    """
    TB = "class:status-toolbar"

    def get_text_fragments() -> StyleAndTextTuples:

        result: StyleAndTextTuples = []
        append = result.append

        append((TB, " "))
        result.extend(get_inputmode_fragments(my_app))
        append((TB, " "))
        result.extend(get_connection_fragments(my_app))


        return result

    return ConditionalContainer(
        content=Window(content=FormattedTextControl(get_text_fragments), style=TB),
        filter=~is_done
        & renderer_height_is_known
        & Condition(
            lambda: not my_app.show_exit_confirmation
        ),
    )

def sql_line_prefix(
        line_number: int,
        wrap_count: int,
        my_app: "sqlApp"
        ) -> StyleAndTextTuples:
    if my_app.active_conn is not None:
        sqlConn = my_app.active_conn
        prompt = sqlConn.username + "@" + sqlConn.dsn + ":" + sqlConn.current_catalog() + " > "
    else:
        prompt = "> "
    if line_number == 0 and wrap_count == 0:
        return to_formatted_text([("class:prompt", prompt)])
    prompt_width = len(prompt)
    return [("class:prompt.dots", "." * (prompt_width - 1) + " ")]

class sqlAppLayout:
    def __init__(
        self,
        my_app: "sqlApp"
    ) -> None:

        self.my_app = my_app
        self.search_field = SearchToolbar()
        history_file = config_location() + 'history'
        ensure_dir_exists(history_file)
        hist = ThreadedHistory(FileHistory(expanduser(history_file)))
        self.input_buffer = Buffer(
                name = "defaultbuffer",
                tempfile_suffix = ".py",
                multiline = MultilineFilter(self.my_app),
                history = hist,
                completer = ThreadedCompleter(self.my_app.completer),
                auto_suggest = ThreadedAutoSuggest(AutoSuggestFromHistory()),
                complete_while_typing = Condition(
                    lambda: self.my_app.active_conn is not None
                )
            )
        main_win_control = BufferControl(
                buffer = self.input_buffer,
                lexer = PygmentsLexer(SqlLexer),
                search_buffer_control = self.search_field.control,
                include_default_input_processors = False,
                input_processors = [AppendAutoSuggestion()],
                preview_search = True
                )

        self.main_win = Window(
                main_win_control,
                height = (
                    lambda: (
                        None
                        if get_app().is_done
                        else (Dimension(min = self.my_app.min_num_menu_lines) if not self.my_app.show_preview else Dimension(min = self.my_app.min_num_menu_lines, preferred = 180))
                    )
                ),
                get_line_prefix = partial(sql_line_prefix, my_app = self.my_app),
                scroll_offsets=ScrollOffsets(bottom = 1, left = 4, right = 4)
            )

        preview_element = PreviewElement(self.my_app)
        self.lprompt = login_prompt(self.my_app)
        self.preview = preview_element.create_container()
        self.disconnect_dialog = disconnect_dialog(self.my_app)
        container = HSplit([
            VSplit([
                FloatContainer(
                    content=HSplit(
                        [
                            self.main_win,
                            self.search_field,
                        ]
                    ),
                    floats=[
                        Float(
                            bottom = 1,
                            left = 1,
                            right = 0,
                            content = sql_sidebar_help(self.my_app),
                        ),
                        Float(
                            content = self.lprompt
                            ),
                        Float(
                            content = self.preview,
                            ),
                        preview_element.create_completion_float(),
                        Float(
                            content = self.disconnect_dialog,
                            ),
                        Float(
                            left = 2,
                            bottom = 1,
                            content = exit_confirmation(self.my_app)
                        ),
                        Float(
                            xcursor = True,
                            ycursor = True,
                            transparent = True,
                            content = CompletionsMenu(
                                scroll_offset = 1,
                                max_height = 16,
                                extra_filter = has_focus(self.input_buffer)
                                )
                            )
                    ]
                ),
                ConditionalContainer(
                    content = sql_sidebar(self.my_app),
                    filter=ShowSidebar(self.my_app) & ~is_done,
                )
            ]),
            VSplit(
                [status_bar(self.my_app), show_sidebar_button_info(self.my_app)]
            )
        ])

        def accept(buff):
            app = get_app()
            app.exit(result = ["non-preview", buff.text])
            app.pre_run_callables.append(buff.reset)
            return True

        self.input_buffer.accept_handler = accept
        self.layout = Layout(container, focused_element = self.main_win)
