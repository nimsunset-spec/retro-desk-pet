"""Pixel-styled speech bubble with typewriter text and chat input."""

from __future__ import annotations

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEvent,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QGuiApplication, QPainter, QPolygon
from PyQt6.QtWidgets import QLabel, QLineEdit, QVBoxLayout, QWidget


class PixelPanel(QWidget):
    """Paint a crisp, transparent speech panel with a small pointer."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.pointer_side = "bottom"

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(QColor("#26334a"))
        painter.setBrush(QColor("#fff6d8"))
        if self.pointer_side == "top":
            painter.drawRect(2, 15, self.width() - 5, self.height() - 18)
            pointer = QPolygon(
                [QPoint(28, 15), QPoint(42, 15), QPoint(30, 3)]
            )
        else:
            painter.drawRect(2, 2, self.width() - 5, self.height() - 15)
            pointer = QPolygon(
                [
                    QPoint(28, self.height() - 14),
                    QPoint(42, self.height() - 14),
                    QPoint(30, self.height() - 3),
                ]
            )
        painter.drawPolygon(pointer)
        painter.end()


class ChatBubble(QWidget):
    """Small always-on-top chat panel anchored near the desktop pet."""

    message_submitted = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        super().__init__(parent, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(300, 144)
        self._full_text = ""
        self._visible_characters = 0
        self._auto_hide_remaining = 6000

        self.panel = PixelPanel(self)
        self.panel.setGeometry(0, 0, self.width(), self.height())
        self.message_label = QLabel(self.panel)
        self.message_label.setWordWrap(True)
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.message_label.setStyleSheet(
            "color: #26334a; background: transparent; "
            "font-family: Consolas, monospace; font-size: 12px; font-weight: bold;"
        )
        self.message_label.setGeometry(14, 10, 270, 72)

        self.input = QLineEdit(self.panel)
        self.input.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.input.setPlaceholderText("Say something...")
        self.input.setMaxLength(300)
        self.input.setStyleSheet(
            "QLineEdit { color: #26334a; background: #fffdf3; border: 2px solid #6d7487; "
            "padding: 4px; font-family: Consolas, monospace; font-size: 11px; }"
        )
        self.input.setGeometry(12, 88, 276, 28)
        self.input.returnPressed.connect(self._submit)
        self.input.installEventFilter(self)

        self._typewriter = QTimer(self)
        self._typewriter.setInterval(22)
        self._typewriter.timeout.connect(self._advance_text)
        self._auto_hide = QTimer(self)
        self._auto_hide.setSingleShot(True)
        self._auto_hide.timeout.connect(self._begin_auto_hide)
        self._fade_out = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_out.setDuration(240)
        self._fade_out.setStartValue(1.0)
        self._fade_out.setEndValue(0.0)
        self._fade_out.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._fade_out.finished.connect(self._finish_auto_hide)
        self.installEventFilter(self)
        self.hide()

    def show_message(self, message: str) -> None:
        """Show a typewritten message, then dismiss it after six idle seconds."""
        self._full_text = message
        self._visible_characters = 0
        self._auto_hide_remaining = 6000
        self._auto_hide.stop()
        self._fade_out.stop()
        self.setWindowOpacity(1.0)
        self.message_label.clear()
        self.show()
        self.raise_()
        if self._full_text:
            self._typewriter.start()
        else:
            self._resume_auto_hide()

    def show_for_chat(self) -> None:
        """Open an interactive chat panel without scheduling an idle dismissal."""
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self._auto_hide.stop()
        self._fade_out.stop()
        self._typewriter.stop()
        self.setWindowOpacity(1.0)
        self._full_text = ""
        self._visible_characters = 0
        self.message_label.setText("I'm listening, human!")
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus(Qt.FocusReason.OtherFocusReason)

    def hide_immediately(self) -> None:
        """Hide on explicit dismissal and cancel all pending animation."""
        self._typewriter.stop()
        self._auto_hide.stop()
        self._fade_out.stop()
        self.hide()
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowOpacity(1.0)

    def _advance_text(self) -> None:
        self._visible_characters = min(len(self._full_text), self._visible_characters + 2)
        self.message_label.setText(self._full_text[: self._visible_characters])
        if self._visible_characters >= len(self._full_text):
            self._typewriter.stop()
            self._resume_auto_hide()

    def _pause_auto_hide(self) -> None:
        if self._auto_hide.isActive():
            remaining = self._auto_hide.remainingTime()
            self._auto_hide_remaining = max(1, remaining)
            self._auto_hide.stop()
        if self._fade_out.state() == QAbstractAnimation.State.Running:
            self._fade_out.stop()
            self.setWindowOpacity(1.0)
            self._auto_hide_remaining = 6000

    def _resume_auto_hide(self) -> None:
        if (
            self.isVisible()
            and not self._typewriter.isActive()
            and not self.input.hasFocus()
            and not self.underMouse()
            and self._fade_out.state() != QAbstractAnimation.State.Running
        ):
            self._auto_hide.start(self._auto_hide_remaining)

    def _begin_auto_hide(self) -> None:
        if self.underMouse() or self.input.hasFocus():
            return
        self._fade_out.start()

    def _finish_auto_hide(self) -> None:
        self.hide()
        self.setWindowOpacity(1.0)

    def enterEvent(self, event: object) -> None:
        self._pause_auto_hide()
        super().enterEvent(event)

    def leaveEvent(self, event: object) -> None:
        self._resume_auto_hide()
        super().leaveEvent(event)

    def eventFilter(self, watched: object, event: QEvent) -> bool:
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.hide_immediately()
            return True
        if watched is self.input:
            if event.type() in (QEvent.Type.FocusIn, QEvent.Type.MouseButtonPress):
                self._pause_auto_hide()
            elif event.type() == QEvent.Type.FocusOut:
                QTimer.singleShot(0, self._resume_auto_hide)
        return super().eventFilter(watched, event)

    def _submit(self) -> None:
        message = self.input.text().strip()
        if not message:
            return
        self.input.clear()
        self.input.clearFocus()
        self.message_submitted.emit(message)

    def position_near(self, global_position: QPoint) -> None:
        """Flip beside the pet when needed and clamp the panel to its screen."""
        parent = self.parentWidget()
        pet_rect = (
            parent.frameGeometry()
            if parent is not None
            else None
        )
        if pet_rect is None or pet_rect.isNull():
            anchor = global_position
            pet_rect = self.rect()
            pet_rect.moveCenter(anchor)

        screen = QGuiApplication.screenAt(pet_rect.center())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else pet_rect

        x = pet_rect.center().x() - self.width() // 2
        gap = 8
        above_y = pet_rect.top() - self.height() - gap
        below_y = pet_rect.bottom() + 1 + gap
        above_fits = above_y >= available.top()
        below_fits = below_y + self.height() - 1 <= available.bottom()

        if above_fits:
            y = above_y
            self.panel.pointer_side = "bottom"
        elif below_fits:
            y = below_y
            self.panel.pointer_side = "top"
        else:
            space_above = pet_rect.top() - available.top()
            space_below = available.bottom() - pet_rect.bottom()
            if space_below > space_above:
                y = below_y
                self.panel.pointer_side = "top"
            else:
                y = above_y
                self.panel.pointer_side = "bottom"

        max_x = max(available.left(), available.right() - self.width() + 1)
        max_y = max(available.top(), available.bottom() - self.height() + 1)
        self.move(
            min(max(x, available.left()), max_x),
            min(max(y, available.top()), max_y),
        )
        if self.panel.pointer_side == "top":
            self.message_label.setGeometry(14, 20, 270, 62)
        else:
            self.message_label.setGeometry(14, 10, 270, 72)
        self.panel.update()
