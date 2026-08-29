"""
Teste pentru registrul de fir (pasul 1.3b.1).
==============================================

Registrul e locul unde numaratorul afirmatiei centrale inceteaza sa fie o
estimare. METRICS.md par. 7 cere sa fie masurat; masuratoarea se face aici, pe
corpul deja serializat de serialize_payload(), in agent, inainte si dupa fiecare
cerere.

Testele apara patru lucruri, in ordinea in care ar strica cifra daca ar ceda:

    1. Cele doua momente sunt independente. `attempted` creste inainte de
       trimitere, `delivered` la orice raspuns. Daca ar fi topite intr-unul
       singur, un agent care trimite ore in sir in gol ar raporta zero
       divulgare — exact cazul pe care metrica trebuie sa-l arate.

    2. Canalele nu se amesteca. Par. 1.4 cere heartbeat-urile raportate ca prag,
       nu topite in divulgare; podeaua aceea e asimptota curbei din par. 3.1.

    3. Octetii nu dispar. Un canal gresit ridica eroare, nu deschide tacut o a
       patra galeata pe care n-o citeste nimeni.

    4. Doua fire nu se pierd unul pe altul. Dispecerul si bucla de heartbeat
       scriu simultan; o incrementare pierduta face cifra mai MICA decat
       realitatea, adica greseste in favoarea celui care publica metrica.

Niciun test de aici nu atinge reteaua: registrul e un obiect de numarat, nu de
trimis.
"""

import sys
import threading
import unittest

from services.wire_ledger import (
    CHANNEL_CONTROL,
    CHANNEL_ENROLLMENT,
    CHANNEL_EVENTS,
    VALID_CHANNELS,
    ChannelCounters,
    WireLedger,
)


class TwoDistinctMomentsTests(unittest.TestCase):
    """`attempted` si `delivered` nu se implica unul pe altul."""

    def test_an_attempt_alone_counts_as_departed_but_not_delivered(self) -> None:
        # Scenariul concret: timeout sau conexiune cazuta. Octetii au parasit
        # endpoint-ul, deci par. 1.1 ii numara, dar serverul nu i-a vazut.
        ledger = WireLedger()

        ledger.record_attempt(CHANNEL_EVENTS, 240)

        counters = ledger.snapshot().channels[CHANNEL_EVENTS]
        self.assertEqual(counters.attempted_bytes, 240)
        self.assertEqual(counters.attempted_messages, 1)
        self.assertEqual(counters.delivered_bytes, 0)
        self.assertEqual(counters.delivered_messages, 0)

    def test_the_gap_between_the_two_is_what_left_into_nothing(self) -> None:
        ledger = WireLedger()

        ledger.record_attempt(CHANNEL_EVENTS, 100)
        ledger.record_delivery(CHANNEL_EVENTS, 100)
        ledger.record_attempt(CHANNEL_EVENTS, 60)

        counters = ledger.snapshot().channels[CHANNEL_EVENTS]
        self.assertEqual(counters.undelivered_bytes, 60)
        self.assertEqual(counters.undelivered_messages, 1)

    def test_a_refused_message_is_still_a_delivered_message(self) -> None:
        # Registrul nu vede coduri HTTP, si tocmai asta e ideea: apelantul
        # cheama record_delivery() la ORICE raspuns, inclusiv 401 sau 422. Ca sa
        # te poata refuza, serverul trebuie mai intai sa fi primit mesajul.
        # Daca refuzurile ar fi tratate ca nelivrari, un agent cu cheia gresita
        # care trimite ore in sir ar aparea ca si cum n-ar divulga nimic.
        ledger = WireLedger()

        ledger.record_attempt(CHANNEL_EVENTS, 150)
        ledger.record_delivery(CHANNEL_EVENTS, 150)

        counters = ledger.snapshot().channels[CHANNEL_EVENTS]
        self.assertEqual(counters.delivered_bytes, 150)
        self.assertEqual(counters.undelivered_bytes, 0)

    def test_a_retransmission_counts_every_time_it_leaves(self) -> None:
        # Par. 1.3: coada e at-least-once, deci acelasi eveniment poate pleca de
        # mai multe ori. Se numara fiecare plecare, nu evenimentul distinct.
        ledger = WireLedger()

        for _ in range(3):
            ledger.record_attempt(CHANNEL_EVENTS, 200)

        counters = ledger.snapshot().channels[CHANNEL_EVENTS]
        self.assertEqual(counters.attempted_bytes, 600)
        self.assertEqual(counters.attempted_messages, 3)


