"""Provider selection list widget."""

from __future__ import annotations

import curses
from typing import List, Tuple

from claude_hub.domain import ProviderRef, ProviderInspection
from claude_hub.ui.layout.widget_protocol import Widget, InputAction


class ProviderListWidget(Widget):
    """Scrollable provider list with navigation."""
    
    def __init__(self, 
                 providers: tuple[ProviderRef, ...],
                 inspections: dict[str, ProviderInspection],
                 selected_provider_id: str | None = None,
                 single_mode: bool = False):  # NEW: Single channel direct mode
        """Initialize provider list.
        
        Args:
            providers: All available providers
            inspections: Mapping of provider_id -> inspection data
            selected_provider_id: Currently selected (cursor) provider
            single_mode: If True, show left arrow for single-channel direct connect
        """
        self.providers = list(providers)
        self.inspections = inspections
        self.single_mode = len(providers) == 1  # Auto-detect single mode
        if single_mode:
            self.single_mode = True
            self.selected_index = 0
        else:
            self.selected_index = 0 if selected_provider_id is None else max(
                0, next((i for i, p in enumerate(providers) 
                        if p.provider_id == selected_provider_id), 0)
            )
        self.scroll_offset = 0
        self.focus = False
        self.max_display_rows = 10
        
    @property
    def visible_count(self) -> int:
        """Number of items to display."""
        return min(len(self.providers), self.max_display_rows)
    
    def render(self) -> str:
        """Render provider list as ASCII art."""
        if not self.providers:
            return self._empty_state()
        
        # Calculate visible range
        visible_range = self._get_visible_range()
        items = self.providers[visible_range[0]:visible_range[1]]
        
        lines = []
        lines.append("┌────────────────────────────────────────────┐")
        
        for idx, provider in enumerate(items):
            actual_idx = idx + visible_range[0]
            is_selected = actual_idx == self.selected_index
            display_idx = actual_idx + 1
            
            # Get model info from inspection
            models_str = ""
            if provider.provider_id in self.inspections:
                models = self.inspections[provider.provider_id].models
                models_dict = models.to_public_dict()
                if models_dict:
                    first_model = next(iter(models_dict.values()))
                    models_str = f" • {first_model[:20]}"
            
            # Build line based on mode
            if self.single_mode:
                # SINGLE CHANNEL: LEFT ARROW direct connect (no number)
                left_arrow = "◀"
                line = f"{left_arrow}  {provider.display_name or provider.provider_id:<45}{models_str}"
            else:
                # MULTI CHANNEL: NUMBERED LIST with selection
                prefix = "▸ " if is_selected else "  "
                line = f"{prefix}{display_idx}  {provider.display_name or provider.provider_id:<34}{models_str}"
            
            # Truncate if too long
            line = line[:76]
            lines.append(line.ljust(78) + "│")
                
        # Footer based on mode
        if self.single_mode:
            footer_lines = [
                "单渠道直连模式",
                "",
                "Enter 直连启动 · q 退出",
            ]
        else:
            total = len(self.providers)
            footer_lines = [
                f"共{total}个 • ?更多操作 · q 退出",
                "",
                "↑↓/j k 移动  ·  Enter 启动  · 数字直达",
            ]
                
        lines.extend(f"└{line:^76}┘" for line in footer_lines)
        lines[-1] = "└" + lines[-1][1:-1] + "┘"
        
        return "\n".join(lines)
    
    def _empty_state(self) -> str:
        """Render empty state message."""
        return """
┌────────────────────────────────────────────┐
│                                            │
│         没有可用的渠道配置                   │
│                                            │
│  请先运行 CC Switch 并添加一个 Claude        │
│  provider，然后再次尝试。                  │
│                                            │
│                         [按 Enter 返回]       │
└────────────────────────────────────────────┘
"""
    
    def _get_visible_range(self) -> Tuple[int, int]:
        """Calculate visible item range based on scroll offset."""
        start = self.scroll_offset
        end = min(start + self.visible_count, len(self.providers))
        return start, end
    
    def on_input(self, key: int) -> InputAction:
        """Handle keyboard input.
        
        Key codes:
        - curses.KEY_UP / ord('k'): Navigate up
        - curses.KEY_DOWN / ord('j'): Navigate down
        - ord('1')-ord('9'), ord('0'): Direct selection by number
        - ord('q'), 27 (Esc): Exit
        - ord('/'): Search mode (placeholder)
        """
        # Navigation
        if key in (curses.KEY_UP, ord('k')):
            self.selected_index = max(0, self.selected_index - 1)
            self._update_scroll()
            return InputAction.navigate_up()
        
        elif key in (curses.KEY_DOWN, ord('j')):
            self.selected_index = min(len(self.providers) - 1, 
                                     self.selected_index + 1)
            self._update_scroll()
            return InputAction.navigate_down()
        
        # Direct selection (1-9, 0)
        elif key in (ord('1'), ord('2'), ord('3'), ord('4'), ord('5'),
                    ord('6'), ord('7'), ord('8'), ord('9'), ord('0')):
            num = key - ord('0')
            if 0 < num <= len(self.providers):
                self.selected_index = num - 1
                return InputAction.select(self.providers[self.selected_index].provider_id)
        
        # Exit
        elif key in (ord('q'), 27):  # 27 = Esc
            return InputAction.exit()
        
        # Help
        elif key == ord('?'):
            return InputAction.none(data={"action": "show_help"})
        
        else:
            return InputAction.none()
    
    def _update_scroll(self) -> None:
        """Update scroll position to keep selected item visible."""
        if self.selected_index < self.scroll_offset:
            self.scroll_offset = self.selected_index
        elif self.selected_index >= self.scroll_offset + self.visible_count:
            self.scroll_offset = self.selected_index - self.visible_count + 1
    
    def set_focus(self, focused: bool) -> None:
        """Set focus state."""
        self.focus = focused
    
    def is_focused(self) -> bool:
        """Check if widget has focus."""
        return self.focus
    
    def get_selected_provider(self) -> ProviderRef | None:
        """Return currently selected provider."""
        if self.providers and 0 <= self.selected_index < len(self.providers):
            return self.providers[self.selected_index]
        return None
