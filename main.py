# main.py
import sys
from PyQt6.QtWidgets import QApplication
from ui.main_window import MainWindow

if __name__ == '__main__':
    # Necessary for ProcessPoolExecutor when packaged with PyInstaller on some platforms
    # import multiprocessing
    # multiprocessing.freeze_support() 

    app = QApplication(sys.argv)
    main_win = MainWindow()
    main_win.show()
    sys.exit(app.exec())