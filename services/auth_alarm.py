"""
Alarma progresivă pentru eșecurile de autentificare.
====================================================

De ce e nevoie de un mecanism separat, și nu doar de un log per încercare:

    Un eșec de autentificare nu e nici temporar ca o pană de rețea, nici
    definitiv ca un payload invalid. E o proprietate a relației dintre agent și
    server la momentul cererii, iar durata e singura care le departajează:

      - 30 de secunde de 401 = o rotație de cheie prinsă la mijloc, un deploy
        în curs, un server repornit. Se repară singură;
      - 30 de minute de 401 = ceva ce nu se repară fără om, iar în tot acest
        timp mașina e nemonitorizată din punctul de vedere al serverului.

    Amândouă arată identic la nivel de cerere. Un WARNING per încercare le-ar
    trata la fel, iar cel important s-ar pierde între celelalte. De aceea
    escaladarea se face pe TIMP SCURS, nu pe număr de încercări: încercările
    sunt rărite de backoff exponențial (services/backoff.py), deci contorul lor
    descrie retry-urile, nu durata problemei — aceeași distincție pe care
    serverul o face deja între failed_attempts și missed_heartbeats.

De ce time.monotonic și nu wall-clock:
    Un ceas de sistem care sare — corectat de NTP, mutat manual, resetat de o
    baterie moartă — ar putea altfel să declanșeze o alarmă instantaneu sau să
    o amâne la nesfârșit. Ceasul monoton nu poate merge înapoi.

Ce raportează alarma când se declanșează:
    Adâncimea cozii, prin detail_provider. Nu ca ornament: „autentificarea eșuează
    de 32 de minute" e o informație de operare, dar „…și 4200 de evenimente
    așteaptă pe disc" e cea care spune cât de urgent e și ce se pierde dacă
    endpoint-ul e reinstalat înainte de reparație. Se calculează leneș, doar când
    o alarmă chiar se emite — interogarea cozii nu are ce căuta pe fiecare eșec.
"""

import logging
import time
from typing import Callable, Optional


DEFAULT_FIRST_ALARM_SECONDS = 300.0     # ~5 minute
DEFAULT_REPEAT_ALARM_SECONDS = 1800.0   # ~30 de minute


class AuthFailureAlarm:
    """
    Ține evidența unei serii neîntrerupte de eșecuri de autentificare pe un canal.

    Un canal = un drum independent către server (livrarea evenimentelor,
    heartbeat-ul, înregistrarea). Fiecare are propria alarmă, pentru că fiecare
    poate eșua separat și pentru că mesajul util diferă.
    """

    def __init__(
        self,
        channel: str,
        first_alarm_seconds: float = DEFAULT_FIRST_ALARM_SECONDS,
        repeat_alarm_seconds: float = DEFAULT_REPEAT_ALARM_SECONDS,
        logger: Optional[logging.Logger] = None,
        clock: Callable[[], float] = time.monotonic,
        subject: str = "authentication",
        remedy: str = "Check the agent key, the server key store and the clock.",
    ):
        self.channel = channel
        # subject si remedy exista pentru ca aceeasi escaladare pe timp scurs
        # se aplica si altor refuzuri decat cele de autentificare — un conflict
        # de identitate, de pilda. Textul implicit reproduce exact mesajele de
        # dinaintea parametrilor, deci canalele existente raman neschimbate.
        self.subject = subject
        self.remedy = remedy
        self.first_alarm_seconds = float(first_alarm_seconds)
        self.repeat_alarm_seconds = float(repeat_alarm_seconds)
        self.logger = logger or logging.getLogger(__name__)
        self._clock = clock

        self._failing_since: Optional[float] = None
        self._next_alarm_at: Optional[float] = None
        self._alarms_raised = 0

    @property
    def is_failing(self) -> bool:
        return self._failing_since is not None

    @property
    def failing_for_seconds(self) -> float:
        if self._failing_since is None:
            return 0.0
        return max(0.0, self._clock() - self._failing_since)

    def record_failure(
        self,
        message: str,
        detail_provider: Optional[Callable[[], str]] = None,
        attempt_level: int = logging.WARNING,
    ) -> None:
        """
        Înregistrează un eșec de autentificare și escaladează dacă e cazul.

        Fiecare încercare produce o linie la attempt_level (implicit WARNING).
        Peste pragul de timp se emite în plus o linie CRITICAL, apoi câte una la
        fiecare interval de repetare — cu detaliul furnizat de detail_provider.
        """
        now = self._clock()

        if self._failing_since is None:
            self._failing_since = now
            self._next_alarm_at = now + self.first_alarm_seconds
            self._alarms_raised = 0

        elapsed = now - self._failing_since

        self.logger.log(
            attempt_level,
            "%s: %s refused by server (%s). Failing for %.0fs; "
            "nothing is being discarded.",
            self.channel,
            self.subject,
            message,
            elapsed,
        )

        if self._next_alarm_at is None or now < self._next_alarm_at:
            return

        self._alarms_raised += 1
        self._next_alarm_at = now + self.repeat_alarm_seconds

        detail = ""
        if detail_provider is not None:
            try:
                detail = " " + detail_provider()
            except Exception:  # pragma: no cover — detaliul nu are voie să rupă alarma
                self.logger.debug("Alarm detail provider failed.", exc_info=True)

        self.logger.critical(
            "%s: %s has been failing for %.0f minutes (alarm #%d). This endpoint "
            "is effectively unmonitored server-side. %s%s",
            self.channel,
            self.subject,
            elapsed / 60.0,
            self._alarms_raised,
            self.remedy,
            detail,
        )

    def record_success(self) -> None:
        """Închide o serie de eșecuri. Tăcut dacă nu era niciuna deschisă."""
        if self._failing_since is None:
            return

        elapsed = self.failing_for_seconds
        raised = self._alarms_raised

        self._failing_since = None
        self._next_alarm_at = None
        self._alarms_raised = 0

        # Recuperarea se loghează la nivelul la care a fost raportată problema:
        # o alarmă CRITICAL fără o închidere la fel de vizibilă lasă operatorul
        # să caute o defecțiune care nu mai există.
        level = logging.CRITICAL if raised else logging.INFO
        self.logger.log(
            level,
            "%s: %s recovered after %.0fs.",
            self.channel,
            self.subject,
            elapsed,
        )
