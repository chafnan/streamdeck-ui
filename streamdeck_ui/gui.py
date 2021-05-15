"""Defines the QT powered interface for configuring Stream Decks"""
import os
import shlex
import sys
import time
import tkinter as tk
from functools import partial
from subprocess import Popen  # nosec - Need to allow users to specify arbitrary commands
from tkinter import filedialog
from typing import Callable, Dict

from pynput.keyboard import Controller, Key
from PySide2 import QtWidgets
from PySide2.QtCore import QMimeData, QSize, Qt, QTimer
from PySide2.QtGui import QDrag, QIcon, QKeySequence, QMouseEvent
from PySide2.QtWidgets import (
    QAction,
    QApplication,
    QDialog,
    QFileDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
    QShortcut,
    QSizePolicy,
    QSystemTrayIcon,
)

from streamdeck_ui import api
from streamdeck_ui.config import LOGO
from streamdeck_ui.ui_main import Ui_MainWindow
from streamdeck_ui.ui_settings import Ui_SettingsDialog

BUTTON_STYLE = """
    QToolButton{background-color:black; color:white;}
    QToolButton:checked{background-color:darkGray; color:black;}
    QToolButton:focus{border:none; }
"""

BUTTON_DRAG_STYLE = """
    QToolButton{background-color:white; color:black;}
    QToolButton:checked{background-color:darkGray; color:black;}
    QToolButton:focus{border:none; }
"""

selected_button: QtWidgets.QToolButton
text_timer = None
dimmer_options = {
    "Never": 0,
    "10 Seconds": 10,
    "1 Minute": 60,
    "5 Minutes": 300,
    "10 Minutes": 600,
    "15 Minutes": 900,
    "30 Minutes": 1800,
    "1 Hour": 3600,
    "5 Hours": 7200,
    "10 Hours": 36000,
}

multiPasteEnabled = False


class Dimmer:
    timeout = 0
    brightness = -1
    __stopped = False
    __dimmer_brightness = -1
    __timer = None
    __change_timer = None

    def __init__(self, timeout: int, brightness: int, brightness_callback: Callable[[int], None]):
        """ Constructs a new Dimmer instance

        :param int timeout: The time in seconds before the dimmer starts.
        :param int brightness: The normal brightness level.
        :param Callable[[int], None] brightness_callback: Callback that receives the current
                                                          brightness level.
         """
        self.timeout = timeout
        self.brightness = brightness
        self.brightness_callback = brightness_callback

    def stop(self) -> None:
        """ Stops the dimmer and sets the brightness back to normal. Call
        reset to start normal dimming operation. """
        if self.__timer:
            self.__timer.stop()

        if self.__change_timer:
            self.__change_timer.stop()

        self.__dimmer_brightness = self.brightness
        self.brightness_callback(self.brightness)
        self.__stopped = True

    def reset(self) -> bool:
        """ Reset the dimmer and start counting down again. If it was busy dimming, it will
        immediately stop dimming. Callback fires to set brightness back to normal."""

        self.__stopped = False
        if self.__timer:
            self.__timer.stop()

        if self.__change_timer:
            self.__change_timer.stop()

        if self.timeout:
            self.__timer = QTimer()
            self.__timer.setSingleShot(True)
            self.__timer.timeout.connect(partial(self.change_brightness))
            self.__timer.start(self.timeout * 1000)

        if self.__dimmer_brightness != self.brightness:
            self.brightness_callback(self.brightness)
            self.__dimmer_brightness = self.brightness
            return True

        return False

    def dim(self, toggle: bool = False):
        """ Manually initiate a dim event.
            If the dimmer is stopped, this has no effect. """

        if self.__stopped:
            return

        if toggle and self.__dimmer_brightness == 0:
            self.reset()
        elif self.__timer and self.__timer.isActive():
            # No need for the timer anymore, stop it
            self.__timer.stop()

            # Verify that we're not already at the target brightness nor
            # busy with dimming already
            if self.__change_timer is None and self.__dimmer_brightness:
                self.change_brightness()

    def change_brightness(self):
        """ Move the brightness level down by one and schedule another change_brightness event. """
        if self.__dimmer_brightness:
            self.__dimmer_brightness = self.__dimmer_brightness - 1
            self.brightness_callback(self.__dimmer_brightness)
            self.__change_timer = QTimer()
            self.__change_timer.setSingleShot(True)
            self.__change_timer.timeout.connect(partial(self.change_brightness))
            self.__change_timer.start(10)
        else:
            self.__change_timer = None


