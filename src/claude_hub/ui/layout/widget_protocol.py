"""Widget protocol for UI components."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class InputAction:
    """Represents a user action from keyboard input."""
    
    type: str
    data: dict | None = None
    
    @classmethod
    def select(cls, provider_id: str) -> "InputAction":
        return cls("SELECT", {"provider_id": provider_id})
    
    @classmethod
    def navigate_up(cls) -> "InputAction":
        return cls("NAV_UP")
    
    @classmethod
    def navigate_down(cls) -> "InputAction":
        return cls("NAV_DOWN")
    
    @classmethod
    def exit(cls) -> "InputAction":
        return cls("EXIT")
    
    @classmethod
    def back(cls) -> "InputAction":
        return cls("BACK")
    
    @classmethod
    def save(cls, data: dict) -> "InputAction":
        return cls("SAVE", data)
    
    @classmethod
    def cancel(cls) -> "InputAction":
        return cls("CANCEL")
    
    @classmethod
    def none(cls) -> "InputAction":
        return cls("NONE")


@runtime_checkable
class Widget(Protocol):
    """All UI widgets must implement this protocol."""
    
    def render(self) -> str:
        """Render widget as ASCII art string."""
        ...
    
    def on_input(self, key: int) -> InputAction:
        """Handle keyboard input, return action or NONE."""
        ...
    
    def is_focused(self) -> bool:
        """Whether this widget currently has focus."""
        ...
    
    def set_focus(self, focused: bool) -> None:
        """Set widget focus state."""
        ...
