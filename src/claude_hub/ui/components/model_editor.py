"""Model editor widget with NORMAL/INSERT modes."""

from __future__ import annotations

from claude_hub.ui.layout.widget_protocol import Widget, InputAction
from claude_hub.domain import ModelMapping, ProviderRef, ProviderInspection


class EditorMode:
    """Editor state modes."""
    NORMAL = "NORMAL"
    INSERT = "INSERT"


class ModelEditorWidget(Widget):
    """Model configuration editor with vim-like modes."""
    
    def __init__(self, 
                 provider: ProviderRef,
                 inspection: ProviderInspection):
        """Initialize editor.
        
        Args:
            provider: Provider reference being edited
            inspection: Full inspection data with current models
        """
        self.provider = provider
        self.inspection = inspection
        self.mode = EditorMode.NORMAL
        self.current_field = "default"  # default, fast, reasoning, coding, long_context, fallback
        self.cursor_pos = 0
        self.edit_buffer = ""
        
    @property
    def field_order(self) -> list[str]:
        """Order of model fields to edit."""
        return ["default", "fast", "reasoning", "coding", "long_context", "fallback"]
    
    @property
    def field_labels(self) -> dict[str, str]:
        """Human-readable labels for each field."""
        return {
            "default": "default (主模型)",
            "fast": "fast (快速模型)",
            "reasoning": "reasoning (推理模型)",
            "coding": "coding (编程模型)",
            "long_context": "long context (长文本模型)",
            "fallback": "fallback (降级模型)",
        }
    
    def render(self) -> str:
        """Render editor interface."""
        if self.mode == EditorMode.NORMAL:
            return self._render_normal()
        else:
            return self._render_insert()
    
    def _render_normal(self) -> str:
        """Render NORMAL mode (view-only)."""
        models = self.inspection.models
        models_dict = models.to_public_dict()
        
        lines = []
        lines.append("┌─────────────────────────────────────────────┐")
        lines.append("│  【NORMAL】编辑模型配置                        │")
        lines.append("├─────────────────────────────────────────────┤")
        
        # Show all configured models
        for field in self.field_order:
            label = self.field_labels[field]
            value = models_dict.get(field) or "(未设置)"
            line = f"│  {label:<24} {value:<31} │"
            lines.append(line[:78])
        
        lines.append("├─────────────────────────────────────────────┤")
        lines.append("│  [i] 进入编辑模式  [Esc]返回列表               │")
        lines.append("└─────────────────────────────────────────────┘")
        
        return "\n".join(lines)
    
    def _render_insert(self) -> str:
        """Render INSERT mode (edit active field)."""
        field_label = self.field_labels[self.current_field]
        current_value = self.inspection.models.to_public_dict().get(
            self.current_field) or "(空)"
        
        lines = []
        lines.append("┌─────────────────────────────────────────────┐")
        lines.append(f"│  【INSERT】编辑 {field_label}                    │")
        lines.append("├─────────────────────────────────────────────┤")
        
        # Show current value with cursor position
        display_line = f"│  当前：{current_value:<40} │"
        lines.append(display_line[:78])
        
        # Show input area
        if self.edit_buffer:
            input_display = f"│  输入：{self.edit_buffer:<40} │"
        else:
            input_display = f"│  输入：_光标位置在这里                      │"
        lines.append(input_display[:78])
        
        # Show cursor position hint
        cursor_hint = f"│             ↑ 您在此位置 (←→ 定位)          │"
        lines.append(cursor_hint)
        
        lines.append("├─────────────────────────────────────────────┤")
        lines.append("│  [←→] 定位  [Ctrl+U]清空  [Enter]保存验证     │")
        lines.append("│  [Esc] 取消修改                              │")
        lines.append("└─────────────────────────────────────────────┘")
        
        return "\n".join(lines)
    
    def on_input(self, key: int) -> InputAction:
        """Handle keyboard input.
        
        In NORMAL mode:
        - i: Switch to INSERT mode
        - Esc/Q: Exit
        
        In INSERT mode:
        - Left/Right arrows: Position cursor
        - Ctrl+U: Clear buffer
        - Enter: Validate and save
        - Esc: Cancel
        """
        if self.mode == EditorMode.NORMAL:
            return self._normal_mode_input(key)
        else:
            return self._insert_mode_input(key)
    
    def _normal_mode_input(self, key: int) -> InputAction:
        """Process input when in NORMAL mode."""
        if key == ord('i'):
            self.mode = EditorMode.INSERT
            self.edit_buffer = ""
            self.cursor_pos = 0
            return InputAction.none()
        
        elif key in (27, ord('q')):  # Esc or q
            return InputAction.back()
        
        return InputAction.none()
    
    def _insert_mode_input(self, key: int) -> InputAction:
        """Process input when in INSERT mode."""
        # Escape - cancel
        if key == 27:
            self.mode = EditorMode.NORMAL
            self.edit_buffer = ""
            return InputAction.cancel()
        
        # Cursor movement
        elif key in (curses.KEY_LEFT, ord('h')):
            if self.edit_buffer:
                self.cursor_pos = max(0, self.cursor_pos - 1)
            return InputAction.none()
        
        elif key in (curses.KEY_RIGHT, ord('l')):
            if self.edit_buffer:
                self.cursor_pos = min(len(self.edit_buffer), self.cursor_pos + 1)
            return InputAction.none()
        
        # Clear buffer
        elif key == 21:  # Ctrl+U
            self.edit_buffer = ""
            self.cursor_pos = 0
            return InputAction.none()
        
        # Save/validate
        elif key == 10:  # Enter
            return self._validate_and_save()
        
        # Regular character input
        elif 32 <= key < 127:  # Printable ASCII
            char = chr(key)
            # Insert at cursor position
            self.edit_buffer = (
                self.edit_buffer[:self.cursor_pos] +
                char +
                self.edit_buffer[self.cursor_pos:]
            )
            self.cursor_pos += 1
            return InputAction.none()
        
        else:
            return InputAction.none()
    
    def _validate_and_save(self) -> InputAction:
        """Validate input and prepare save action."""
        if not self.edit_buffer.strip():
            # Empty = clear the field
            new_models = self._clear_field()
        else:
            # Validate as public identifier
            if self._is_valid_model_id(self.edit_buffer):
                new_models = self._set_field(self.edit_buffer)
            else:
                # Show validation error (next render will show red border)
                return InputAction.none(data={
                    "error": "invalid_model_id",
                    "message": "模型 ID 格式无效（不能包含路径或敏感词）"
                })
        
        return InputAction.save({
            "provider_id": self.provider.provider_id,
            "models": new_models.to_public_dict(),
        })
    
    def _is_valid_model_id(self, value: str) -> bool:
        """Check if value is a valid public identifier."""
        # Check for forbidden patterns (paths, sensitive keywords)
        import re
        if "://" in value:
            return False
        if re.search(r"(?i)(api|key|token|secret|password)", value):
            return False
        if value.startswith("/") or value.startswith("\\"):
            return False
        return True
    
    def _set_field(self, value: str) -> ModelMapping:
        """Set a specific model field."""
        mapping = self.inspection.models
        return ModelMapping(**{**mapping.to_public_dict(), self.current_field: value})
    
    def _clear_field(self) -> ModelMapping:
        """Clear a specific model field."""
        current = self.inspection.models.to_public_dict()
        current[self.current_field] = None
        return ModelMapping(**{k: v for k, v in current.items() if v is not None})
    
    def set_focus(self, focused: bool) -> None:
        """Set focus state."""
        pass  # No-op for now
    
    def is_focused(self) -> bool:
        """Check focus status."""
        return True