dimmers: Dict[str, Dimmer] = {}


class DraggableButton(QtWidgets.QToolButton):
    """A QToolButton that supports drag and drop and swaps the button properties on drop """

    def __init__(self, parent, ui):
        super(DraggableButton, self).__init__(parent)

        self.setAcceptDrops(True)
        self.ui = ui

    def mouseMoveEvent(self, e):  # noqa: N802 - Part of QT signature.

        if e.buttons() != Qt.LeftButton:
            return

        dimmers[_deck_id(self.ui)].reset()

        mimedata = QMimeData()
        drag = QDrag(self)
        drag.setMimeData(mimedata)
        drag.exec_(Qt.MoveAction)

    def dropEvent(self, e):  # noqa: N802 - Part of QT signature.
        global selected_button

        self.setStyleSheet(BUTTON_STYLE)

        # Ignore drag and drop on yourself
        if e.source().index == self.index:
            return

        api.swap_buttons(_deck_id(self.ui), _page(self.ui), e.source().index, self.index)
        # In the case that we've dragged the currently selected button, we have to
        # check the target button instead so it appears that it followed the drag/drop
        if e.source().isChecked():
            e.source().setChecked(False)
            self.setChecked(True)
            selected_button = self

        redraw_buttons(self.ui)

    def dragEnterEvent(self, e):  # noqa: N802 - Part of QT signature.
        if type(self) is DraggableButton:
            e.setAccepted(True)
            self.setStyleSheet(BUTTON_DRAG_STYLE)
        else:
            e.setAccepted(False)

    def dragLeaveEvent(self, e):  # noqa: N802 - Part of QT signature.
        self.setStyleSheet(BUTTON_STYLE)


def _replace_special_keys(key):
    """Replaces special keywords the user can use with their character equivalent."""
    if key.lower() == "plus":
        return "+"
    if key.lower() == "comma":
        return ","
    if key.lower().startswith("delay"):
        return key.lower()
    return key


def handle_keypress(deck_id: str, key: int, state: bool) -> None:
    if state:

        if dimmers[deck_id].reset():
            return

        keyboard = Controller()
        page = api.get_page(deck_id)

        command = api.get_button_command(deck_id, page, key)
        if command:
            try:
                Popen(shlex.split(command))
            except Exception as error:
                print(f"The command '{command}' failed: {error}")

        keys = api.get_button_keys(deck_id, page, key)
        if keys:
            keys = keys.strip().replace(" ", "")
            for section in keys.split(","):
                # Since + and , are used to delimit our section and keys to press,
                # they need to be substituted with keywords.
                section_keys = [_replace_special_keys(key_name) for key_name in section.split("+")]

                # Translate string to enum, or just the string itself if not found
                section_keys = [
                    getattr(Key, key_name.lower(), key_name) for key_name in section_keys
                ]

                for key_name in section_keys:
                    if isinstance(key_name, str) and key_name.startswith("delay"):
                        sleep_time_arg = key_name.split("delay", 1)[1]
                        if sleep_time_arg:
                            try:
                                sleep_time = float(sleep_time_arg)
                            except Exception:
                                print(f"Could not convert sleep time to float '{sleep_time_arg}'")
                                sleep_time = 0
                        else:
                            # default if not specified
                            sleep_time = 0.5

                        if sleep_time:
                            try:
                                time.sleep(sleep_time)
                            except Exception:
                                print(f"Could not sleep with provided sleep time '{sleep_time}'")
                    else:
                        try:
                            keyboard.press(key_name)
                        except Exception:
                            print(f"Could not press key '{key_name}'")

                for key_name in section_keys:
                    if not (isinstance(key_name, str) and key_name.startswith("delay")):
                        try:
                            keyboard.release(key_name)
                        except Exception:
                            print(f"Could not release key '{key_name}'")

        write = api.get_button_write(deck_id, page, key)
        if write:
            try:
                keyboard.type(write)
            except Exception as error:
                print(f"Could not complete the write command: {error}")

        brightness_change = api.get_button_change_brightness(deck_id, page, key)
        if brightness_change:
            try:
                api.change_brightness(deck_id, brightness_change)
                dimmers[deck_id].brightness = api.get_brightness(deck_id)
                dimmers[deck_id].reset()
            except Exception as error:
                print(f"Could not change brightness: {error}")

        switch_page = api.get_button_switch_page(deck_id, page, key)
        target_device = api.get_target_device(deck_id, page, key)
        if switch_page:
            api.set_page(target_device, switch_page - 1)


