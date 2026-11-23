# edr-agent

Agentul care rulează pe endpoint și decide **ce anume merită să părăsească
mașina**.

Trimite amprente SHA-256 și metadate, niciodată conținut de fișier. Pe prima
măsurare, din 25–26 august 2026 — 444 de fișiere sintetice, 143,1 MiB — asta a
însemnat 264 KB în loc de 143 MB, adică de aproximativ 567 de ori mai puțin.
Cifra e calculată de [edr-server](https://github.com/ZiGabiZi/edr-server), care
primește datele și contabilizează raportul, și e a definiției metricii de
atunci: n-a fost recalculată de când numărătorul se măsoară pe fir, iar
numitorul include și plicul.

Lucrare de licență.

**Unde a ajuns (18 noiembrie 2026).** Agentul observă directoarele configurate,
calculează amprenta fiecărui fișier stabilizat și o livrează cu garanție
*at-least-once*, autentificat cu cheia proprie. Pe heartbeat raportează dacă
supravegherea e întreagă, iar în antete câți octeți a pus pe fir, ca serverul
să poată verifica cifra. Toate evenimentele de fișier se opresc la treapta T0:
agentul nu trimite încă nimic din conținut și nu răspunde la cereri de
escaladare. Rulează pe cinci endpoint-uri Windows clonate, fiecare cu identitate
proprie, înaintea măsurătorii pe parc.

---

## Lanțul: de la o scriere pe disc la un eveniment livrat

Agentul e, în esență, o conductă cu trei etaje de fire, plus livrarea. Fiecare
etaj există ca să protejeze garanția etajului dinaintea lui de latența etajului
de după.

```
   scriere pe disc
        │
        ▼
   [ watchdog ]          fir observer, timp constant
        │                 nu face nimic costisitor: doar înregistrează
        ▼
   [ settle tracker ]    coalescează zeci de evenimente într-unul singur
        │                 eliberează după o perioadă de liniște, cu plafon dur
        ▼
   [ file hasher ]       fir propriu, poate dura
        │                 stat → SHA-256 → re-stat; dacă s-a schimbat, reia
        ▼
   [ event spool ]       coadă SQLite pe disc
        │                 evenimentul e al ei până când serverul confirmă
        ▼
   [ dispatcher ]        fir propriu, cu backoff exponențial
        │
        ▼
     serverul
```

### De ce fiecare etaj

**Watchdog nu poate aștepta.** Firul care primește notificările de la sistemul
de operare trebuie să rămână liber; dacă ar hash-ui acolo, ar pierde notificări
sub rafală.

**Un fișier scris în bucăți produce zeci de evenimente.** Tracker-ul le
coalesce: un fișier, un eveniment. Cu plafon dur, altfel un fișier care se
schimbă la nesfârșit (un log) n-ar fi raportat niciodată — orice mecanism de
suprimare fără termen limită poate suprima pentru totdeauna.

**Amprenta trebuie să descrie un fișier finit.** Hasher-ul verifică fișierul
înainte *și* după citire. Dacă s-a schimbat între timp, îl reintroduce în
așteptare; după un plafon de reintroduceri îl raportează `unstable`, fără
amprentă. Un SHA-256 al unei stări intermediare, care n-a existat niciodată ca
fișier finit, e mai rău decât niciun hash: serverul l-ar căuta într-o reputație,
ar primi „necunoscut" și ar escalada zgomot — sau, mai rău, ar rata un fișier
malițios cunoscut.

**Coada e pe disc, nu în memorie.** Un eveniment se șterge doar după confirmarea
serverului. Server căzut, rețea picată, agent oprit brusc — evenimentele așteaptă
și pleacă singure la reconectare.

### Ce mai pleacă, în afară de evenimente

**Heartbeat-ul spune dacă endpoint-ul chiar e supravegheat.** La fiecare
heartbeat agentul verifică observer-ul, firele conductei și fiecare director
configurat, și trimite câmpul `monitoring`: `active`, `partial` (o parte din
directoare lipsește), `spool_unavailable`, `monitor_unavailable` sau `stopped`.
Un director șters și recreat e pus singur înapoi sub observer, iar starea revine
la `active` fără repornire. Fără câmpul ăsta, un agent viu cu conducta oprită ar
arăta exact ca unul liniștit.

**Antetele de fir.** Fiecare cerere poartă `X-Agent-Wire-Attempted-Bytes`,
`X-Agent-Wire-Delivered-Bytes` și `X-Agent-Instance-Id`: câți octeți a predat
agentul transportului și pentru câți a primit răspuns, de la pornirea procesului.
Serverul le compară cu ce a primit el, ca cifra de divulgare să fie măsurată,
nu estimată (`contracts/METRICS.md` §7).

**Treapta de divulgare.** Fiecare eveniment de fișier poartă blocul
`disclosure`, cu treapta la care s-a oprit (azi mereu `T0`) și octeții de
conținut trimiși (azi mereu zero).

Răspunsul serverului la un eveniment poartă dispoziția de reputație și
prevalența conținutului în parc. Agentul le primește, dar nu le folosește încă:
nu există nimic de făcut cu ele până la treapta T1.

---

## Proprietățile care contează pe un endpoint

Un agent EDR nu rulează într-un centru de date. Rulează pe laptopul cuiva, care
se deconectează, se reinstalează, se redenumește și se oprește la mijlocul unei
scrieri. Trei reguli decurg din asta și explică multe alegeri din cod.

### Nu pierde dovezi, niciodată, din nicio cauză

Criteriul de eliminare a unui eveniment din coadă este „**proprietate a
mesajului**", nu „cod HTTP de eroare".

`422` înseamnă că payload-ul e stricat: nu va deveni valid dacă îl mai trimiți o
dată, deci se elimină. `401` înseamnă „nu te cunosc **acum**" — o proprietate a
relației cu serverul, care poate fi falsă peste zece minute, din cauze fără
legătură cu evenimentul.

Tratate la fel, cinci minute de cheie greșită ar fi șters ireversibil toată
coada. Consecința de securitate e mai directă: dacă spargerea autentificării ar
goli coada, „strică cheia" ar deveni o metodă prin care dovezile dispar singure.

`401`, `403` și `409` au deci excepții proprii, niciuna descinzând din
`FatalTransportError`. Regula e fixată printr-un test pe **ierarhia de
excepții** — pentru că exact ierarhia e ce citește dispatcher-ul, iar un test de
flux ar putea rămâne verde după o refactorizare care o rupe.

### Nu tăcea

Nicio eroare de configurare sau credențiale nu oprește procesul. Conflict de
nume, cheie revocată, secret lipsă — toate produc reîncercare cu alarmă
escaladată, nu oprire.

Motivul e operațional: o mașină care dispare din consolă nu se distinge de una
oprită sau compromisă. Pentru un EDR, tăcerea totală e cel mai prost simptom
posibil.

### Alarma escaladează pe timp scurs, nu pe încercări

Treizeci de secunde de refuz înseamnă o rotație de cheie prinsă la mijloc, care
se repară singură. Treizeci de minute înseamnă ceva ce nu se repară fără om, iar
mașina e nemonitorizată în tot acest timp. Amândouă arată identic la nivel de
cerere.

Escaladarea nu se poate face pe numărul de încercări, pentru că ele sunt rărite
de backoff exponențial: contorul descrie retry-urile, nu durata. Ceasul folosit
e monoton, ca o corecție NTP să nu declanșeze sau să amâne o alarmă.

---

## Ce stă pe disc

| Fișier | Conținut | Se copiază între mașini? |
|---|---|---|
| `config.example.json` | șablonul versionat, fără identitate | **da** — e artefactul livrat |
| `config.json` | configurarea mașinii, copiată din șablon | **nu** — diverge imediat |
| `enrollment_secret` | secret comun de înrolare | da, la instalare; se șterge după prima folosire |
| `agent_key` | cheia proprie, emisă de server | **nu** — stare locală, permisiuni restrânse |
| `agent_id` | identitatea, derivată o dată și înghețată | **nu** |
| `event_spool.db` | coada persistentă | **nu** |
| `agent.log` | jurnal rotativ | **nu** |

Împărțirea asta e ce face desfășurarea pe mai multe endpoint-uri posibilă dintr-o
singură imagine: `agent_id` se derivă din hostname plus amprenta mașinii la prima
pornire, apoi se îngheață. Înghețarea nu e opțională — dacă s-ar recalcula și
componenta s-ar schimba (o redenumire de mașină), cheia de API emisă pentru
numele vechi ar produce `403`, iar `403` nu se repară singur niciodată.

Detalii despre credențiale: [contracts/AUTH.md](contracts/AUTH.md).

---

## Rulare

```bash
python -m venv .venv && .venv\Scripts\activate && pip install -r requirements.txt
```

Suita trece pe Python 3.14.6; pe endpoint-uri agentul se instalează cu aceeași
versiune, într-un mediu virtual. `requirements.txt` fixează versiunile exacte,
inclusiv dependențele lui `requests`.

Configurarea nu vine gata din repo: `config.json` e ignorat de git, pentru că e
fișierul unei mașini, nu al proiectului. Se naște din șablon:

```bash
copy config.example.json config.json
```

Editează `server_url` — și `monitored_directories`, dacă nu monitorizezi calea
implicită. `agent_id` lipsește din șablon **deliberat**: absent, e derivat din
hostname plus amprenta mașinii și înghețat local, ceea ce face ca aceeași imagine
de instalare să poată fi desfășurată pe oricâte endpoint-uri. Scris fix în șablon,
a doua mașină ar revendica numele primei și ar fi respinsă cu `409`. Pune-l doar
dacă vrei un nume impus pe o mașină anume.

Pune secretul de înrolare în `enrollment_secret`, apoi:

```bash
.venv\Scripts\python.exe agent.py
```

Testele — **326**, fără rețea și fără server:

```bash
.venv\Scripts\python.exe -m unittest discover -s tests
```

Reglaje utile din `config.json`: `settle_quiet_seconds` (cât timp fără observații
noi înseamnă „s-a liniștit"), `hash_max_file_size_bytes` (peste cât nu se mai
citește deloc), `auth_alarm_first_seconds` și `auth_alarm_repeat_seconds`.

---

## Cum a fost construit

**Testele sunt garde împotriva eșecurilor tăcute, nu acoperire.** Clasa de bug
vizată e cea care nu se vede: o cheie de configurare validată pe care nimeni nu o
citește, un câmp aruncat fără urmă de serverul care îl primește, o reclasificare
de eroare care golește o coadă. De aceea multe teste verifică *traseul* unei
valori din `config.json` până la consumatorul ei, sau *ierarhia* unui tip.

**Lanțul complet e verificat cu un `Observer` real.**
`tests/test_file_pipeline_end_to_end.py` pornește watchdog pe un director
temporar și scrie fișiere reale. Motivul: toate celelalte teste presupun cum se
comportă watchdog — câte evenimente emite, în ce ordine — iar un test cu dubluri
nu poate infirma presupunerea, pentru că el *este* presupunerea.

**Măsoară, nu presupune.** Un issue bănuia că watchdog pierde evenimente sub
rafală. Măsurătoarea, făcută cu watchdog 6.0.0, a arătat 10.000 din 10.000
livrate la 1577 de scrieri pe secundă — iar pierderea era în întregime internă:
un plafon de zece fișiere pe secundă într-o buclă proprie, care după reparație a
devenit ~170. Issue-ul a fost rescris cu cifrele, cu ipoteza inițială păstrată
vizibil.

**watchdog e fixat pe versiune, iar corecturile peste el stau într-un singur
loc.** Pe o clonă cu două procesoare, 6.0.0 închidea de două ori handle-ul unui
director șters, iar procesul cădea cu `Fatal Python error: _PySemaphore_Wakeup`.
7.1.0 repară asta, dar aduce o cursă care poate bloca oprirea la nesfârșit;
`services/watchdog_compat.py` o corectează, iar `tests/test_watchdog_compat.py`
pică la orice altă versiune, ca să fie revăzută. Tot de la 7.0, o scriere a
cărei dată rămâne în urmă, cum e cea a unei copieri, sosește ca schimbare de
atribute; agentul o tratează ca modificare, la fel ca înainte. A doua corectură
scoate din ce cere watchdog schimbarea datei ultimului acces: citirea
amprentării o producea, iar în măsurătoarea 2.5 fiecare fișier a fost raportat
de două ori, cu aceeași amprentă. O scriere schimbă data scrierii sau mărimea,
care rămân cerute, deci nu se pierde nicio modificare de conținut.

Aceeași metodă a curățat payload-ul: măsurarea liniei de bază a arătat că cel mai
scump câmp trimis la fiecare fișier, `description` — 16,7% din payload-ul unui
eveniment —, nu conținea nicio informație nouă. Scos, cu o gardă care îl
împiedică să revină, a scăzut metadatele totale cu 11%.

**Comentariile explică ce s-ar strica altfel.** Multe docstring-uri conțin lanțul
de eșec concret pe care linia respectivă îl previne, nu o parafrazare a codului.

**Verificare manuală pe două mașini** — server într-un VM, agent pe gazdă — cu
un plan de unsprezece scenarii: chei greșite, identități împrumutate, revocări,
restarturi de server, payload-uri invalide injectate direct în coadă.

La 18 noiembrie 2026: 21 de issue-uri închise și 2 deschise.

---

## Contribuții proprii

Partea de agent a lucrării. Ce e original, față de ce e aplicare de tehnică
cunoscută:

1. **Garanția că o amprentă descrie întotdeauna un fișier finit.** Stabilizare
   cu verificare dublă și plafon de reintroduceri, iar la depășire raportare
   fără amprentă în loc de o amprentă falsă. E precondiția pe care se sprijină
   tot protocolul: dacă hash-urile pot descrie stări inexistente, prevalența
   calculată pe ele nu înseamnă nimic.

2. **Conservarea dovezilor ridicată la rang de invariantă.** Clasificarea
   erorilor după cauză, nu după familia codului HTTP, derivată explicit din
   observația că altfel un atacator ar putea folosi eșecul de autentificare ca
   mecanism de ștergere.

3. **Un agent care nu tace niciodată.** Nicio stare de eroare nu oprește
   colectarea locală; toate produc reîncercare cu escaladare vizibilă.

4. **Escaladare pe durată, nu pe număr de încercări**, tocmai pentru că
   backoff-ul face contorul de încercări să nu mai descrie timpul.

5. **Identitate derivată o dată și înghețată local**, ceea ce face posibilă
   desfășurarea pe un parc dintr-o singură imagine, fără pas manual per mașină.

6. **Separarea costului impus de obiectul observat de cel impus de observator**
   (`unstable` față de `skipped_capacity` / `skipped_shutdown`), fără de care
   metrica de divulgare n-ar putea spune care parte a golului aparține metodei.

---

## Future work

**Treapta următoare a protocolului.** Astăzi agentul trimite amprente
*întotdeauna*. Treapta următoare înseamnă să răspundă la o cerere de escaladare:
serverul constată că un fișier e necunoscut și cere ceva în plus despre el.
Serverul decide deja, în banda minimă, care fișiere ar escalada, dar nu cere
nimic, iar canalul există doar ca formă — `collect_file_path` în directiva de
heartbeat, pe care agentul nu o tratează.

**Reducerea numărului de evenimente.** Payload-ul a fost curățat cu 11% prin
eliminarea unui câmp derivat. Pârghia rămasă nu mai e dimensiunea unui eveniment,
ci câte se emit: un fișier scris de zece ori produce azi zece evenimente cu zece
amprente identice. Serverul nu le numără ca zece mașini și nu le escaladează de
zece ori, dar fiecare costă un plic pe fir.

**Fișierele deja existente.** La pornire, ca și la repunerea unui director sub
observer, agentul vede doar schimbările de acum încolo; ce era deja în director
nu se raportează. Scanarea inițială e un pas viitor.

**Ștergerile.** Agentul raportează crearea, modificarea și mutarea în zonă, dar
nu ștergerea sau mutarea în afara ei.

**TLS.** Cheia de API circulă în clar. Partea de agent nu e doar schimbarea
schemei în URL: `requests` validează împotriva pachetului `certifi`, nu a
magazinului de certificate al Windows-ului, deci un CA intern instalat prin
politică de grup nu ar fi văzut. Iar un `SSLError` cade azi în eroarea de
transport generică, adică e reîncercat ca o pană de rețea, deși un certificat
invalid e ori configurare greșită, ori interpunere (edr-agent#15).

**Plafonul cozii sub eșec prelungit de autentificare.** Coada crește deliberat
nelimitat, dar plafonul general rămâne activ și elimină cele mai vechi intrări —
adică exact începutul unui incident (edr-agent#17).

**Debitul la cozi foarte adânci.** După reparația plafonului de hashing, la zece
mii de intrări debitul scade de la ~170 la ~106 fișiere pe secundă: tracker-ul
iterează peste toate intrările la fiecare trecere a releaser-ului.

Problemele deschise: [issues](https://github.com/ZiGabiZi/edr-agent/issues).
