"""Transparent, draggable desktop pet and system-tray controls."""

from __future__ import annotations

import math
from pathlib import Path

from PyQt6.QtCore import QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QCursor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QMenu,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)


SPRITE_DIR = Path(__file__).resolve().parent.parent / "assets" / "sprites"
PET_STATES = {"idle", "happy", "sleepy", "hungry", "sassy"}


class AnimatedSprite(QLabel):
    """Draw the current pixel sprite with a low-cost state-specific motion."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background: transparent;")
        self._state = "idle"
        self._phase = 0.0
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(70)
        self._frame_timer.timeout.connect(self._advance_frame)
        self._frame_timer.start()

    def set_state(self, state: str, pixmap: QPixmap) -> None:
        self._state = state
        self.setPixmap(pixmap)
        self.update()

    def _advance_frame(self) -> None:
        self._phase = (self._phase + 0.18) % (2 * math.pi)
        self.update()

    def paintEvent(self, event: object) -> None:
        pixmap = self.pixmap()
        if pixmap.isNull():
            return

        phase = self._phase
        y_offset = 0.0
        angle = 0.0
        scale = 0.92
        if self._state == "happy":
            y_offset = -abs(math.sin(phase * 2.0)) * 3.0
            angle = math.sin(phase * 2.0) * 2.5
            scale += abs(math.sin(phase * 2.0)) * 0.035
        elif self._state == "sleepy":
            y_offset = math.sin(phase * 0.45) * 1.0
            scale += math.sin(phase * 0.45) * 0.018
        elif self._state == "hungry":
            angle = math.sin(phase * 2.8) * 2.0
            y_offset = abs(math.sin(phase * 2.8)) * 0.8
        elif self._state == "sassy":
            angle = math.sin(phase * 0.8) * 3.0
            y_offset = math.sin(phase * 1.6) * 1.0
        else:
            y_offset = math.sin(phase) * 1.2
            scale += math.sin(phase * 0.7) * 0.012

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.translate(self.width() / 2.0, self.height() / 2.0 + y_offset)
        painter.rotate(angle)
        painter.scale(scale, scale)
        painter.drawPixmap(-pixmap.width() // 2, -pixmap.height() // 2, pixmap)
        painter.end()


class PetWindow(QWidget):
    """Always-on-top frameless sprite with care and tray menus."""

    clicked = pyqtSignal()
    position_changed = pyqtSignal()
    action_requested = pyqtSignal(str)
    stats_requested = pyqtSignal()

    def __init__(self, pet_name: str = "Pixel") -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle(f"{pet_name} - Retro Desktop Pet")
        self.setFixedSize(64, 64)
        self._drag_offset: QPoint | None = None
        self._press_position: QPoint | None = None
        self._dragged = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.sprite = AnimatedSprite(self)
        self.sprite.setFixedSize(64, 64)
        self.sprite.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name_label = QLabel(pet_name, self)
        self.name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.name_label.setStyleSheet(
            "color: #fff6d8; background: rgba(38, 51, 74, 190); "
            "font-family: Consolas, monospace; font-size: 10px; font-weight: bold; "
            "padding: 2px 5px;"
        )
        self.name_label.hide()
        layout.addWidget(self.sprite, alignment=Qt.AlignmentFlag.AlignCenter)

        self._tray: QSystemTrayIcon | None = None
        self._tray_menu: QMenu | None = None
        self.set_state("idle")
        self._create_tray_icon()

    def set_name(self, name: str) -> None:
        self.name_label.setText(name)
        self.setWindowTitle(f"{name} - Retro Desktop Pet")

    def set_state(self, state: str) -> None:
        if state not in PET_STATES:
            raise ValueError(f"Unknown pet sprite state: {state}")
        sprite_path = SPRITE_DIR / f"{state}.png"
        if not sprite_path.is_file():
            raise FileNotFoundError(f"Pet sprite does not exist: {sprite_path}")
        pixmap = QPixmap(str(sprite_path))
        if pixmap.isNull():
            raise OSError(f"Could not load pet sprite: {sprite_path}")
        self.sprite.set_state(state, pixmap)

    def _create_tray_icon(self) -> None:
        self._tray_menu = self._build_context_menu()
        icon_path = SPRITE_DIR / "idle.png"
        if not QSystemTrayIcon.isSystemTrayAvailable() or not icon_path.is_file():
            return
        icon = QIcon(str(icon_path))
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("Retro Desktop Pet")
        self._tray.setContextMenu(self._tray_menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    def _build_context_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #fff6d8; color: #26334a; "
            "font-family: Consolas, monospace; border: 2px solid #26334a; }"
            "QMenu::item:selected { background: #ffd166; }"
        )
        feed = QAction("Feed", menu)
        pet = QAction("Pet", menu)
        stats = QAction("Stats", menu)
        quit_action = QAction("Quit", menu)
        feed.triggered.connect(lambda checked=False: self.action_requested.emit("feed"))
        pet.triggered.connect(lambda checked=False: self.action_requested.emit("pet"))
        stats.triggered.connect(self.stats_requested.emit)
        quit_action.triggered.connect(QApplication.instance().quit)
        menu.addAction(feed)
        menu.addAction(pet)
        menu.addSeparator()
        menu.addAction(stats)
        menu.addSeparator()
        menu.addAction(quit_action)
        return menu

    def contextMenuEvent(self, event: object) -> None:
        if self._tray_menu is not None:
            self._tray_menu.exec(QCursor.pos())
        event.accept()

    def mousePressEvent(self, event: object) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_position = event.globalPosition().toPoint()
            self._drag_offset = self._press_position - self.frameGeometry().topLeft()
            self._dragged = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: object) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            current_position = event.globalPosition().toPoint()
            if (
                self._press_position is not None
                and (current_position - self._press_position).manhattanLength()
                >= QApplication.startDragDistance()
            ):
                self._dragged = True
            if self._dragged:
                self.move(current_position - self._drag_offset)
                self.position_changed.emit()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: object) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._press_position is not None and not self._dragged:
                self.clicked.emit()
            self._press_position = None
            self._drag_offset = None
            self._dragged = False
            event.accept()
            return
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.setVisible(not self.isVisible())

    def closeEvent(self, event: object) -> None:
        if self._tray is not None:
            self._tray.hide()
        event.accept()