def _deck_id(ui) -> str:
    return ui.device_list.itemData(ui.device_list.currentIndex())


def _page(ui) -> int:
    return ui.pages.currentIndex()


def update_button_text(ui, text: str) -> None:
    deck_id = _deck_id(ui)
    api.set_button_text(deck_id, _page(ui), selected_button.index, text)
    redraw_buttons(ui)


def update_font_size(ui, value: int) -> None:
    deck_id = _deck_id(ui)
    api.set_font_size(deck_id, _page(ui), selected_button.index, value)
    redraw_buttons(ui)


def update_font_color(ui, value: str) -> None:
    deck_id = _deck_id(ui)
    api.set_font_color(deck_id, _page(ui), selected_button.index, value)
    redraw_buttons(ui)


def update_selected_font(ui, value: str) -> None:
    deck_id = _deck_id(ui)
    api.set_selected_font(deck_id, _page(ui), selected_button.index, value)
    redraw_buttons(ui)


def update_feedback_enabled(ui, value: bool) -> None:
    deck_id = _deck_id(ui)
    api.set_feedback_enabled(deck_id, value)
    redraw_buttons(ui)


def update_text_align(ui, value: str) -> None:
    deck_id = _deck_id(ui)
    api.set_text_align(deck_id, _page(ui), selected_button.index, value)
    redraw_buttons(ui)


def update_button_command(ui, command: str) -> None:
    deck_id = _deck_id(ui)
    api.set_button_command(deck_id, _page(ui), selected_button.index, command)


def update_button_keys(ui, keys: str) -> None:
    deck_id = _deck_id(ui)
    api.set_button_keys(deck_id, _page(ui), selected_button.index, keys)


def update_button_write(ui) -> None:
    deck_id = _deck_id(ui)
    api.set_button_write(deck_id, _page(ui), selected_button.index, ui.write.toPlainText())


def update_change_brightness(ui, amount: int) -> None:
    deck_id = _deck_id(ui)
    api.set_button_change_brightness(deck_id, _page(ui), selected_button.index, amount)


def update_switch_page(ui, page: int) -> None:
    deck_id = _deck_id(ui)
    api.set_button_switch_page(deck_id, _page(ui), selected_button.index, page)


def update_target_device(ui, target_device_id: str) -> None:
    deck_id = _deck_id(ui)
    api.set_target_device(deck_id, _page(ui), selected_button.index, target_device_id)


def _highlight_first_button(ui) -> None:
    button = ui.pages.currentWidget().findChildren(QtWidgets.QToolButton)[0]
    button.setChecked(False)
    button.click()


def change_page(ui, page: int) -> None:
    deck_id = _deck_id(ui)
    api.set_page(deck_id, page)
    redraw_buttons(ui)
    _highlight_first_button(ui)
    dimmers[deck_id].reset()


def select_image(window) -> None:
    deck_id = _deck_id(window.ui)
    image = api.get_button_icon(deck_id, _page(window.ui), selected_button.index)
    if not image:
        image = os.path.expanduser("~")

    root = tk.Tk()
    root.withdraw()

    file_name = filedialog.askopenfilename(
        initialdir=os.path.dirname(api.get_last_known_folder(deck_id))
    )

    # file_name = QFileDialog.getOpenFileName(
    #     window, "Open Image", image, "Image Files (*.png *.jpg *.bmp *.gif)"
    # )[0]
    if file_name:
        deck_id = _deck_id(window.ui)
        api.set_button_icon(deck_id, _page(window.ui), selected_button.index, file_name)
        redraw_buttons(window.ui)


