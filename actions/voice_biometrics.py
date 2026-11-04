"""
actions/voice_biometrics.py — Voice biometrics and speaker verification action tool.

Allows KIVO to check voice security status, toggle speaker verification,
or adjust verification sensitivity.
"""
from __future__ import annotations

from core.speaker_verifier import get_speaker_verifier


def voice_biometrics_handler(parameters: dict, **kwargs) -> str:
    action = parameters.get("action", "status").lower().strip()
    sv = get_speaker_verifier()

    if action == "status":
        if not sv.is_enrolled():
            return "Speaker verification is not yet configured. No voice profile has been enrolled."
        enrolled_name = sv._profile.speaker_name if sv._profile else "Owner"
        state = "ACTIVE & PROTECTING" if sv.is_enabled() else "DISABLED"
        thresh = sv._profile.threshold if sv._profile else 0.94
        return f"Speaker Verification is {state} for user '{enrolled_name}'. Security threshold: {thresh:.2f}."

    elif action in ("enable", "activate"):
        if not sv.is_enrolled():
            return "Cannot enable speaker verification: please enroll your voice first."
        sv.set_enabled(True)
        return "Speaker verification is now enabled. KIVO will only accept commands from your voice."

    elif action in ("disable", "deactivate"):
        sv.set_enabled(False)
        return "Speaker verification has been disabled. KIVO will accept commands from any voice."

    elif action in ("set_threshold", "sensitivity"):
        thresh_val = parameters.get("threshold")
        if thresh_val is None:
            return "Please specify a threshold value between 0.80 (relaxed) and 0.98 (strict)."
        try:
            val = float(thresh_val)
            sv.set_threshold(val)
            return f"Voice biometric verification threshold updated to {val:.2f}."
        except ValueError:
            return f"Invalid threshold value '{thresh_val}'. Must be a number."

    return f"Unknown action '{action}'. Valid actions: status, enable, disable, set_threshold."


TOOL = {
    "name": "voice_biometrics",
    "description": (
        "Controls speaker verification and voice biometric security. "
        "Allows KIVO to check voice recognition status, enable or disable "
        "speaker gating, and adjust the verification security threshold."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Action to perform: 'status', 'enable', 'disable', 'set_threshold'."
            },
            "threshold": {
                "type": "NUMBER",
                "description": "Threshold between 0.80 and 0.98 when action is 'set_threshold'."
            }
        },
        "required": ["action"]
    },
    "handler": voice_biometrics_handler,
}
