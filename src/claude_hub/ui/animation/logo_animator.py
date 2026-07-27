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
        
        # Performance optimization: Pre-compute frame timing offsets
        # to avoid runtime calculations
        self._frame_offsets = []
        total_offset = 0
        for i in range(len(self.LOGO_FRAMES)):
            self._frame_offsets.append(total_offset)
            total_offset += int(self.frame_duration * 2)
        
        self._last_update_time = 0.0  # Optimize state tracking
        
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
        # Early exit: static phase or already stopped (ZERO CPU overhead)
        if not self.is_running or self.user_interrupted:
            return self.STATIC_FRAME
        
        # Performance optimization: Use monotonic clock with caching
        current_time = time.monotonic()
        
        # Check if enough time has passed to update frame
        elapsed_since_last = (current_time - self._last_update_time) * 1000
        if elapsed_since_last < self.frame_duration:
            return self.frames[self.current_frame]
        
        self._last_update_time = current_time
        
        # Calculate frame using pre-computed offsets (no real-time math)
        if self.start_time is None:
            self.start_time = current_time
            self.current_frame = 0
        else:
            elapsed_ms = (current_time - self.start_time) * 1000
            
            # Enforce max duration - exit immediately
            if elapsed_ms > self.MAX_ANIMATION_DURATION_MS:
                self.is_running = False
                self.current_frame = len(self.frames) - 1
                if self.end_callback:
                    self.end_callback()
                return self.STATIC_FRAME
            
            # Fast frame lookup using pre-computed offsets
            # Binary search would be overkill for small arrays, linear scan is faster
            for i, offset in enumerate(self._frame_offsets):
                if elapsed_ms < offset + int(self.frame_duration * 2):
                    self.current_frame = i
                    break
        return self.frames[self.current_frame]
    
    def should_stop(self) -> bool:
        """Check if animation should stop.
        
        Returns:
            True if animation is complete or interrupted
        """
        return not self.is_running or self.user_interrupted
    
    def get_elapsed_percentage(self) -> float:
        """Return animation progress as percentage (0.0 to 1.0)."""
        if not self.start_time:
            return 0.0
        
        current_time = time.monotonic()
        elapsed_ms = (current_time - self.start_time) * 1000
        return min(elapsed_ms / self.MAX_ANIMATION_DURATION_MS, 1.0)
    
    def get_last_update_time(self) -> float:
        """Get last frame update time for performance monitoring."""
        return self._last_update_time
    
    def interrupt(self) -> None:
        """User pressed a key - stop animation immediately."""
        self.user_interrupted = True