def select_image_for_custom_feedback(window) -> None:
    deck_id = _deck_id(window.ui)
    image = api.get_custom_image_for_feedback(deck_id)
    if not image:
        image = os.path.expanduser("~")

    root = tk.Tk()
    root.withdraw()

    file_name = filedialog.askopenfilename(
        initialdir=os.path.dirname(api.get_last_known_folder(deck_id))
    )

    # file_name = QFileDialog.getOpenFileName(
    #     window, "Open Image", image, "Image Files (*.png *.jpg *.bmp *.gif)"
    # )[0]
    if file_name:
        deck_id = _deck_id(window.ui)
        api.set_custom_image_for_feedback(deck_id, file_name)
        api.set_last_known_folder(deck_id, file_name)


def remove_image(window) -> None:
    deck_id = _deck_id(window.ui)
    image = api.get_button_icon(deck_id, _page(window.ui), selected_button.index)
    if image:
        confirm = QMessageBox(window)
        confirm.setWindowTitle("Remove image")
        confirm.setText("Are you sure you want to remove the image for this button?")
        confirm.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        confirm.setIcon(QMessageBox.Question)
        button = confirm.exec_()
        if button == QMessageBox.Yes:
            api.set_button_icon(deck_id, _page(window.ui), selected_button.index, "")
            redraw_buttons(window.ui)


def redraw_buttons(ui) -> None:
    deck_id = _deck_id(ui)
    current_tab = ui.pages.currentWidget()
    buttons = current_tab.findChildren(QtWidgets.QToolButton)
    for button in buttons:
        button.setText(
            api.get_button_text(deck_id, _page(ui), button.index).replace("\\n", os.linesep)
        )
        button.setIcon(QIcon(api.get_button_icon(deck_id, _page(ui), button.index)))


def set_brightness(ui, value: int) -> None:
    deck_id = _deck_id(ui)
    api.set_brightness(deck_id, value)
    dimmers[deck_id].brightness = value
    dimmers[deck_id].reset()


def button_clicked_action(ui: str, deck_id: str, page: int, button: int):
    deck_id = _deck_id(ui)
    button_id = selected_button.index
    ui.text.setText(api.get_button_text(deck_id, _page(ui), button_id))
    ui.text_Align.setCurrentText(api.get_text_align(deck_id, _page(ui), button_id))
    ui.font_Size.setValue(api.get_font_size(deck_id, _page(ui), button_id))
    ui.font_Color.setCurrentText(api.get_font_color(deck_id, _page(ui), button_id))
    ui.command.setText(api.get_button_command(deck_id, _page(ui), button_id))
    ui.keys.setText(api.get_button_keys(deck_id, _page(ui), button_id))
    ui.write.setPlainText(api.get_button_write(deck_id, _page(ui), button_id))
    ui.change_brightness.setValue(api.get_button_change_brightness(deck_id, _page(ui), button_id))
    ui.switch_page.setValue(api.get_button_switch_page(deck_id, _page(ui), button_id))
    ui.target_device.setCurrentText(api.get_target_device(deck_id, _page(ui), button_id))
    ui.selected_font.setCurrentText(api.get_selected_font(deck_id, _page(ui), button_id))
    dimmers[deck_id].reset()


def button_clicked(ui, clicked_button, buttons) -> None:
    global selected_button
    selected_button = clicked_button

    for button in buttons:
        if button == clicked_button:
            continue

        button.setChecked(False)

    selected_button.setFocus()

    deck_id = _deck_id(ui)
    button_id = selected_button.index
    button_clicked_action(ui, deck_id, _page(ui), button_id)



