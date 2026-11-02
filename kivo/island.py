"""Dynamic Island widget for KIVO.

A floating pill-shaped indicator at the top-center of the screen.
Shows status and provides quick access to the Dashboard.

Features:
- Dynamic Island appearance (pill shape with glow)
- Color-coded status indicators
- Tap for Dashboard
- Long press for PTT
- Center alignment
"""
from __future__ import annotations

import time
from typing import Callable

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QBrush
from PyQt6.QtWidgets import QWidget

from kivo.core.state import KivoState, KivoStateType
class DynamicIsland(QWidget):
    """Dynamic Island widget for KIVO assistant status display.

    A pill-shaped widget at the top-center of the screen that shows the
    current assistant status and provides quick access to the Dashboard.
    """

    # Signals for user interactions
    clicked = pyqtSignal()
    long_pressed = pyqtSignal()

    # Size constants
    MIN_WIDTH = 180
    HEIGHT = 36
    CORNER_RADIUS = 18
    EXPANDED_WIDTH = 260

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        # Set up the widget appearance
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.ToolTip)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        # State management
        self._state = KivoStateType.IDLE
        self._is_expanded = False
        self._last_click_time = 0
        self._press_timer = QTimer(self)
        self._press_timer.setSingleShot(True)
        self._press_timer.timeout.connect(self._handle_long_press)

        # Colors for different states
        self._colors = {
            KivoStateType.IDLE: {"bg": QColor(44, 44, 44, 200), "text": QColor(200, 200, 200)},
            KivoStateType.LISTENING: {"bg": QColor(0, 100, 200, 220), "text": QColor(255, 255, 255)},
            KivoStateType.THINKING: {"bg": QColor(100, 50, 150, 220), "text": QColor(255, 255, 255)},
            KivoStateType.SPEAKING: {"bg": QColor(255, 100, 50, 220), "text": QColor(255, 255, 255)},
            KivoStateType.DRIVING: {"bg": QColor(0, 150, 100, 220), "text": QColor(255, 255, 255)},
            KivoStateType.ERROR: {"bg": QColor(200, 50, 50, 220), "text": QColor(255, 255, 255)},
        }

        # Set up the UI
        self._setup_ui()

    def _setup_ui(self) -> None:
        """Set up the UI components."""
        self.setFixedSize(self.MIN_WIDTH, self.HEIGHT)

        # Create layout
        from PyQt6.QtWidgets import QHBoxLayout, QLabel

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(8)

        # Status indicator
        self._status_indicator = QLabel()
        self._status_indicator.setFixedSize(8, 8)
        self._status_indicator.setStyleSheet("""
            QLabel {
                background-color: #00FF00;
                border-radius: 4px;
                color: transparent;
            }
        """)
        layout.addWidget(self._status_indicator)

        # Text label
        self._status_label = QLabel("Waiting for command")
        self._status_label.setFont(QFont("Segoe UI", 10))
        self._status_label.setStyleSheet("color: white; border: none;")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setSizePolicy(1, 1)
        layout.addWidget(self._status_label)

        # Make the widget transparent to pass clicks through to parent
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)

        # Install event filter for click and press handling
        self.installEventFilter(self)

    def set_state(self, state: KivoStateType) -> None:
        """Update the Dynamic Island state.

        Args:
            state: The new state
        """
        self._state = state
        self._update_display()

    def set_text(self, text: str) -> None:
        """Set the text displayed in the Dynamic Island.

        Args:
            text: The text to display
        """
        self._status_label.setText(text)

    def expand(self, expanded: bool) -> None:
        """Expand or collapse the Dynamic Island.

        Args:
            expanded: True to expand, False to collapse
        """
        self._is_expanded = expanded
        if expanded:
            self.setFixedWidth(self.EXPANDED_WIDTH)
        else:
            self.setFixedWidth(self.MIN_WIDTH)
        self._update_display()

    def _update_display(self) -> None:
        """Update the display based on current state."""
        # Get colors for current state
        colors = self._colors.get(self._state, self._colors[KivoStateType.IDLE])

        # Update background color
        bg_color = colors["bg"]
        text_color = colors["text"]

        # Set stylesheet
        self.setStyleSheet(f"""
            DynamicIsland {{
                background-color: {bg_color.name()};
                border-radius: {self.CORNER_RADIUS}px;
                border: 1px solid {QColor(255, 255, 255, 50).name()};
            }}
            QLabel {{
                color: {text_color.name()};
                font-family: Segoe UI;
                font-size: 10px;
                font-weight: 500;
            }}
        """)

        # Update status indicator color
        if self._state == KivoStateType.LISTENING:
            self._status_indicator.setStyleSheet("""
                QLabel {{
                    background-color: #00FF00;
                    border-radius: 4px;
                    color: transparent;
                }}
            """)
        elif self._state == KivoStateType.THINKING:
            self._status_indicator.setStyleSheet("""
                QLabel {{
                    background-color: #FFA500;
                    border-radius: 4px;
                    color: transparent;
                }}
            """)
        elif self._state == KivoStateType.SPEAKING:
            self._status_indicator.setStyleSheet("""
                QLabel {{
                    background-color: #FF0000;
                    border-radius: 4px;
                    color: transparent;
                }}
            """)
        elif self._state == KivoStateType.DRIVING:
            self._status_indicator.setStyleSheet("""
                QLabel {{
                    background-color: #00FFFF;
                    border-radius: 4px;
                    color: transparent;
                }}
            """)
        else:
            self._status_indicator.setStyleSheet("""
                QLabel {{
                    background-color: #808080;
                    border-radius: 4px;
                    color: transparent;
                }}
            """)

    def eventFilter(self, obj: QWidget, event: QEvent) -> bool:
        """Handle mouse events for the Dynamic Island.

        Args:
            obj: The object that received the event
            event: The event

        Returns:
            True if the event was handled, False otherwise
        """
        if obj is self:
            if event.type() == event.Type.MouseButtonPress:
                self._last_click_time = time.time()
                self._press_timer.start(300)  # 300ms long press threshold
                return False
            elif event.type() == event.Type.MouseButtonRelease:
                self._press_timer.stop()

                # Check if it's a simple click (not a long press)
                if time.time() - self._last_click_time < 0.3:
                    self.clicked.emit()
                return True
            elif event.type() == event.Type.MouseMove:
                # Stop long press timer if mouse moves
                self._press_timer.stop()

        return super().eventFilter(obj, event)

    def _handle_long_press(self) -> None:
        """Handle long press (PTT)."""
        self.long_pressed.emit()