"""
NbmSearch protocol handler — registered as handler for nbmsearch:// URIs.

URI formats:
  nbmsearch://open/<url-encoded-path>    open file with default app
  nbmsearch://folder/<url-encoded-path>  open containing folder in Explorer
                                         with file selected
"""
import sys
import os
import subprocess
from urllib.parse import unquote


def main():
    if len(sys.argv) < 2:
        return

    uri = sys.argv[1].strip()

    if uri.startswith("nbmsearch://open/"):
        path = unquote(uri.removeprefix("nbmsearch://open/"))
        try:
            os.startfile(path)
        except Exception as e:
            _error(f"Не удалось открыть файл:\n{path}\n\n{e}")

    elif uri.startswith("nbmsearch://folder/"):
        path = unquote(uri.removeprefix("nbmsearch://folder/"))
        try:
            # /select, highlights the specific file inside Explorer
            subprocess.Popen(
                f'explorer.exe /select,"{path}"',
                shell=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as e:
            _error(f"Не удалось открыть папку:\n{os.path.dirname(path)}\n\n{e}")


def _error(msg: str):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("NbmSearch", msg)
        root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    main()