class ChannelsStaySeparateTests(unittest.TestCase):
    """Par. 1.4: controlul se raporteaza ca prag, nu topit in divulgare."""

    def test_control_bytes_never_land_in_the_events_channel(self) -> None:
        ledger = WireLedger()

        ledger.record_attempt(CHANNEL_CONTROL, 90)
        ledger.record_attempt(CHANNEL_ENROLLMENT, 300)

        channels = ledger.snapshot().channels
        self.assertEqual(channels[CHANNEL_EVENTS].attempted_bytes, 0)
        self.assertEqual(channels[CHANNEL_CONTROL].attempted_bytes, 90)
        self.assertEqual(channels[CHANNEL_ENROLLMENT].attempted_bytes, 300)

    def test_every_declared_channel_exists_from_the_start(self) -> None:
        # Un canal care apare abia la primul mesaj ar face un agent tacut
        # imposibil de deosebit de unul fara canalul acela. Zero afirmat e
        # verificabil, absenta nu — acelasi rationament ca la blocul disclosure.
        channels = WireLedger().snapshot().channels

        self.assertEqual(set(channels), set(VALID_CHANNELS))
        for channel in VALID_CHANNELS:
            self.assertEqual(channels[channel], ChannelCounters())

    def test_totals_are_the_sum_across_channels(self) -> None:
        # Reconcilierea agent-server se face pe total: serverul numara octetii
        # primiti de la o incarnare fara sa-i desparta pe canale.
        ledger = WireLedger()

        ledger.record_attempt(CHANNEL_EVENTS, 100)
        ledger.record_delivery(CHANNEL_EVENTS, 100)
        ledger.record_attempt(CHANNEL_CONTROL, 40)
        ledger.record_delivery(CHANNEL_CONTROL, 40)
        ledger.record_attempt(CHANNEL_ENROLLMENT, 500)

        totals = ledger.snapshot().totals
        self.assertEqual(totals.attempted_bytes, 640)
        self.assertEqual(totals.attempted_messages, 3)
        self.assertEqual(totals.delivered_bytes, 140)
        self.assertEqual(totals.delivered_messages, 2)


class BytesNeverVanishTests(unittest.TestCase):
    """Un octet nenumarat e mai rau decat unul numarat gresit: nu se vede."""

    def test_an_unknown_channel_raises_instead_of_opening_a_bucket(self) -> None:
        ledger = WireLedger()

        with self.assertRaises(ValueError):
            ledger.record_attempt("evenimente", 100)

        # Si nu lasa nimic in urma: nici canal nou, nici octeti pierduti pe
        # vreunul existent.
        channels = ledger.snapshot().channels
        self.assertEqual(set(channels), set(VALID_CHANNELS))
        self.assertEqual(ledger.snapshot().totals.attempted_bytes, 0)

    def test_the_same_strictness_applies_to_delivery(self) -> None:
        ledger = WireLedger()

        with self.assertRaises(ValueError):
            ledger.record_delivery("control-plane", 100)

        self.assertEqual(ledger.snapshot().totals.delivered_bytes, 0)

    def test_a_negative_byte_count_is_refused(self) -> None:
        # Un numar negativ ar scadea din total, adica ar ascunde divulgare
        # deja intamplata. Singura sursa posibila e un bug de calcul la
        # apelant, iar tacerea l-ar face invizibil.
        ledger = WireLedger()

        with self.assertRaises(ValueError):
            ledger.record_attempt(CHANNEL_EVENTS, -1)

        self.assertEqual(ledger.snapshot().totals.attempted_bytes, 0)

    def test_a_zero_byte_body_is_a_message_even_if_it_is_not_bytes(self) -> None:
        # Zero octeti nu e o eroare, e un mesaj gol: contorul de mesaje creste,
        # cel de octeti nu. Distinctia conteaza pentru dimensiunea tipica de
        # mesaj, pe care se calibreaza pragul de discrepanta.
        ledger = WireLedger()

        ledger.record_attempt(CHANNEL_CONTROL, 0)

        counters = ledger.snapshot().channels[CHANNEL_CONTROL]
        self.assertEqual(counters.attempted_bytes, 0)
        self.assertEqual(counters.attempted_messages, 1)


