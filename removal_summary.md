# UI Removal Summary

## Files/Directories Removed:
- main.py - Qt entry point
- ui.py - Qt UI components
- dashboard/ - Qt-based dashboard (web server)
- systemd/ - Qt-based systemd units
- .gitignore - kept, will be updated
- readme.md - will be updated
- kivo.log - kept for logs

## Files/Directories Kept:
- actions/ - core tool implementations
- core/ - all backend: brain, screen_context, drive, task_orchestrator, action_policy, layered_memory, etc.
- memory/ - memory management
- providers/ - audio/stt/llm providers
- config/ - configuration
- tests/ - existing tests
- venv/.venv - Python environment