# edr-agent

Agentul care rulează pe endpoint și decide **ce anume merită să părăsească
mașina**.

Trimite amprente SHA-256 și metadate, niciodată conținut de fișier. Pe măsurarea
de referință — 444 de fișiere, 143,1 MiB — asta a însemnat 264 KB în loc de 143
MB, adică de aproximativ 567 de ori mai puțin. Cifra e calculată de
[edr-server](https://github.com/ZiGabiZi/edr-server), care primește datele și
contabilizează raportul.

Lucrare de licență.

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

Testele — **198**, fără rețea și fără server:

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
rafală. Măsurătoarea a arătat 10.000 din 10.000 livrate la 1577 de scrieri pe
secundă — iar pierderea era în întregime internă: un plafon de zece fișiere pe
secundă într-o buclă proprie, care după reparație a devenit ~170. Issue-ul a fost
rescris cu cifrele, cu ipoteza inițială păstrată vizibil.

Aceeași metodă a curățat payload-ul: măsurarea liniei de bază a arătat că cel mai
scump câmp trimis la fiecare fișier — 16% din trafic — nu conținea nicio
informație nouă. Scos, cu o gardă care îl împiedică să revină.

**Comentariile explică ce s-ar strica altfel.** Multe docstring-uri conțin lanțul
de eșec concret pe care linia respectivă îl previne, nu o parafrazare a codului.

**Verificare manuală pe două mașini** — server într-un VM, agent pe gazdă — cu
un plan de unsprezece scenarii: chei greșite, identități împrumutate, revocări,
restarturi de server, payload-uri invalide injectate direct în coadă.

70 de commituri, 15 issue-uri închise.

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
serverul constată că un fișier e necunoscut și cere conținutul lui. Canalul
există deja ca formă — `collect_file_path` în directiva de heartbeat — dar
agentul nu îl tratează.

**Reducerea numărului de evenimente.** Payload-ul a fost curățat cu 11% prin
eliminarea unui câmp derivat. Pârghia rămasă nu mai e dimensiunea unui eveniment,
ci câte se emit: un fișier scris de zece ori produce azi zece evenimente cu zece
amprente identice.

**TLS.** Cheia de API circulă în clar. Partea de agent nu e doar schimbarea
schemei în URL: `requests` validează împotriva pachetului `certifi`, nu a
magazinului de certificate al Windows-ului, deci un CA intern instalat prin
politică de grup nu ar fi văzut. Iar un `SSLError` cade azi în eroarea de
transport generică, adică e reîncercat ca o pană de rețea, deși un certificat
invalid e ori configurare greșită, ori interpunere.

**Plafonul cozii sub eșec prelungit de autentificare.** Coada crește deliberat
nelimitat, dar plafonul general rămâne activ și elimină cele mai vechi intrări —
adică exact începutul unui incident.

**Debitul la cozi foarte adânci.** După reparația plafonului de hashing, la zece
mii de intrări debitul scade de la ~170 la ~106 fișiere pe secundă: tracker-ul
iterează peste toate intrările la fiecare trecere a releaser-ului.

Problemele deschise: [issues](https://github.com/ZiGabiZi/edr-agent/issues).