def build_buttons(ui, tab) -> None:
    deck_id = _deck_id(ui)
    deck = api.get_deck(deck_id)

    if hasattr(tab, "deck_buttons"):
        tab.deck_buttons.hide()
        tab.deck_buttons.deleteLater()

    base_widget = QtWidgets.QWidget(tab)
    tab.children()[0].addWidget(base_widget)
    tab.deck_buttons = base_widget

    row_layout = QtWidgets.QVBoxLayout(base_widget)
    index = 0
    buttons = []
    for _row in range(deck["layout"][0]):  # type: ignore
        column_layout = QtWidgets.QHBoxLayout()
        row_layout.addLayout(column_layout)

        for _column in range(deck["layout"][1]):  # type: ignore
            button = DraggableButton(base_widget, ui)
            button.setCheckable(True)
            button.index = index
            button.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.MinimumExpanding)
            button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            button.setIconSize(QSize(100, 100))
            button.setStyleSheet(BUTTON_STYLE)
            buttons.append(button)
            column_layout.addWidget(button)
            index += 1

    for button in buttons:
        button.clicked.connect(
            lambda button=button, buttons=buttons: button_clicked(ui, button, buttons)
        )


def export_config(window) -> None:
    deck_id = _deck_id(window.ui)
    valueLocation = api.get_last_known_export_folder(deck_id)
    file_name = QFileDialog.getSaveFileName(
        window, "Export Config", valueLocation, "JSON (*.json)"
    )[0]
    if not file_name:
        return

    api.set_last_known_export_folder(deck_id, file_name)
    api.export_config(file_name)


def import_config(window) -> None:
    deck_id = _deck_id(window.ui)
    valueLocation = api.get_last_known_import_folder(deck_id)
    root = tk.Tk()
    root.withdraw()

    file_name = filedialog.askopenfilename(
        initialdir=os.path.dirname(api.get_last_known_import_folder(deck_id))
    )

    # file_name = QFileDialog.getOpenFileName(
    #     window, "Import Config", valueLocation, "Config Files (*.json)"
    # )[0]
    if not file_name:
        return

    api.import_config(file_name)
    api.set_last_known_import_folder(deck_id, file_name)
    redraw_buttons(window.ui)


def cut_button(window) -> None:
    deck_id = _deck_id(window.ui)
    api.edit_menu_cut_button(deck_id, _page(window.ui), selected_button.index)
    redraw_buttons(window.ui)
    button_clicked_action(window.ui, deck_id, _page(window.ui), selected_button.index)


def copy_button(window) -> None:
    deck_id = _deck_id(window.ui)
    api.edit_menu_copy_button(deck_id, _page(window.ui), selected_button.index)
    redraw_buttons(window.ui)


def paste_button(window) -> None:
    global multiPasteEnabled

    deck_id = _deck_id(window.ui)
    api.edit_menu_paste_button(deck_id, _page(window.ui), selected_button.index, multiPasteEnabled)
    redraw_buttons(window.ui)
    button_clicked_action(window.ui, deck_id, _page(window.ui), selected_button.index)


def delete_button(window) -> None:
    deck_id = _deck_id(window.ui)
    api.edit_menu_delete_button(deck_id, _page(window.ui), selected_button.index)
    redraw_buttons(window.ui)
    button_clicked_action(window.ui, deck_id, _page(window.ui), selected_button.index)


def multi_paste_Button(window) -> None:
    global multiPasteEnabled

    multiPasteEnabled = not multiPasteEnabled

    if multiPasteEnabled:
        window.ui.actionMultiPaste.setText("Multi Paste Enabled")
    else:
        window.ui.actionMultiPaste.setText("Multi Paste Disabled")

    api.edit_menu_multi_paste_button()
    redraw_buttons(window.ui)
    _highlight_first_button(window.ui)


def sync(ui) -> None:
    api.ensure_decks_connected()
    ui.pages.setCurrentIndex(api.get_page(_deck_id(ui)))


def build_device(ui, _device_index=None) -> None:
    for page_id in range(ui.pages.count()):
        page = ui.pages.widget(page_id)
        page.setStyleSheet("background-color: black")
        build_buttons(ui, page)

    # Set the active page for this device
    ui.pages.setCurrentIndex(api.get_page(_deck_id(ui)))

    # Draw the buttons for the active page
    redraw_buttons(ui)
    sync(ui)
    _highlight_first_button(ui)


class MainWindow(QMainWindow):
    def __init__(self):
        super(MainWindow, self).__init__()
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        self.window_shown: bool = True

    def closeEvent(self, event) -> None:  # noqa: N802 - Part of QT signature.
        self.window_shown = False
        self.hide()
        event.ignore()

    def systray_clicked(self, _status=None) -> None:
        if self.window_shown:
            self.hide()
            self.window_shown = False
            return

        self.bring_to_top()

    def bring_to_top(self):
        self.show()
        self.activateWindow()
        self.raise_()
        self.window_shown = True


