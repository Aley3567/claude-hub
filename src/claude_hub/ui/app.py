"""Main TUI application coordinator."""

from __future__ import annotations

import os
import sys
import curses
import threading
import time
import queue
from typing import Callable, Optional

from claude_hub.domain import ProviderRef, ProviderInspection
from claude_hub.service import ProviderApplicationService
from claude_hub.routing import StartupRoute, resolve_tui_startup
from .components.provider_list import ProviderListWidget
from .components.model_editor import ModelEditorWidget
from .animation.logo_animator import LogoAnimator
from .input.keybindings import KeyBindings, setup_curses_defaults, InputReader


class ClaudeHubTUI:
    """Main TUI application orchestrator."""
    
    def __init__(self, service: ProviderApplicationService, 
                 stdscr=None):
        """Initialize TUI application.
        
        Args:
            service: Provider application service for data access
            stdscr: Curses standard screen (for interactive mode)
        """
        self.service = service
        self.stdscr = stdscr
        self.route: Optional[StartupRoute] = None
        
        # Widgets
        self.provider_list: Optional[ProviderListWidget] = None
        self.model_editor: Optional[ModelEditorWidget] = None
        
        # Animation state
        self.animator = LogoAnimator(
            start_callback=self._on_animation_start,
            end_callback=self._on_animation_end,
        )
        
        # Input handling
        self.input_reader = InputReader()
        self.binding = KeyBindings.from_environment()
        
        # State
        self.current_state = "logo"  # logo | selecting | editing | goodbye
        self.running = True
        self.selected_provider: Optional[str] = None
        
        # Threading
        self.input_thread: Optional[threading.Thread] = None
    
    def run(self) -> int:
        """Run the TUI application.
        
        Returns:
            Exit code (0 = success, 1 = error)
        """
        if not self.stdscr:
            return self._run_headless()
        
        try:
            curses.wrapper(self._curses_main)
            return 0
        except KeyboardInterrupt:
            return 130
        except Exception as e:
            self._print_error(str(e))
            return 1
    
    def _curses_main(self, stdscr) -> None:
        """Main curses entry point (called by curses.wrapper)."""
        self.stdscr = stdscr
        setup_curses_defaults(stdscr)
        
        # Determine initial state
        capability = self.service.detect()
        self.route = resolve_tui_startup(
            self.service,
            standalone_exists=False,
            store_override=None,
        )
        
        # Start input reader thread
        self._start_input_reader()
        
        try:
            while self.running:
                self._render_frame()
                self._process_events()
                
                if self.animator.should_stop():
                    self.current_state = "selecting"
                    break
                
                time.sleep(0.06)  # ~16fps during animation
                
        finally:
            self.stop_input_reader()
    
    def _render_frame(self) -> None:
        """Render current frame to terminal."""
        self.stdscr.clear()
        
        # Get terminal dimensions
        height, width = self.stdscr.getmaxyx()
        
        if self.current_state == "logo":
            content = self.animator.update()
            lines = content.strip().split('\n')
            
            # Center logo
            start_y = max(0, (height - len(lines)) // 2)
            start_x = max(0, (width - max(len(line) for line in lines)) // 2)
            
            for i, line in enumerate(lines):
                try:
                    self.stdscr.addstr(start_y + i, start_x, line[:width-1])
                except curses.error:
                    pass
        
        elif self.current_state == "selecting" and self.provider_list:
            content = self.provider_list.render()
            lines = content.split('\n')
            
            # Center widget
            start_y = max(0, (height - len(lines)) // 2)
            start_x = max(0, (width - max(len(line) for line in lines)) // 2)
            
            for i, line in enumerate(lines):
                try:
                    self.stdscr.addstr(start_y + i, start_x, line[:width-1])
                except curses.error:
                    pass
        
        self.stdscr.refresh()
    
    def _process_events(self) -> None:
        """Process pending events and user input."""
        # Check for key presses
        while True:
            key = self.input_reader.pop_key()
            if key is None:
                break
            
            action = self._handle_input(key)
            if action.type == "EXIT":
                self.running = False
                break
    
    def _start_input_reader(self) -> None:
        """Start background thread to read keyboard input."""
        def reader_loop():
            while self.running:
                key = self.stdscr.getch()
                if key != -1:
                    self.input_reader.push_key(key)
                    
                    # Interrupt animation
                    if self.current_state == "logo":
                        self.animator.interrupt()
                time.sleep(0.01)
        
        self.input_thread = threading.Thread(target=reader_loop, daemon=True)
        self.input_thread.start()
    
    def stop_input_reader(self) -> None:
        """Stop input reader thread."""
        if self.input_thread:
            self.running = False
            try:
                self.input_thread.join(timeout=0.5)
            except RuntimeError:
                pass
    
    def _handle_input(self, key: int) -> tuple:
        """Handle keyboard input based on current state."""
        if self.current_state == "selecting" and self.provider_list:
            return self.provider_list.on_input(key)
        elif self.current_state == "editing" and self.model_editor:
            return self.model_editor.on_input(key)
        else:
            return self._default_handler(key)
    
    def _default_handler(self, key: int) -> tuple:
        """Default key handler."""
        if key in (ord('q'), 27):  # Esc or q
            return self._action_exit()
        return self._action_none()
    
    def prepare_selection_screen(self) -> None:
        """Prepare provider list for display."""
        providers = self.service.list_providers()
        inspections = {}
        
        for ref in providers:
            try:
                inspection = self.service.inspect(ref)
                inspections[ref.provider_id] = inspection
            except Exception:
                pass
        
        # Find currently selected (current marker or MRU)
        selected_id = next((p.provider_id for p in providers if p.is_current), None)
        
        # Enable single-channel mode when only one provider is available
        single_mode = len(providers) == 1
        
        self.provider_list = ProviderListWidget(
            providers, inspections, selected_id, single_mode=single_mode
        )
        self.current_state = "selecting"
    
    def show_model_editor(self, provider_id: str) -> None:
        """Show model editor for a provider."""
        try:
            inspection = self.service.inspect_provider(provider_id)
            ref = next(r for r in self.service.list() if r.provider_id == provider_id)
            
            self.model_editor = ModelEditorWidget(ref, inspection)
            self.current_state = "editing"
        except Exception as e:
            self._print_error(f"无法加载编辑器：{e}")
    
    def _on_animation_start(self) -> None:
        """Called when animation begins."""
        pass
    
    def _on_animation_end(self) -> None:
        """Called when animation completes."""
        pass
    
    def _action_exit(self) -> tuple:
        """Handle exit action."""
        self.current_state = "goodbye"
        return ("EXIT", None)
    
    def _action_none(self) -> tuple:
        """Handle no-action input."""
        return ("NONE", None)
    
    def _print_error(self, message: str) -> None:
        """Print error message to stderr."""
        print(f"[错误] {message}", file=sys.stderr)
    
    def _run_headless(self) -> int:
        """Run without curses (fallback mode)."""
        self._print_error("不支持无终端模式运行")
        return 1
    
    @classmethod
    def from_args(cls, argv: list[str] | None = None) -> "ClaudeHubTUI":
        """Create TUI instance from command line args.
        
        Args:
            argv: Command line arguments (defaults to sys.argv[1:])
            
        Returns:
            Configured TUI instance
        """
        import argparse
        parser = argparse.ArgumentParser(description="Claude Hub TUI")
        parser.parse_args(argv)
        
        service = ProviderApplicationService(None)  # Will be configured later
        return cls(service)
