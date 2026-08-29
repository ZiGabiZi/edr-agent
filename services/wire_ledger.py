"""
Registrul de fir: câți octeți a pus agentul efectiv pe canalul agent→server.

De ce există modulul ăsta:
    METRICS.md §7 cere ca numărătorul afirmației centrale să fie **măsurat, nu
    estimat**. Până acum singura cifră disponibilă venea din reserializarea
    evenimentului *stocat pe server* — o aproximare care include câmpuri
    adăugate de server și exclude tot ce n-a ajuns niciodată acolo.

    Registrul numără la sursă: în agent, pe corpul deja serializat, exact
    înainte și exact după fiecare cerere. serialize_payload() din
    services/transport.py e cea care a făcut cifra asta posibilă; aici e locul
    unde se adună.

Ce NU face:
    Nu ține octeți per eveniment, ci totaluri per canal. Distribuția per fișier
    cerută de METRICS.md §3.2 (mediană, p95) are nevoie de o valoare pentru
    fiecare mesaj în parte, iar singurul mod de a o avea în mesaj ar fi să pui
    dimensiunea mesajului în mesajul însuși — circular.

    Asimetria rămâne, deliberat: totalul e măsurat, cifrele per fișier rămân
    estimate. Dar comparând totalul măsurat cu suma estimărilor obții cât de
    mult greșește estimatorul, iar cu factorul acela poți mărgini onest cifrele
    per fișier. Registrul nu înlocuiește estimatorul — îl calibrează.
"""

import threading
from dataclasses import dataclass
from typing import Dict, Mapping


# Canalele, separate pentru că METRICS.md §1.4 le separă.
#
# Heartbeat-urile sunt proporționale cu TIMPUL, nu cu numărul de fișiere, și se
# raportează ca prag, nu topite în totalul de divulgare. Consecința e că nu
# există un singur contor corect: costul per endpoint nu poate coborî sub
# podeaua canalului de control, iar podeaua aceea e asimptota curbei din §3.1.
# Cu un contor unic ea ar fi imposibil de calculat din telemetrie.
#
# Înrolarea stă separat de amândouă pentru că e proporțională cu numărul de
# reporniri, nu cu timpul și nu cu fișierele: a treia lege de creștere merită a
# treia linie.
CHANNEL_EVENTS = "events"
CHANNEL_CONTROL = "control"
CHANNEL_ENROLLMENT = "enrollment"

VALID_CHANNELS = (CHANNEL_EVENTS, CHANNEL_CONTROL, CHANNEL_ENROLLMENT)


@dataclass(frozen=True)
class ChannelCounters:
    """
    Ce s-a întâmplat pe un canal, în două momente distincte.

    attempted — octeții predați stratului de transport, indiferent de soartă.
        Momentul acesta e definiția din METRICS.md §1.1: un octet e divulgat
        dacă a **părăsit endpoint-ul**, nu dacă a fost acceptat. Retransmisiile
        se numără de fiecare dată (§1.3), pentru că de fiecare dată pleacă din
        nou.

    delivered — octeții pentru care a venit **orice** răspuns HTTP.
        Inclusiv 401 și 422. Un refuz nu e o nelivrare: ca să te poată refuza,
        serverul trebuie mai întâi să fi primit mesajul. Confuzia asta ar
        ascunde exact traficul cel mai interesant — un agent cu cheia greșită
        care trimite ore în șir fără succes divulgă tot ce trimite.

    Diferența dintre cele două e volumul plecat în gol: timeout-uri și conexiuni
    căzute, unde attempted a crescut, iar delivered a rămas pe loc.
    """

    attempted_bytes: int = 0
    attempted_messages: int = 0
    delivered_bytes: int = 0
    delivered_messages: int = 0

    @property
    def undelivered_bytes(self) -> int:
        """Octeți plecați de pe endpoint pentru care n-a venit niciun răspuns."""
        return self.attempted_bytes - self.delivered_bytes

    @property
    def undelivered_messages(self) -> int:
        """Mesaje plecate de pe endpoint pentru care n-a venit niciun răspuns."""
        return self.attempted_messages - self.delivered_messages