def queue_text_change(ui, text: str) -> None:
    global text_timer

    if text_timer:
        text_timer.stop()

    text_timer = QTimer()
    text_timer.setSingleShot(True)
    text_timer.timeout.connect(partial(update_button_text, ui, text))
    text_timer.start(500)


def change_brightness(deck_id: str, brightness: int):
    """Changes the brightness of the given streamdeck, but does not save
    the state."""
    api.decks[deck_id].set_brightness(brightness)


class SettingsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.ui = Ui_SettingsDialog()
        self.ui.setupUi(self)
        self.show()


def show_settings(window) -> None:
    """Shows the settings dialog and allows the user the change deck specific
    settings. Settings are not saved until OK is clicked."""
    ui = window.ui
    main_window = window
    deck_id = _deck_id(ui)
    settings = SettingsDialog(window)
    dimmers[deck_id].stop()

    settings.ui.buttonfeedback.addItem("Disabled")
    settings.ui.buttonfeedback.addItem("Enabled")

    if api.get_feedback_enabled(deck_id) == "Enabled":
        settings.ui.buttonfeedback.setCurrentIndex(1)
    else:
        settings.ui.buttonfeedback.setCurrentIndex(0)

    settings.ui.buttonfeedback.currentTextChanged.connect(partial(update_feedback_enabled, ui))

    location = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ok.png")
    settings.ui.removeButton.clicked.connect(api.set_default_custom_image_for_feedback(deck_id))

    settings.ui.imageButton.clicked.connect(partial(select_image_for_custom_feedback, main_window))

    for label, value in dimmer_options.items():
        settings.ui.dim.addItem(f"{label}", userData=value)

    existing_timeout = api.get_display_timeout(deck_id)
    existing_index = next(
        (i for i, (k, v) in enumerate(dimmer_options.items()) if v == existing_timeout), None
    )

    if existing_index is None:
        settings.ui.dim.addItem(f"Custom: {existing_timeout}s", userData=existing_timeout)
        existing_index = settings.ui.dim.count() - 1
        settings.ui.dim.setCurrentIndex(existing_index)
    else:
        settings.ui.dim.setCurrentIndex(existing_index)

    settings.ui.label_streamdeck.setText(deck_id)
    settings.ui.brightness.setValue(api.get_brightness(deck_id))
    settings.ui.brightness.valueChanged.connect(partial(change_brightness, deck_id))
    if settings.exec_():
        # Commit changes
        if existing_index != settings.ui.dim.currentIndex():
            dimmers[deck_id].timeout = settings.ui.dim.currentData()
            api.set_display_timeout(deck_id, settings.ui.dim.currentData())
        set_brightness(window.ui, settings.ui.brightness.value())
    else:
        # User cancelled, reset to original brightness
        change_brightness(deck_id, api.get_brightness(deck_id))

    dimmers[deck_id].reset()


def dim_all_displays() -> None:
    for _deck_id, dimmer in dimmers.items():
        dimmer.dim(True)