class SnapshotIsAPhotographTests(unittest.TestCase):
    def test_a_snapshot_does_not_change_after_later_writes(self) -> None:
        # Daca snapshot() ar intoarce dictionarul viu, o cifra citita in timp ce
        # dispecerul trimite ar amesteca doua stari si ar descrie un moment care
        # n-a existat.
        ledger = WireLedger()
        ledger.record_attempt(CHANNEL_EVENTS, 100)

        snapshot = ledger.snapshot()
        ledger.record_attempt(CHANNEL_EVENTS, 900)

        self.assertEqual(snapshot.channels[CHANNEL_EVENTS].attempted_bytes, 100)
        self.assertEqual(snapshot.totals.attempted_bytes, 100)
        self.assertEqual(ledger.snapshot().totals.attempted_bytes, 1000)

    def test_the_ledger_has_no_reset(self) -> None:
        # D3, scris in cod ca sa nu se piarda: registrul e per incarnare, in
        # memorie. Un reset la cerere ar rupe atribuirea — totalul n-ar mai
        # spune cat a divulgat incarnarea asta, ci cat a divulgat de la un
        # moment pe care nu-l vede nimeni din afara. Repornirea agentului e
        # incarnare noua, deci proces nou, deci obiect nou.
        self.assertFalse(hasattr(WireLedger(), "reset"))


class ConcurrentWritersTests(unittest.TestCase):
    """
    Dispecerul si bucla de heartbeat scriu de pe fire diferite.

    De ce umblam la intervalul de comutare al interpretorului:
        Prima versiune a testelor de aici trecea si cu lacatul scos — adica nu
        apara nimic. Motivul e ca sectiunea critica (citeste contorul, compune
        unul nou, pune-l la loc) dureaza cateva microsecunde, iar GIL-ul comuta
        firele o data la 5 milisecunde, deci sansa sa cada exact intre citire si
        scriere e practic zero pe o rulare scurta.

        Masurat pe registrul fara lacat: la intervalul implicit se pierdeau 0
        din 8000 de incrementari; la un interval de o microsecunda se pierdeau
        peste 5000. Cursa exista in ambele cazuri — doar ca implicit nu se vede,
        si ar fi aparut in productie, unde firele chiar fac I/O intre operatii.

        Coborand intervalul, cursa devine reproductibila, iar testul chiar cade
        daca cineva scoate lacatul. Valoarea se pune inapoi in tearDown, ca sa
        nu afecteze restul suitei.
    """

    def setUp(self) -> None:
        self._original_switch_interval = sys.getswitchinterval()
        sys.setswitchinterval(0.000001)

    def tearDown(self) -> None:
        sys.setswitchinterval(self._original_switch_interval)

    def test_no_increment_is_lost_between_two_threads(self) -> None:
        ledger = WireLedger()
        increments = 2000

        def write(channel: str, size: int) -> None:
            for _ in range(increments):
                ledger.record_attempt(channel, size)
                ledger.record_delivery(channel, size)

        threads = [
            threading.Thread(target=write, args=(CHANNEL_EVENTS, 7)),
            threading.Thread(target=write, args=(CHANNEL_CONTROL, 3)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        channels = ledger.snapshot().channels
        self.assertEqual(channels[CHANNEL_EVENTS].attempted_bytes, 7 * increments)
        self.assertEqual(channels[CHANNEL_CONTROL].attempted_bytes, 3 * increments)
        self.assertEqual(
            ledger.snapshot().totals.delivered_messages, 2 * increments
        )

    def test_two_threads_on_the_same_channel_do_not_overwrite_each_other(self) -> None:
        # Cazul mai greu: acelasi canal, deci acelasi obiect frozen inlocuit de
        # amandoua firele. Fara lacat, citirea si scrierea se intercaleaza si o
        # parte din incrementari se pierd — cifra ar iesi mai mica decat
        # realitatea, adica in favoarea afirmatiei de confidentialitate.
        ledger = WireLedger()
        increments = 2000

        def write() -> None:
            for _ in range(increments):
                ledger.record_attempt(CHANNEL_EVENTS, 5)

        threads = [threading.Thread(target=write) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        counters = ledger.snapshot().channels[CHANNEL_EVENTS]
        self.assertEqual(counters.attempted_messages, 4 * increments)
        self.assertEqual(counters.attempted_bytes, 5 * 4 * increments)


if __name__ == "__main__":
    unittest.main()
