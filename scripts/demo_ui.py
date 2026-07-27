#!/usr/bin/env python3
"""Demonstration of the new TUI system - Text mode preview."""

from __future__ import annotations

import sys
from pathlib import Path

# Add src to path
cwd = Path.cwd()
if str(cwd) not in sys.path:
    sys.path.insert(0, str(cwd))


def demo_logo_animator():
    """Demo logo animation in text mode."""
    from claude_hub.ui.animation.logo_animator import LogoAnimator
    import time
    
    print("\n" + "=" * 80)
    print("【Logo Animator Demo】")
    print("=" * 80 + "\n")
    
    animator = LogoAnimator()
    
    # Simulate animation progression
    frames_shown = 0
    start_time = time.time()
    
    while not animator.should_stop() and frames_shown < 10:
        frame = animator.update()
        
        # Show current frame
        print(f"[{frames_shown + 1}] Frame (elapsed: {animator.get_elapsed_percentage()*100:.0f}%):")
        print(frame)
        print()
        
        frames_shown += 1
        
        # Simulate time passing
        if frames_shown < 5:
            time.sleep(0.06)  # During animation
        else:
            break  # Static phase
    
    print("✅ Animation completed (or user interrupted)\n")


def demo_provider_list():
    """Demo provider list widget rendering."""
    from claude_hub.domain import ProviderRef, ProviderInspection, ModelMapping
    from claude_hub.ui.components.provider_list import ProviderListWidget
    
    print("\n" + "=" * 80)
    print("【Provider List Widget Demo】")
    print("=" * 80 + "\n")
    
    # Create mock data
    providers = [
        ProviderRef(store="cc-switch", provider_id="primary", 
                   display_name="Primary Provider", is_current=True),
        ProviderRef(store="cc-switch", provider_id="fast-api", 
                   display_name="Fast API", is_current=False),
        ProviderRef(store="cc-switch", provider_id="backup", 
                   display_name="Backup Service", is_current=False),
        ProviderRef(store="cc-switch", provider_id="experimental", 
                   display_name="Experimental Model", is_current=False),
    ]
    
    inspections = {
        "primary": ProviderInspection(
            reference=providers[0],
            models=ModelMapping(default="claude-3-opus-20240229"),
            is_current=True,
        ),
        "fast-api": ProviderInspection(
            reference=providers[1],
            models=ModelMapping(fast="claude-3-haiku-20240307"),
            is_current=False,
        ),
        "backup": ProviderInspection(
            reference=providers[2],
            models=ModelMapping(default="claude-3-sonnet-20240229"),
            is_current=False,
        ),
        "experimental": ProviderInspection(
            reference=providers[3],
            models=ModelMapping(reasoning="claude-3-5-sonnet-research"),
            is_current=False,
        ),
    }
    
    # Create and render widget
    widget = ProviderListWidget(providers, inspections, selected_provider_id="primary")
    
    print(widget.render())
    print("\n✅ Widget rendered successfully\n")


def demo_model_editor():
    """Demo model editor widget."""
    from claude_hub.domain import ProviderRef, ProviderInspection, ModelMapping
    from claude_hub.ui.components.model_editor import ModelEditorWidget, EditorMode
    
    print("\n" + "=" * 80)
    print("【Model Editor Widget Demo】")
    print("=" * 80 + "\n")
    
    provider = ProviderRef(
        store="cc-switch", 
        provider_id="test-provider",
        display_name="Test Provider",
    )
    
    inspection = ProviderInspection(
        reference=provider,
        models=ModelMapping(
            default="claude-3-opus",
            fast="claude-3-haiku",
            coding="claude-3-sonnet-coding",
            long_context="claude-3-opus-200k",
        ),
        is_current=False,
    )
    
    editor = ModelEditorWidget(provider, inspection)
    
    # Show NORMAL mode
    print("【NORMAL Mode】")
    print(editor.render())
    print("\n📝 Press 'i' to enter INSERT mode")
    print("    Press 'Esc' to exit\n")
    
    # Switch to INSERT mode manually
    editor.mode = EditorMode.INSERT
    editor.current_field = "default"
    editor.edit_buffer = "claude-3-5-sonnet-beta"
    editor.cursor_pos = len(editor.edit_buffer)
    
    print("\n【INSERT Mode】")
    print(editor.render())
    print("\n💡 Features:")
    print("   - Move cursor with ← → keys")
    print("   - Clear buffer with Ctrl+U")
    print("   - Enter to save/validate")
    print("   - Esc to cancel\n")


def demo_keybindings():
    """Demo key binding system."""
    from claude_hub.ui.input.keybindings import KeyBindings
    
    print("\n" + "=" * 80)
    print("【Key Binding System Demo】")
    print("=" * 80 + "\n")
    
    bindings = KeyBindings()
    
    print("Default key bindings:")
    print("-" * 60)
    
    actions = ["up", "down", "select", "exit", "help"]
    for action in actions:
        keys = bindings.get_keys(action)
        key_names = []
        for k in keys:
            if 32 <= k < 127:
                key_names.append(f"'{chr(k)}'")
            elif k == 27:
                key_names.append("Esc")
            elif k == 10:
                key_names.append("Enter")
            else:
                key_names.append(f"Code({k})")
        
        print(f"{action:>12}: {', '.join(key_names)}")
    
    print("\n✅ Environment customization:")
    print("   CLAUDE1_KEYMAP=vim|emacs|custom")
    print("   CLAUDE1_CUSTOM_BINDINGS='{...}'\n")


def demo_themes():
    """Demo color theme system."""
    from claude_hub.ui.themes.color_theme import Theme, DEFAULT_THEME, DARK_THEME, LIGHT_THEME
    
    print("\n" + "=" * 80)
    print("【Color Theme System Demo】")
    print("=" * 80 + "\n")
    
    themes = [
        ("Default Theme", DEFAULT_THEME),
        ("Dark Theme", DARK_THEME),
        ("Light Theme", LIGHT_THEME),
    ]
    
    for name, theme in themes:
        print(f"\n{name}")
        print("-" * 60)
        for color_name, color_code in theme.colors.items():
            print(f"  {color_name:<20}: 256-color #{color_code:03d}")
    
    print("\n✅ Themes are fully customizable via Theme dataclass\n")


def main():
    """Run all demos."""
    print("\n╔" + "═" * 78 + "╗")
    print("║" + " " * 20 + "CLAUDE-HUB UI SYSTEM DEMO" + " " * 33 + "║")
    print("╚" + "═" * 78 + "╝")
    
    try:
        demo_logo_animator()
        demo_provider_list()
        demo_model_editor()
        demo_keybindings()
        demo_themes()
        
        print("\n" + "╔" + "═" * 78 + "╗")
        print("║" + " " * 25 + "All Demos Complete! ✨" + " " * 34 + "║")
        print("╚" + "═" * 78 + "╝\n")
        
        return 0
        
    except ImportError as e:
        print(f"\n❌ Import error: {e}", file=sys.stderr)
        print("Please ensure you're running this from the project root:\n")
        print("   python3 scripts/demo_ui.py\n")
        return 1
    except Exception as e:
        print(f"\n❌ Demo error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