def start(_exit: bool = False) -> None:
    show_ui = True
    if "-h" in sys.argv or "--help" in sys.argv:
        print(f"Usage: {os.path.basename(sys.argv[0])}")
        print("Flags:")
        print("  -h, --help\tShow this message")
        print("  -n, --no-ui\tRun the program without showing a UI")
        return
    elif "-n" in sys.argv or "--no-ui" in sys.argv:
        show_ui = False

    app = QApplication(sys.argv)

    logo = QIcon(LOGO)
    main_window = MainWindow()
    ui = main_window.ui
    main_window.setWindowIcon(logo)
    tray = QSystemTrayIcon(logo, app)
    tray.activated.connect(main_window.systray_clicked)

    menu = QMenu()
    action_dim = QAction("Dim display (toggle)")
    action_dim.triggered.connect(dim_all_displays)
    action_configure = QAction("Configure...")
    action_configure.triggered.connect(main_window.bring_to_top)
    menu.addAction(action_dim)
    menu.addAction(action_configure)
    menu.addSeparator()
    action_exit = QAction("Exit")
    action_exit.triggered.connect(app.exit)
    menu.addAction(action_exit)

    tray.setContextMenu(menu)

    ui.text.textChanged.connect(partial(queue_text_change, ui))
    ui.font_Size.valueChanged.connect(partial(update_font_size, ui))
    ui.command.textChanged.connect(partial(update_button_command, ui))
    ui.keys.textChanged.connect(partial(update_button_keys, ui))
    ui.write.textChanged.connect(partial(update_button_write, ui))
    ui.change_brightness.valueChanged.connect(partial(update_change_brightness, ui))
    ui.switch_page.valueChanged.connect(partial(update_switch_page, ui))
    ui.imageButton.clicked.connect(partial(select_image, main_window))
    ui.removeButton.clicked.connect(partial(remove_image, main_window))
    ui.settingsButton.clicked.connect(partial(show_settings, main_window))

    ui.font_Color.addItem("white")
    ui.font_Color.addItem("black")
    ui.font_Color.addItem("blue")
    ui.font_Color.addItem("red")
    ui.font_Color.addItem("green")
    ui.font_Color.addItem("purple")
    ui.font_Color.addItem("cyan")
    ui.font_Color.addItem("magenta")
    ui.font_Color.currentTextChanged.connect(partial(update_font_color, ui))

    ui.selected_font.addItem("Goblin_One")
    ui.selected_font.addItem("Open_Sans")
    ui.selected_font.addItem("Roboto")
    ui.selected_font.addItem("Lobster")
    ui.selected_font.addItem("Anton")
    ui.selected_font.addItem("Pacifico")
    ui.selected_font.currentTextChanged.connect(partial(update_selected_font, ui))

    ui.text_Align.addItem("left")
    ui.text_Align.addItem("center")
    ui.text_Align.addItem("right")
    ui.text_Align.currentTextChanged.connect(partial(update_text_align, ui))

    api.streamdesk_keys.key_pressed.connect(handle_keypress)

    items = api.open_decks().items()
    if len(items) == 0:
        print("Waiting for Stream Deck(s)...")
        while len(items) == 0:
            time.sleep(3)
            items = api.open_decks().items()

    for deck_id, deck in items:
        ui.device_list.addItem(f"{deck['type']} - {deck_id}", userData=deck_id)
        ui.target_device.addItem(deck_id)
        dimmers[deck_id] = Dimmer(
            api.get_display_timeout(deck_id),
            api.get_brightness(deck_id),
            partial(change_brightness, deck_id),
        )
        dimmers[deck_id].reset()

    build_device(ui)
    ui.device_list.currentIndexChanged.connect(partial(build_device, ui))

    ui.target_device.currentTextChanged.connect(partial(update_target_device, ui))

    ui.pages.currentChanged.connect(partial(change_page, ui))

    ui.actionExport.triggered.connect(partial(export_config, main_window))
    ui.actionImport.triggered.connect(partial(import_config, main_window))

    ui.actionCut.triggered.connect(partial(cut_button, main_window))
    ui.actionCopy.triggered.connect(partial(copy_button, main_window))

    ui.actionCut.setShortcuts([QKeySequence.Cut, QKeySequence("Shift+Del")])
    ui.actionCopy.setShortcuts([QKeySequence.Copy, QKeySequence("Ctrl+Insert")])
    ui.actionPaste.setShortcuts([QKeySequence.Paste, QKeySequence("Shift+Insert")])
    ui.actionDelete.setShortcuts([QKeySequence.Delete])

    ui.actionPaste.triggered.connect(partial(paste_button, main_window))
    ui.actionDelete.triggered.connect(partial(delete_button, main_window))
    ui.actionMultiPaste.triggered.connect(partial(multi_paste_Button, main_window))

    ui.actionExit.triggered.connect(app.exit)

    timer = QTimer()
    timer.timeout.connect(partial(sync, ui))
    timer.start(1000)

    api.render()
    tray.show()

    if show_ui:
        main_window.show()

    if _exit:
        return
    else:
        app.exec_()
        api.close_decks()
        sys.exit()


if __name__ == "__main__":
    start()
