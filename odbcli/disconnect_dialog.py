from prompt_toolkit.widgets import Button, Dialog, Label
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.filters import is_done
from .conn import connWrappers
from .filters import ShowDisconnectDialog

def disconnect_dialog(my_app: "sqlApp"):
    def yes_handler() -> None:
        obj = my_app.selected_object
        obj.conn.close()
        my_app.completer.reset_completions()
        if my_app.active_conn is obj.conn:
            my_app.active_conn = None
        my_app.show_disconnect_dialog = False
        my_app.show_sidebar = True
        my_app.application.layout.focus("sidebarbuffer")

    def no_handler() -> None:
        my_app.show_disconnect_dialog = False
        my_app.show_sidebar = True
        my_app.application.layout.focus("sidebarbuffer")

    dialog = Dialog(
        title = lambda: my_app.selected_object.name,
        body=Label(text = "Are you sure you want to disconnect?",
            dont_extend_height = True),
        buttons=[
            Button(text = "OK", handler = yes_handler),
            Button(text = "Cancel", handler = no_handler),
        ],
        with_background = False,
    )

    return ConditionalContainer(
            content = dialog,
            filter = ShowDisconnectDialog(my_app) & ~is_done
    )
