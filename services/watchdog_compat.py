"""
Corecturile locale peste watchdog, fixate pe versiunea din requirements.txt.
=============================================================================

De ce există
------------
Pe Windows, watchdog 7.1.0 citește directorul pe un fir separat
(winapi.DirectoryChangeReader), blocat în ReadDirectoryChangesExW până apare o
schimbare. Oprirea îl trezește cu CancelIoEx și apoi îl așteaptă cu join() fără
termen. Firul își verifică însă steagul de oprire ÎNAINTE de citire: dacă
anularea sosește între verificare și intrarea în citire, nu găsește nicio
operație de anulat, iar firul intră în citire și rămâne acolo până la
următoarea schimbare din director. Oprirea așteaptă după el la nesfârșit.

Măsurat pe 19 noiembrie 2026: pe gazdă, o buclă de porniri și opriri ale unui
singur cititor, într-un director în care nu se schimbă nimic, s-a blocat după
aproximativ 1500 de cicluri; pe clona Endpoint01, testele de monitorizare s-au
blocat o dată din 25 de rulări. Cu corectura de aici, 10.000 de cicluri au
trecut, iar unul singur a avut nevoie de a doua anulare.

Consecința pentru agent ar fi o oprire care nu se mai termină: observer.stop()
oprește fiecare emitter, iar emitter-ul își oprește cititorul pe același fir.

Ce face corectura
-----------------
Aceeași oprire, cu o singură diferență: anularea se repetă, sub lacătul
cititorului, până când firul chiar s-a terminat. Lacătul e cel sub care firul
își golește handle-ul înainte să-l închidă, deci anularea nu atinge niciodată un
handle închis.

De ce e fixată pe versiune
--------------------------
Corectura înlocuiește o metodă internă. O altă versiune de watchdog poate avea
alt cititor, deci se aplică numai versiunii verificate, iar
tests/test_watchdog_compat.py pică la orice schimbare de versiune, ca să fie
revăzută odată cu ea.
"""

import os

from watchdog.version import VERSION_STRING


PATCHED_WATCHDOG_VERSION = "7.1.0"

# Cât așteaptă oprirea între două anulări. Mic față de bugetul de oprire al
# agentului (secunde); de cele mai multe ori prima anulare ajunge.
_CANCEL_RETRY_SECONDS = 0.05

_applied = False


def _stop_until_the_reader_exits(self) -> None:
    """Înlocuitorul lui winapi.DirectoryChangeReader.stop()."""
    from watchdog.observers import winapi

    with self._lock:
        if self._should_stop:
            return  # deja în curs de oprire, ca în original
        self._should_stop = True
        reader_thread = self._reader_thread
        if reader_thread is None:
            return  # n-a pornit niciodată
        self._reader_thread = None

    while True:
        with self._lock:
            if self._handle is not None:
                winapi._cancel_handle_io(self._handle)
        reader_thread.join(_CANCEL_RETRY_SECONDS)
        if not reader_thread.is_alive():
            return


def apply() -> bool:
    """
    Aplică corectura o singură dată. Întoarce True dacă e activă.

    Pe alt sistem decât Windows cititorul nu există, deci nu e nimic de
    corectat. Pe altă versiune de watchdog nu se aplică nimic: testul de
    versiune pică și cere revizuirea.
    """
    global _applied

    if os.name != "nt" or VERSION_STRING != PATCHED_WATCHDOG_VERSION:
        return False

    if not _applied:
        from watchdog.observers import winapi

        winapi.DirectoryChangeReader.stop = _stop_until_the_reader_exits
        _applied = True

    return True
