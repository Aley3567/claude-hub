"""Claude Hub UI - Modular terminal interface."""

# Lazy imports to avoid circular dependencies
try:
    from .components.provider_list import ProviderListWidget
    __all__ = ["ProviderListWidget"]
except ImportError:
    # Fallback if dependencies not ready
    pass

try:
    from .components.model_editor import ModelEditorWidget
    if 'all' not in dir():
        __all__ = []
    __all__.append("ModelEditorWidget")
except ImportError:
    pass

try:
    from .animation.logo_animator import LogoAnimator
    if 'all' not in dir():
        __all__ = []
    __all__.append("LogoAnimator")
except ImportError:
    pass

try:
    from .input.keybindings import KeyBindings, InputAction
    if 'all' not in dir():
        __all__ = []
    __all__.extend(["KeyBindings", "InputAction"])
except ImportError:
    pass

try:
    from .app import ClaudeHubTUI
    if 'all' not in dir():
        __all__ = []
    __all__.append("ClaudeHubTUI")
except ImportError:
    pass
