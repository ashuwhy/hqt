import ctypes
import time
from typing import Any

class RingBuffer:
    def __init__(self, size: int = 1048576):
        # Ensure size is a power of 2
        assert (size & (size - 1)) == 0, "Size must be a power of 2"
        self.size = size
        self.mask = size - 1
        
        # We use a pre-allocated list as the underlying contiguous array for objects
        self.buffer: list[Any] = [None] * size
        
        # Lock-free sequence counters using ctypes
        self.published_seq = ctypes.c_longlong(-1)
        self.consumed_matching_seq = ctypes.c_longlong(-1)
        self.consumed_persist_seq = ctypes.c_longlong(-1)

    def publish(self, item: Any) -> None:
        """Called by InboundThread to publish an item."""
        seq = self.published_seq.value + 1
        
        # Spin if the slow consumer (persistence) is too far behind
        while seq - self.consumed_persist_seq.value >= self.size:
            time.sleep(0) # yield to other threads
            
        self.buffer[seq & self.mask] = item
        
        # Memory barrier publish
        self.published_seq.value = seq

    def __getitem__(self, seq: int) -> Any:
        return self.buffer[seq & self.mask]

    def get_published_seq(self) -> int:
        return self.published_seq.value
        
    def get_matching_seq(self) -> int:
        return self.consumed_matching_seq.value
        
    def set_matching_seq(self, seq: int) -> None:
        self.consumed_matching_seq.value = seq
        
    def get_persist_seq(self) -> int:
        return self.consumed_persist_seq.value
        
    def set_persist_seq(self, seq: int) -> None:
        self.consumed_persist_seq.value = seq
