"""Key bindings and input handling."""

import os
import sys


class KeyBindings:
    """Configurable key binding system."""
    
    # Default bindings
    DEFAULT_BINDINGS = {
        # Navigation
        "up": [65, ord('k'), ord('K')],       # curses.KEY_UP alternatives
        "down": [66, ord('j'), ord('J')],     # curses.KEY_DOWN alternatives
        "left": [68, ord('h'), ord('H')],     # curses.KEY_LEFT alternatives
        "right": [ord('l'), ord('L')],        # cursor movement
        
        # Actions
        "select": [10],                         # Enter
        "exit": [ord('q'), 27],                # q or Esc
        "cancel": [27],                         # Esc
        "help": [ord('?'), ord('/')],          # Help
        "clear_input": [21],                    # Ctrl+U
        
        # Number shortcuts (1-9, 0)
        "number_1": [ord('1')],
        "number_2": [ord('2')],
        "number_3": [ord('3')],
        "number_4": [ord('4')],
        "number_5": [ord('5')],
        "number_6": [ord('6')],
        "number_7": [ord('7')],
        "number_8": [ord('8')],
        "number_9": [ord('9')],
        "number_0": [ord('0')],
    }
    
    def __init__(self, bindings: dict | None = None):
        """Initialize bindings.
        
        Args:
            bindings: Custom binding map to override defaults
        """
        self.bindings = {**self.DEFAULT_BINDINGS}
        if bindings:
            self.bindings.update(bindings)
    
    def get_keys(self, action: str) -> list[int]:
        """Get all key codes for an action."""
        return self.bindings.get(action, [])
    
    def matches(self, key: int, action: str) -> bool:
        """Check if a key matches an action.
        
        Args:
            key: Raw keyboard code
            action: Action name to check
            
        Returns:
            True if key triggers the action
        """
        return key in self.get_keys(action)
    
    @classmethod
    def from_environment(cls) -> "KeyBindings":
        """Create bindings from environment variables.
        
        Environment:
            CLAUDE1_KEYMAP: vim | emacs | custom
        
        Returns:
            Configured KeyBindings instance
        """
        keymap = os.environ.get("CLAUDE1_KEYMAP", "vim").lower()
        
        if keymap == "emacs":
            return cls({
                "up": [curses.KEY_UP, ord('p')],
                "down": [curses.KEY_DOWN, ord('n')],
                "left": [curses.KEY_LEFT, ord('b')],
                "right": [curses.KEY_RIGHT, ord('f')],
            })
        elif keymap == "custom":
            # Load from CLAUDE1_CUSTOM_BINDINGS JSON
            import json
            custom = os.environ.get("CLAUDE1_CUSTOM_BINDINGS")
            if custom:
                try:
                    return cls(json.loads(custom))
                except json.JSONDecodeError:
                    pass
        elif keymap == "default" or keymap == "vim":
            pass  # Use default bindings
        
        return cls()
    
    def to_dict(self) -> dict[str, list[int]]:
        """Export bindings as dictionary."""
        return {**self.bindings}


def read_key_non_blocking(stdscr) -> int | None:
    """Read a single key without blocking.
    
    Args:
        stdscr: Curses standard screen
        
    Returns:
        Key code or None if no key available
    """
    stdscr.timeout(0)  # Non-blocking mode
    try:
        key = stdscr.getch()
        return key if key != -1 else None
    except Exception:
        return None


def setup_curses_defaults(stdscr) -> None:
    """Configure curses terminal settings.
    
    Sets up:
    - No echo of typed characters
    - Enable mouse support (optional)
    - 256 color support
    - Function keys
    """
    curses.curs_set(0)           # Hide cursor
    stdscr.keypad(True)         # Enable function keys
    curses.start_color()
    curses.use_default_colors()
    
    # Support 256 colors if terminal supports it
    if curses.has_colors():
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_WHITE)


class InputReader:
    """Thread-safe keyboard input reader."""
    
    def __init__(self):
        import queue
        self.queue: queue.Queue[int | None] = queue.Queue(maxsize=1)
    
    def push_key(self, key: int | None) -> None:
        """Add a key to the input queue."""
        try:
            self.queue.put_nowait(key)
        except queue.Full:
            pass  # Drop oldest if queue full
    
    def pop_key(self) -> int | None:
        """Remove and return next key, or None if empty."""
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            return None
