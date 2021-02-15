#!/usr/bin/env python
"""
A simple example of a calculator program.
This could be used as inspiration for a REPL.
"""
import os
from sys import stderr
from time import time
from cyanodbc import DatabaseError, datasources
from click import echo_via_pager, secho
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.utils import get_cwidth
from .app import sqlApp, ExitEX
from .layout import sqlAppLayout
from .conn import connStatus, executionStatus


def main():

    my_app = sqlApp()
#    with patch_stdout():
    while True:
        try:
            app_res = my_app.application.run()
        except ExitEX:
            for i in range(len(my_app.obj_list)):
                my_app.obj_list[i].conn.close()
            return
        else:
            # If it's a preview query we need an indication
            # of where to run the query
            if(app_res[0] == "preview"):
                sql_conn = my_app.selected_object.conn
            else:
                sql_conn = my_app.active_conn
            if sql_conn is not None:
                #TODO also check that it is connected
                try:
                    secho("Executing query...Ctrl-c to cancel", err = False)
                    start = time()
                    crsr = sql_conn.async_execute(app_res[1])
                    execution = time() - start
                    secho("Query execution...done", err = False)
                    if(app_res[0] == "preview"):
                        sql_conn.async_fetchall(my_app.preview_chunk_size,
                                my_app.application)
                        continue
                    if my_app.timing_enabled:
                        print("Time: %0.03fs" % execution)

                    if sql_conn.execution_status == executionStatus.FAIL:
                        err = sql_conn.execution_err
                        secho("Query error: %s\n" % err, err = True, fg = "red")
                    else:
                        if crsr.description:
                            cols = [col.name for col in crsr.description]
                        else:
                            cols = []
                        if len(cols):
                            ht = my_app.application.output.get_size()[0]
                            sql_conn.async_fetchall(my_app.fetch_chunk_multiplier *
                                    (ht - 3 - my_app.pager_reserve_lines), my_app.application)
                            formatted = sql_conn.formatted_fetch(ht - 3 - my_app.pager_reserve_lines, cols, my_app.table_format)
                            echo_via_pager(formatted)
                        else:
                            secho("No rows returned\n", err = False)
                except KeyboardInterrupt:
                    secho("Cancelling query...", err = True, fg = "red")
                    sql_conn.cancel()
                    secho("Query cancelled.", err = True, fg = "red")
                sql_conn.close_cursor()
