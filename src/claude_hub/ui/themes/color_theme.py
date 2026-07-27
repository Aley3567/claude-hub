"""Color theme system for TUI."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    """Terminal color configuration (256-color palette)."""
    
    # Primary colors (Orange theme matching Claude.ai)
    primary_bg: int = 214      # Bright orange
    primary_fg: int = 236      # Dark gray
    
    # Semantic colors
    success: int = 46          # Green
    warning: int = 208         # Amber
    error: int = 196           # Red
    info: int = 75             # Blue
    
    # UI elements
    border: int = 252          # Light gray
    highlight_bg: int = 235    # Selected item background
    highlight_fg: int = 231    # Selected item text (white)
    cursor: int = 231          # Cursor color
    muted: int = 242           # Dimmed text (help text)
    logo_accent: int = 214     # Logo accent color
    
    @classmethod
    def default(cls) -> "Theme":
        """Return default theme configuration."""
        return cls()
    
    @property
    def colors(self) -> dict[str, int]:
        """Export all colors as a dictionary."""
        return {
            "primary_bg": self.primary_bg,
            "primary_fg": self.primary_fg,
            "success": self.success,
            "warning": self.warning,
            "error": self.error,
            "info": self.info,
            "border": self.border,
            "highlight_bg": self.highlight_bg,
            "highlight_fg": self.highlight_fg,
            "cursor": self.cursor,
            "muted": self.muted,
            "logo_accent": self.logo_accent,
        }


# Predefined themes
DEFAULT_THEME = Theme.default()
DARK_THEME = Theme(
    primary_bg=236,
    primary_fg=253,
    border=240,
)

LIGHT_THEME = Theme(
    primary_bg=215,
    primary_fg=236,
    border=250,
    muted=102,
)
