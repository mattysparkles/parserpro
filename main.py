import sys
import warnings

import tkinter as tk
from tkinter import ttk

try:
    from urllib3.exceptions import InsecureRequestWarning
except ImportError:
    InsecureRequestWarning = Warning

from gui import CombinedParserGUI

warnings.simplefilter("ignore", InsecureRequestWarning)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        style.theme_use("clam")
    except Exception:
        pass
    CombinedParserGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
