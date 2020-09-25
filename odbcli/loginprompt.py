from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.widgets import Button, Dialog, Label
from prompt_toolkit.widgets import TextArea
from prompt_toolkit.layout.containers import HSplit, ConditionalContainer, WindowAlign, Window
from prompt_toolkit.filters import is_done
from cyanodbc import ConnectError, DatabaseError, SQLGetInfo
from .conn import connWrappers, sqlConnection
from .filters import ShowLoginPrompt

def login_prompt(my_app: "sqlApp"):

    def ok_handler() -> None:
        my_app.application.layout.focus(uidTextfield)
        obj = my_app.selected_object
        try:
            obj.conn.connect(username = uidTextfield.text, password = pwdTextfield.text)
            # Query the type of back-end and instantiate an appropriate class
            dbms = obj.conn.get_info(SQLGetInfo.SQL_DBMS_NAME)
            # Now clone object
            cls = connWrappers[dbms] if dbms in connWrappers.keys() else sqlConnection
            newConn = cls(
                    dsn = obj.conn.dsn,
                    conn = obj.conn.conn,
                    username = obj.conn.username,
                    password = obj.conn.password)
            obj.conn.close()
            newConn.connect()
            obj.conn = newConn
            my_app.active_conn = obj.conn
            # OG some thread locking may be needed here
            obj.expand()
        except ConnectError as e:
            msgLabel.text = "Connect failed"
        else:
            msgLabel.text = ""
            my_app.show_login_prompt = False
            my_app.show_sidebar = True
            my_app.application.layout.focus("sidebarbuffer")

        uidTextfield.text = ""
        pwdTextfield.text = ""

    def cancel_handler() -> None:
        msgLabel.text = ""
        my_app.application.layout.focus(uidTextfield)
        my_app.show_login_prompt = False
        my_app.show_sidebar = True
        my_app.application.layout.focus("sidebarbuffer")

    def accept(buf: Buffer) -> bool:
        my_app.application.layout.focus(ok_button)
        return True

    ok_button = Button(text="OK", handler=ok_handler)
    cancel_button = Button(text="Cancel", handler=cancel_handler)

    pwdTextfield = TextArea(
        multiline=False, password=True, accept_handler=accept
    )
    uidTextfield = TextArea(
        multiline=False, password=False, accept_handler=accept
    )
    msgLabel = Label(text = "", dont_extend_height = True, style = "class:frame.label")
    msgLabel.window.align = WindowAlign.CENTER
    dialog = Dialog(
            title="Server Credentials",
            body=HSplit(
                [
                    Label(text="Username ", dont_extend_height=True),
                    uidTextfield,
                    Label(text="Password", dont_extend_height=True),
                    pwdTextfield,
                    msgLabel
                ],
                padding=D(preferred=1, max=1),
            ),
            width = D(min = 10, preferred = 50),
            buttons=[ok_button, cancel_button],
            with_background = False
    )

    return ConditionalContainer(
            content = dialog,
            filter = ShowLoginPrompt(my_app) & ~is_done
    )
