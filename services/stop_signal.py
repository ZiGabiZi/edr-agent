"""
Contractul de oprire partajat între buclele agentului.
=======================================================

Buclele lungi (startup, heartbeat, dispatcher) nu au nevoie de un
threading.Event complet: ele doar *interoghează* semnalul și *așteaptă* pe el.
Nu îl setează niciodată — setarea aparține exclusiv lui run_agent(), prin
handler-ele de semnal și prin blocul finally.

Exprimarea acestui contract ca Protocol (PEP 544) în loc de threading.Event
are două efecte:

    1. Documentează în semnătură că bucla este un consumator pasiv al
       semnalului de oprire — nu poate opri agentul, doar reacționa la oprire.
    2. Permite substituirea în teste (vezi FakeStopEvent din
       tests/test_agent_startup.py) fără cast-uri și fără a moșteni un obiect
       de sincronizare real: un backoff de 5–60s devine instantaneu, iar
       numărul de așteptări devine observabil.

threading.Event satisface structural acest protocol, deci apelanții din
producție rămân complet neschimbați.
"""

from typing import Optional, Protocol


class StopSignal(Protocol):
    """Un semnal de oprire pe care se poate aștepta cu timeout."""

    def is_set(self) -> bool:
        """True dacă oprirea a fost cerută."""
        ...

    def wait(self, timeout: Optional[float] = None) -> bool:
        """
        Așteaptă cel mult `timeout` secunde. Întoarce starea semnalului la
        ieșire — deci True înseamnă „oprire cerută", nu „timp expirat".
        """
        ...