@dataclass(frozen=True)
class WireLedgerSnapshot:
    """
    O fotografie coerentă a registrului, luată sub lacăt.

    Există ca tip separat, în loc să întoarcem dicționarul viu, din același
    motiv pentru care contoarele sunt frozen: un apelant care citește pe rând
    două canale în timp ce dispecerul trimite ar compune o cifră care n-a
    existat niciodată. Fotografia se ia dintr-o singură dată și nu se mai
    schimbă după.

    totals e suma peste canale, pentru reconcilierea agent↔server: serverul
    numără octeții primiți de la o încarnare fără să-i despartă pe canale, deci
    comparația se face pe total. Separarea pe canale rămâne pentru afirmațiile
    din METRICS.md, nu pentru reconciliere.
    """

    channels: Mapping[str, ChannelCounters]
    totals: ChannelCounters


class WireLedger:
    """
    Contoare de octeți pe canal, pentru o singură încarnare a agentului.

    De ce e protejat cu lacăt:
        Dispecerul de evenimente și bucla de heartbeat rulează pe fire diferite
        și scriu în același registru. Fără lacăt, două incrementări simultane se
        pot pierde una pe alta, iar cifra rezultată ar fi mai mică decât
        realitatea — adică exact în direcția care face afirmația de
        confidențialitate să arate mai bine decât e. O metrică nu are voie să
        greșească în favoarea celui care o publică.

    De ce nu are reset():
        Registrul e per încarnare, în memorie, și moare odată cu procesul. Un
        reset la cerere ar rupe atribuirea: totalul n-ar mai spune „atât a
        divulgat încarnarea asta", ci „atât a divulgat de la ultimul reset, pe
        care nu-l vede nimeni din afară". Repornirea agentului e o încarnare
        nouă, deci un obiect nou — la fel ca la detecția de repornire prin
        agent_instance_id, unde resetul e așteptat tocmai pentru că e
        atribuibil.

    De ce încarnarea nu e un câmp aici:
        Registrul numără, nu se identifică. Legătura cu agent_instance_id se
        face la emiterea anteturilor și pe server, unde perechea
        (agent_id, agent_instance_id) e cheia de contabilizare. Un contor care
        își ține singur identitatea ar putea fi refolosit peste încarnări, ceea
        ce e exact ce nu vrem.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._channels: Dict[str, ChannelCounters] = {
            channel: ChannelCounters() for channel in VALID_CHANNELS
        }

    def record_attempt(self, channel: str, byte_count: int) -> None:
        """
        Înregistrează octeții predați transportului, înainte de trimitere.

        Se apelează ÎNAINTE, nu după, și nu doar la succes: dacă cererea moare
        într-un timeout, octeții tot au părăsit endpoint-ul, iar §1.1 îi numără.
        Un contor incrementat la succes ar raporta zero divulgare exact în
        scenariul în care agentul trimite fără oprire și nu primește nimic
        înapoi.
        """
        self._add(channel, byte_count, delivered=False)

    def record_delivery(self, channel: str, byte_count: int) -> None:
        """
        Înregistrează octeții pentru care serverul a răspuns ceva, orice.

        Se apelează la ORICE răspuns HTTP, inclusiv 401, 403, 409 și 422.
        Criteriul e „a ajuns la server", nu „a fost acceptat de server" — vezi
        ChannelCounters.

        byte_count trebuie să fie același număr dat lui record_attempt pentru
        aceeași cerere; apelantul are corpul serializat într-o singură variabilă
        exact ca să nu poată fi altul.
        """
        self._add(channel, byte_count, delivered=True)

    def snapshot(self) -> WireLedgerSnapshot:
        """Fotografia coerentă a tuturor canalelor, plus totalul peste ele."""
        with self._lock:
            channels = dict(self._channels)

        totals = ChannelCounters(
            attempted_bytes=sum(c.attempted_bytes for c in channels.values()),
            attempted_messages=sum(c.attempted_messages for c in channels.values()),
            delivered_bytes=sum(c.delivered_bytes for c in channels.values()),
            delivered_messages=sum(c.delivered_messages for c in channels.values()),
        )

        return WireLedgerSnapshot(channels=channels, totals=totals)

    def _add(self, channel: str, byte_count: int, delivered: bool) -> None:
        """
        Incrementarea propriu-zisă, cu validările comune celor două momente.

        Canalul necunoscut ridică ValueError, nu creează tăcut o găleată nouă.
        Motivul: canalul e ales de programator dintr-o mulțime închisă, nu vine
        din date, deci o valoare nouă e un bug de cod, nu o stare de rulare. Un
        al patrulea canal apărut dintr-o greșeală de tastare ar rupe podeaua din
        §1.4 fără ca nimeni să observe — heartbeat-urile s-ar amesteca în
        divulgare, iar asimptota ar fi calculată greșit.

        Consecința pentru apelanți: canalul se dă întotdeauna prin constantele
        de mai sus, niciodată printr-un literal scris la fața locului.
        """
        if channel not in self._channels:
            raise ValueError(
                f"Canal de fir necunoscut: {channel!r}. "
                f"Canalele valide sunt {VALID_CHANNELS}."
            )

        if byte_count < 0:
            raise ValueError(
                f"Numar de octeti negativ pe canalul {channel!r}: {byte_count}."
            )

        with self._lock:
            current = self._channels[channel]

            if delivered:
                self._channels[channel] = ChannelCounters(
                    attempted_bytes=current.attempted_bytes,
                    attempted_messages=current.attempted_messages,
                    delivered_bytes=current.delivered_bytes + byte_count,
                    delivered_messages=current.delivered_messages + 1,
                )
            else:
                self._channels[channel] = ChannelCounters(
                    attempted_bytes=current.attempted_bytes + byte_count,
                    attempted_messages=current.attempted_messages + 1,
                    delivered_bytes=current.delivered_bytes,
                    delivered_messages=current.delivered_messages,
                )


# Registrul încarnării curente.
#
# De ce o instanță la nivel de modul, când globalele sunt de obicei o problemă:
#     Apelurile de transport sunt împrăștiate — dispecerul cheamă send_event,
#     agent.py cheamă send_heartbeat, register_agent și tot send_event pentru
#     evenimentele de ciclu de viață. Pasarea unui registru prin toate ar
#     însemna un parametru nou pe fiecare, iar un apelant care uită să-l dea nu
#     primește o eroare: primește octeți nenumărați. Adică exact modul de eșec
#     pe care metrica trebuie să-l excludă.
#
# De ce nu strică D3, deși e o stare globală:
#     Un proces ESTE o încarnare. Obiectul se creează la import și moare cu
#     procesul, deci durata lui de viață coincide exact cu ce trebuia să
#     măsurăm. Aici globalul nu e un compromis, e chiar semantica cerută:
#     dacă l-am construi la apel, cineva ar putea construi două într-o
#     încarnare, iar totalul ar înceta să mai fie al încarnării.
#
# Ce rămâne testabil:
#     post_request acceptă un registru injectat. Testele își dau al lor și nu
#     ating instanța asta, deci nu depind de ordinea în care rulează.
_INCARNATION_LEDGER = WireLedger()


def get_wire_ledger() -> WireLedger:
    """
    Registrul încarnării curente, cel folosit implicit de transport.

    Funcție, nu import direct al variabilei: un `from wire_ledger import
    _INCARNATION_LEDGER` ar lega numele la momentul importului și ar face
    înlocuirea imposibil de urmărit. Prin funcție, toți apelanții văd același
    obiect și există un singur loc de căutat când te întrebi cine numără.
    """
    return _INCARNATION_LEDGER
