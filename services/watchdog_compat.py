"""
Corecturile locale peste watchdog, fixate pe versiunea din requirements.txt.
=============================================================================

Sunt două: oprirea cititorului de director și lista de schimbări pe care o
cere. Amândouă privesc numai Windows.

Prima corectură: oprirea cititorului
------------------------------------
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

A doua corectură: cititorul nu mai cere schimbările datei de acces
-----------------------------------------------------------------
watchdog cere sistemului opt tipuri de schimbări (winapi.
WATCHDOG_FILE_NOTIFY_FLAGS), printre ele FILE_NOTIFY_CHANGE_LAST_ACCESS. O
citire nu schimbă conținutul, dar poate schimba data ultimului acces, iar
Windows o anunță ca FILE_ACTION_MODIFIED, ca pe o scriere. watchdog 7 o
clasifică drept atribute (data schimbării, mutată de redenumire, e mai nouă
decât data scrierii), iar agentul tratează atributele ca modificare
(file_monitor.py::EDRFileEventHandler.on_attrib). Rezultatul: agentul își
vedea propria citire. Amprentarea citea fișierul, data accesului se schimba, iar
fișierul revenea ca file_modified, cu aceeași amprentă.

Măsurat pe 23 noiembrie 2026, pe clona Endpoint01, în rularea de măsurătoare
2.5: fiecare din cele 400 de fișiere eliberate a dat file_created urmat de
file_modified, iar ordinea dublurilor era exact ordinea amprentării, fără
nicio inversiune. Apoi, cu agentul oprit, câte un cititor pe fiecare tip de
schimbare: un fișier citit a dat FILE_ACTION_MODIFIED pe LAST_ACCESS imediat
la citire, numai dacă data lui de acces era mai veche de o oră. Pentru
fișierele proaspete Windows nu actualizează data, de aceea probele cu fișiere
generate pe loc n-au arătat nimic. fsutil arăta DisableLastAccess = 2, adică
actualizarea ar fi trebuit să fie oprită, iar data tot s-a actualizat. Setarea
nu e deci o apărare.

Tipul acesta se scoate din listă și doar el. O schimbare de conținut schimbă
data scrierii sau mărimea, iar acelea rămân cerute (LAST_WRITE, SIZE), deci nu
se pierde nicio scriere. Rămân cerute și numele, atributele, securitatea și
data creării: corectura scoate exact sursa auto-observării, nu și alte
schimbări de metadate, pe care agentul le observa și înainte.

De ce e fixată pe versiune
--------------------------
Prima corectură înlocuiește o metodă internă, a doua o constantă a modulului
winapi, citită la fiecare apel de ReadDirectoryChangesExW. O altă versiune de
watchdog poate avea alt cititor sau altă listă, deci se aplică numai versiunii
verificate, iar tests/test_watchdog_compat.py pică la orice schimbare de
versiune, ca să fie revăzută odată cu ea.
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


def content_notify_flags() -> int:
    """
    Tipurile de schimbări pe care le cere agentul: cele din watchdog, fără data
    ultimului acces. Calculate din lista originală, nu scrise de mână, ca orice
    alt tip din ea să rămână cerut.
    """
    from watchdog.observers import winapi

    return winapi.WATCHDOG_FILE_NOTIFY_FLAGS & ~winapi.FILE_NOTIFY_CHANGE_LAST_ACCESS


def apply() -> bool:
    """
    Aplică ambele corecturi o singură dată. Întoarce True dacă sunt active.

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
        winapi.WATCHDOG_FILE_NOTIFY_FLAGS = content_notify_flags()
        _applied = True

    return True
