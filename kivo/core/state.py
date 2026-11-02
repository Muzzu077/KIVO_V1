"""KIVO state management.

Enum types for KIVO assistant states and status codes.
"""
from enum import Enum, auto


class KivoStateType(Enum):
    """Enumeration of KIVO assistant states.

    These states are used by the Dynamic Island widget to display
    the current assistant status.
    """

    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()
    DRIVING = auto()
    ERROR = auto()

    # Additional states for more granular control
    WAKE_WORD = auto()
    VOICE_COMMAND = auto()
    CANCELLATION = auto()
    FULL_SCREEN_MODE = auto()
    SETTINGS_MODE = auto()
    DASHBOARD_OPEN = auto()
    MUTE = auto()

    # State transition helpers
    def is_active(self) -> bool:
        """Check if this state requires active processing.

        Returns:
            True if the state requires active processing, False otherwise
        """
        return self in (
            KivoStateType.LISTENING,
            KivoStateType.THINKING,
            KivoStateType.SPEAKING,
            KivoStateType.DRIVING,
            KivoStateType.WAKE_WORD,
            KivoStateType.VOICE_COMMAND,
            KivoStateType.CANCELLATION,
        )

    def requires_ui(self) -> bool:
        """Check if this state requires UI updates.

        Returns:
            True if the state requires UI updates, False otherwise
        """
        return self in (
            KivoStateType.LISTENING,
            KivoStateType.THINKING,
            KivoStateType.SPEAKING,
            KivoStateType.DRIVING,
            KivoStateType.ERROR,
            KivoStateType.FULL_SCREEN_MODE,
            KivoStateType.DASHBOARD_OPEN,
        )

    def to_display_name(self) -> str:
        """Convert state to a human-readable display name.

        Returns:
            The display name for the state
        """
        mapping = {
            KivoStateType.IDLE: "Idle",
            KivoStateType.LISTENING: "Listening",
            KivoStateType.THINKING: "Thinking",
            KivoStateType.SPEAKING: "Speaking",
            KivoStateType.DRIVING: "Driving",
            KivoStateType.ERROR: "Error",
            KivoStateType.WAKE_WORD: "Wake Word",
            KivoStateType.VOICE_COMMAND: "Voice Command",
            KivoStateType.CANCELLATION: "Cancellation",
            KivoStateType.FULL_SCREEN_MODE: "Full Screen",
            KivoStateType.SETTINGS_MODE: "Settings",
            KivoStateType.DASHBOARD_OPEN: "Dashboard",
            KivoStateType.MUTE: "Mute",
        }
        return mapping.get(self, str(self))

    def to_status_emoji(self) -> str:
        """Convert state to a status emoji.

        Returns:
            The emoji representing the state
        """
        mapping = {
            KivoStateType.IDLE: "⏸️",
            KivoStateType.LISTENING: "🎤",
            KivoStateType.THINKING: "🧠",
            KivoStateType.SPEAKING: "🗣️",
            KivoStateType.DRIVING: "🚗",
            KivoStateType.ERROR: "⚠️",
            KivoStateType.WAKE_WORD: "👂",
            KivoStateType.VOICE_COMMAND: "💬",
            KivoStateType.CANCELLATION: "❌",
            KivoStateType.FULL_SCREEN_MODE: "📺",
            KivoStateType.SETTINGS_MODE: "⚙️",
            KivoStateType.DASHBOARD_OPEN: "📊",
            KivoStateType.MUTE: "🔇",
        }
        return mapping.get(self, "❓")