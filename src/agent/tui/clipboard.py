"""Clipboard watcher (spec 12.3): notices when a Solana address is copied, e.g. a coin's
mint in Padre, so the screen can analyze it without pasting.

Windows only for now (the user's OS); elsewhere the watcher reports itself unavailable.
It asks Windows for the clipboard's change counter, which is cheap and does not lock the
clipboard, and reads the text only after the counter changes. So it never holds the
clipboard while the browser is copying.

Privacy: the clipboard can hold anything, including keys a wallet app copied. Text that
is not exactly one address is dropped at once: never stored, logged or sent anywhere.
Text longer than any address is not even read.

The screen also writes to the clipboard in one case: when you click a coin in its list,
that coin's address is copied, ready to paste into Padre's search (spec 12.1). Nothing
else is ever written.
"""

from __future__ import annotations

import sys
import time
from typing import Protocol

from agent.ingest.solana import is_address

POLL_S = 0.1
MAX_CHARS = 64  # an address is at most 44 characters; with spaces or a line break, a bit more


def find_mint(text: str | None) -> str | None:
    """The address in `text` if the text is exactly one address (spaces around it are
    fine), else None."""
    if not text or len(text) > MAX_CHARS:
        return None
    candidate = text.strip()
    return candidate if is_address(candidate) else None


class ClipboardSource(Protocol):
    def sequence(self) -> int: ...

    def text(self) -> str | None: ...

    def copy(self, text: str) -> object: ...  # true if the text is on the clipboard now


class WindowsClipboard:
    """Windows' clipboard through its own API (user32/kernel32), no extra package."""

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalSize.restype = ctypes.c_size_t
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL
        self._user32, self._kernel32 = user32, kernel32

    def sequence(self) -> int:
        return int(self._user32.GetClipboardSequenceNumber())

    def text(self) -> str | None:
        user32, kernel32 = self._user32, self._kernel32
        if not user32.IsClipboardFormatAvailable(self.CF_UNICODETEXT):
            return None
        for _ in range(5):  # another program may have it open for a moment
            if user32.OpenClipboard(None):
                break
            time.sleep(0.01)
        else:
            return None
        try:
            handle = user32.GetClipboardData(self.CF_UNICODETEXT)
            if not handle:
                return None
            size = kernel32.GlobalSize(handle)  # bytes, 2 per character
            if size > 2 * (MAX_CHARS + 1):
                return None  # far longer than an address: not read at all
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return None
            try:
                return self._ctypes.wstring_at(pointer, size // 2).split("\0", 1)[0]
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def copy(self, text: str) -> bool:
        """Put `text` on the clipboard. False if Windows would not let us."""
        user32, kernel32 = self._user32, self._kernel32
        data = text.encode("utf-16-le") + b"\0\0"
        for _ in range(5):  # another program may have it open for a moment
            if user32.OpenClipboard(None):
                break
            time.sleep(0.01)
        else:
            return False
        try:
            handle = kernel32.GlobalAlloc(self.GMEM_MOVEABLE, len(data))
            if not handle:
                return False
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                kernel32.GlobalFree(handle)
                return False
            self._ctypes.memmove(pointer, data, len(data))
            kernel32.GlobalUnlock(handle)
            user32.EmptyClipboard()
            if not user32.SetClipboardData(self.CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)  # ours again: Windows did not take it
                return False
            return True
        finally:
            user32.CloseClipboard()


def system_clipboard() -> ClipboardSource | None:
    """This PC's clipboard, or None where the watcher is not supported yet."""
    if sys.platform != "win32":
        return None
    try:
        return WindowsClipboard()
    except (OSError, AttributeError):
        return None


class ClipboardWatcher:
    """Call `poll()` every POLL_S. It returns a newly copied address once, else None.
    The same address copied again is ignored, as is whatever was on the clipboard when
    the watcher started or was switched back on."""

    def __init__(self, source: ClipboardSource | None) -> None:
        self.source = source
        self.enabled = source is not None
        self._last_sequence = source.sequence() if source is not None else 0
        self._last_mint: str | None = None
        self.changed_at: float | None = None  # when the last address was noticed

    @property
    def available(self) -> bool:
        return self.source is not None

    def toggle(self) -> bool:
        if self.source is None:
            return False
        self.enabled = not self.enabled
        if self.enabled:
            self._last_sequence = self.source.sequence()
        return self.enabled

    def poll(self) -> str | None:
        if not self.enabled or self.source is None:
            return None
        sequence = self.source.sequence()
        if sequence == self._last_sequence:
            return None
        self._last_sequence = sequence
        mint = find_mint(self.source.text())
        if mint is None or mint == self._last_mint:
            return None
        self._last_mint = mint
        self.changed_at = time.perf_counter()
        return mint

    def copy(self, mint: str) -> bool:
        """Put a coin's address on the clipboard (the user clicked that coin). The
        watcher does not report its own copy. Only an address is ever written."""
        if self.source is None or not is_address(mint):
            return False
        try:
            done = bool(self.source.copy(mint))
        except OSError:
            done = False
        self._last_sequence = self.source.sequence()
        if done:
            self._last_mint = mint
        return done
