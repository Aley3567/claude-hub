#!/usr/bin/env python3
"""Quick UI component showcase."""

import sys
from pathlib import Path

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent))


def main():
    print("\n" + "=" * 80)
    print("       CLAUDE-HUB TUI SYSTEM - COMPONENT SHOWCASE")
    print("=" * 80 + "\n")
    
    # Demo 1: Logo Animator
    print("【1. Logo Animation Controller】")
    print("-" * 80)
    from claude_hub.ui.animation.logo_animator import LogoAnimator
    
    animator = LogoAnimator()
    frame = animator.update()
    lines = frame.strip().split('\n')
    
    for i, line in enumerate(lines[:7], 1):
        if i <= 5:
            print(f"{line}")
        else:
            print(f"... (and {len(lines)-5} more lines)")
    
    print(f"\n✅ Features:")
    print(f"   • Max duration: {animator.MAX_ANIMATION_DURATION_MS}ms")
    print(f"   • Current progress: {animator.get_elapsed_percentage()*100:.0f}%")
    print(f"   • Should stop: {animator.should_stop()}\n")
    
    # Demo 2: Provider List Widget
    print("【2. Provider List Widget】")
    print("-" * 80)
    from claude_hub.domain import ProviderRef, ProviderInspection, ModelMapping
    from claude_hub.ui.components.provider_list import ProviderListWidget
    
    providers = [
        ProviderRef(store="cc-switch", provider_id="primary", 
                   display_name="Primary Claude", is_current=True),
        ProviderRef(store="cc-switch", provider_id="mimo", 
                   display_name="Mimo API", is_current=False),
        ProviderRef(store="cc-switch", provider_id="backup", 
                   display_name="Backup Provider", is_current=False),
    ]
    
    inspections = {
        "primary": ProviderInspection(
            reference=providers[0],
            models=ModelMapping(default="claude-3-opus"),
            is_current=True,
        ),
        "mimo": ProviderInspection(
            reference=providers[1],
            models=ModelMapping(fast="claude-3-haiku"),
            is_current=False,
        ),
        "backup": ProviderInspection(
            reference=providers[2],
            models=ModelMapping(default="claude-3-sonnet"),
            is_current=False,
        ),
    }
    
    widget = ProviderListWidget(providers, inspections, selected_provider_id="primary")
    output = widget.render()
    print(output)
    print("\n✅ Features:")
    print(f"   • Display rows: {widget.visible_count}")
    print(f"   • Selected index: {widget.selected_index}")
    print(f"   • Get selected: {widget.get_selected_provider().provider_id if widget.get_selected_provider() else None}\n")
    
    # Demo 3: Model Editor
    print("【3. Model Editor Widget】")
    print("-" * 80)
    from claude_hub.ui.components.model_editor import ModelEditorWidget, EditorMode
    
    provider = ProviderRef(store="cc-switch", provider_id="test", 
                          display_name="Test Provider")
    inspection = ProviderInspection(
        reference=provider,
        models=ModelMapping(default="claude-3-opus", fast="claude-3-haiku"),
        is_current=False,
    )
    
    editor = ModelEditorWidget(provider, inspection)
    
    print("NORMAL Mode Preview:")
    normal_view = editor.render()
    for line in normal_view.split('\n')[:8]:
        print(line)
    
    print("\n💡 Press 'i' → Enter INSERT mode\n")
    
    # Demo 4: Key Bindings
    print("【4. Key Binding System】")
    print("-" * 80)
    from claude_hub.ui.input.keybindings import KeyBindings
    
    kb = KeyBindings()
    print("Default bindings:")
    actions = [("up", "Navigate up"), ("down", "Navigate down"), 
              ("select", "Select item"), ("exit", "Exit")]
    for action, desc in actions:
        keys = kb.get_keys(action)
        key_strs = []
        for k in keys:
            if k == 27:
                key_strs.append("Esc")
            elif k == 10:
                key_strs.append("Enter")
            elif 32 <= k < 127:
                key_strs.append(f"'{chr(k)}'")
            else:
                key_strs.append(str(k))
        print(f"   {action:>10}: {', '.join(key_strs)}  # {desc}")
    
    print("\n🎮 Environment customizations:")
    print("   CLAUDE1_KEYMAP=vim|emacs|custom")
    print("   CLAUDE1_CUSTOM_BINDINGS='{...}'\n")
    
    # Demo 5: Theme System
    print("【5. Color Theme System】")
    print("-" * 80)
    from claude_hub.ui.themes.color_theme import DEFAULT_THEME, DARK_THEME
    
    themes = [("Default", DEFAULT_THEME), ("Dark", DARK_THEME)]
    for name, theme in themes:
        print(f"\n{name} Theme:")
        colors = list(theme.colors.items())[:4]  # Show first 4 colors
        for color_name, code in colors:
            print(f"   {color_name:<15}: #{code:03d}")
    
    print("\n✅ All themes support 256-color palette\n")
    
    print("╔" + "═" * 78 + "╗")
    print("║" + " " * 20 + "CLAUDE-HUB UI SYSTEM DEMO COMPLETE ✨" + " " * 25 + "║")
    print("╚" + "═" * 78 + "╝\n")
    
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n❌ Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
