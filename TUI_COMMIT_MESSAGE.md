feat: Implement modular TUI system with component architecture

## 🎨 UI System Overview

Created a complete, modular TUI system that was previously missing from the codebase. The system features:

### Core Components Implemented

1. **Logo Animation Controller** (`ui/animation/logo_animator.py`)
   - ASCII art animation with timing budget enforcement (240ms max)
   - User interrupt support via key presses
   - Graceful transition from animated to static state
   - Progress tracking and FPS control

2. **Provider Selection Widget** (`ui/components/provider_list.py`)
   - Scrollable list with j/k navigation (vim-style)
   - Visual selection marker (▸ for current)
   - Model info display per provider
   - Empty state handling
   - Footer with shortcuts help

3. **Model Editor Widget** (`ui/components/model_editor.py`)
   - vim-like NORMAL/INSERT mode switching
   - Field-specific editing with cursor positioning
   - Real-time validation of model identifiers
   - Backup creation before saving
   - Clear input (Ctrl+U) and cancel (Esc) operations

4. **Key Binding System** (`ui/input/keybindings.py`)
   - Configurable keyboard mappings (vim/emacs/custom)
   - Environment variable customization (CLAUDE1_KEYMAP)
   - Non-blocking terminal input reading
   - Thread-safe input queue

5. **Color Theme System** (`ui/themes/color_theme.py`)
   - 256-color palette configuration
   - Predefined themes (Default/Dark/Light)
   - Semantic color roles (success/warning/error/info)
   - Claude.ai-inspired orange theme by default

### Architecture Highlights

- **Pure stdlib**: Zero third-party dependencies (curses only for rendering)
- **Protocol-based**: Widget protocol enables interchangeable components
- **Lazy loading**: Prevents circular import issues
- **Type-safe**: Full type annotations throughout
- **Testable**: Each component can be tested independently

### File Structure

```
src/claude_hub/ui/
├── __init__.py                 # Module exports with lazy loading
├── app.py                      # Main application orchestrator
├── animation/                  # Animation controllers
│   └── logo_animator.py
├── components/                 # UI widgets
│   ├── __init__.py
│   ├── provider_list.py       # Provider selection menu
│   └── model_editor.py        # Model configuration editor
├── input/                      # Input handling
│   ├── __init__.py
│   └── keybindings.py         # Keyboard mapping system
├── layout/                     # Layout primitives
│   └── widget_protocol.py     # Widget protocol definition
└── themes/                     # Color themes
    ├── __init__.py
    └── color_theme.py         # Theme configuration
```

## ✅ Testing & Demo

- Created `scripts/showcase.py` demonstrating all working components
- All widgets render correctly in text mode preview
- Logo animation progresses through frames with proper timing
- Provider list displays selection markers and model info
- Model editor shows both NORMAL and INSERT mode previews
- Key bindings export properly with environment customization hints

## 🚀 Next Steps

1. Integrate with main launcher flow
2. Add curses rendering wrapper for interactive mode  
3. Write unit tests for each component
4. Deploy to production with existing launcher scripts
