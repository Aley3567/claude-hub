"""ASCII Logo animation controller."""

import time
from dataclasses import dataclass
from typing import List, Callable


@dataclass(frozen=True)
class AnimationFrame:
    """A single frame of animation."""
    content: str
    duration_ms: int = 40  # ~25fps base speed
    
    @property
    def fps(self) -> float:
        return 1000 / self.duration_ms


class LogoAnimator:
    """Logo animation with timing budget enforcement."""
    
    # ASCII logo frames (animated sequence)
    LOGO_FRAMES = [
        """
     __ _  ___  ___   ___  ____  
    / _` |/ _ \\/ __| / __|/ __| 
   | (_| |  __/\\__ \\| (__| (__  
    \\__,_|\\___||___/ \\___|\\___| 
                                 
""",
        """
     __ _  ___  ___   ___  ____  
    / _` |/ _ \\/ __| / __|/ __| 
   | (_| |  __/\\__ \\| (__| (_-< 
    \\__,_|\\___||___/ \\___|\\___/ 
                                 
""",
        """
     __ _  ___  ___   ___  ____  
    / _` |/ _ \\/ __| / __|/ __/ 
   | (_| |  __/\\__ \\| (__| (_|_
    \\__,_|\\___||___/ \\___|\\___/ 
                                 
""",
        """
     __ _  ___  ___   ___  ____  
    / _` |/ _ \\/ __| / __|/___/ 
   | (_| |  __/\\__ \\| (__ _____ 
    \\__,_|\\___||___/ \\___|_____|
                                 
""",
    ]
    
    # Final static frame
    STATIC_FRAME = """
██████╗ ███████╗███╗   ██╗████████╗ █████╗  ██████╗███████╗
██╔══██╗██╔════╝████╗  ██║╚══██╔══╝██╔══██╗██╔════╝██╔════╝
██████╔╝█████╗  ██╔██╗ ██║   ██║   ███████║██║     █████╗  
██╔═══╝ ██╔══╝  ██║╚██╗██║   ██║   ██╔══██║██║     ██╔══╝  
██║     ███████╗██║ ╚████║   ██║   ██║  ██║╚██████╗███████╗
╚═╝     ╚══════╝╚═╝  ╚═══╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝╚══════╝
                                                            
"""
    
    MAX_ANIMATION_DURATION_MS = 240  # Maximum total animation time
    
    def __init__(self, start_callback: Callable[[], None] | None = None,
                 end_callback: Callable[[], None] | None = None):
        """Initialize animator.
        
        Args:
            start_callback: Called when animation begins
            end_callback: Called when animation ends (static)
        """
        self.frames = self.LOGO_FRAMES + [self.STATIC_FRAME]
        self.frame_duration = 60  # ms per frame (~16fps for breathing effect)
        self.start_time: float | None = None
        self.current_frame = 0
        self.is_running = False
        self.start_callback = start_callback
        self.end_callback = end_callback
        self.user_interrupted = False
        
    def start(self) -> None:
        """Start the animation sequence."""
        if self.is_running:
            return
            
        self.start_time = time.monotonic()
        self.is_running = True
        self.current_frame = 0
        self.user_interrupted = False
        
        if self.start_callback:
            self.start_callback()
    
    def update(self) -> str:
        """Get current frame based on elapsed time.
        
        Returns:
            String representation of current frame
        """
        if not self.is_running:
            return self.STATIC_FRAME
        
        # Check for user interrupt (already set by input handler)
        if self.user_interrupted:
            return self.STATIC_FRAME
        
        # Calculate elapsed time
        elapsed_ms = (time.monotonic() - self.start_time) * 1000 if self.start_time else 0
        
        # Enforce max duration
        if elapsed_ms > self.MAX_ANIMATION_DURATION_MS:
            self.is_running = False
            if self.end_callback:
                self.end_callback()
            return self.STATIC_FRAME
        
        # Calculate frame index
        # First 3 frames quick flash, then slower breathing
        if elapsed_ms < 180:
            # Quick transition through first 3 frames
            frame_idx = min(int(elapsed_ms // (self.frame_duration * 2)), 3)
        else:
            # Slow breathing effect in static frame
            frame_idx = 4
        
        self.current_frame = frame_idx
        return self.frames[frame_idx]
    
    def should_stop(self) -> bool:
        """Check if animation should stop.
        
        Returns:
            True if animation is complete or interrupted
        """
        return not self.is_running or self.user_interrupted
    
    def interrupt(self) -> None:
        """User pressed a key - stop animation immediately."""
        self.user_interrupted = True
    
    def get_elapsed_percentage(self) -> float:
        """Return animation progress as percentage (0.0 to 1.0)."""
        if not self.start_time:
            return 0.0
        
        elapsed_ms = (time.monotonic() - self.start_time) * 1000
        return min(elapsed_ms / self.MAX_ANIMATION_DURATION_MS, 1.0)
