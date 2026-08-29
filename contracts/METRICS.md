# Metricile — definițiile operaționale

Document valabil pentru ambele repo-uri (`edr-agent`, `edr-server`). Cele două
exemplare trebuie să fie identice; sincronizarea lor se verifică la fel ca la
`AUTH.md`.

De ce e un contract și nu o notă de implementare: serverul calculează metricile,
agentul furnizează intrările. O definiție care diferă între cele două produce
cifre care par corecte și nu descriu nimic. Documentul acesta e sursa
autoritativă; `app/services/disclosure_metrics.py` e implementarea.

**Regula de aur:** o metrică se definește înainte să existe mecanismul pe care
îl măsoară. Precedentul e `file_size`, impus prin contract cu doi pași înainte
să existe hashing — și singurul motiv pentru care raportul de divulgare se poate
calcula azi. O metrică definită după ce mecanismul funcționează măsoară ce e
ușor de măsurat, nu ce trebuia.

---

## 1. Octetul divulgat

**Definiție.** Un octet e divulgat dacă a părăsit endpoint-ul pe canalul
agent→server, indiferent dacă a ajuns, dacă a fost acceptat sau dacă a mai fost
trimis o dată.

Definiția vine din §2.2, modelul de amenințări: datele care părăsesc
endpoint-ul fac parte din suprafața de atac. Un octet retransmis a părăsit
endpoint-ul de două ori, deci se numără de două ori.

### 1.1 Ce se numără

| Categorie | Se numără | Observație |
|---|---|---|
| Metadate de eveniment de fișier | da | plicul: identificatori, cale, amprentă, măsurători |
| Metadate de eveniment de ciclu de viață | da | `agent_startup`, `_shutdown`, `_restart` — aceeași rețea |
| Conținut divulgat la T2 | da | regiuni cerute de server |
| Conținut divulgat la T3 | da | fișierul integral |
| Retransmisii | da | vezi §1.3 |
| Canalul de control (heartbeat) | separat | vezi §1.4 |

### 1.2 Ce nu se numără, și de ce

**Anteturile HTTP și cadrele TCP/TLS.** Sunt cost de transport, nu de protocol,
și sunt identice sub ambele politici. Includerea lor ar adăuga aceeași constantă
în numărător și numitor.

**Traficul server→agent.** Rulesetul coborât, directivele, verdictele. Ele merg
în direcția opusă și nu ating suprafața de atac descrisă în §2.2. Costul lor se
raportează separat, la §2.7.

**Fișierele pe care agentul le-a citit local.** Un fișier de 200 MB hash-uit pe
endpoint costă I/O, nu divulgare. Costul acela e o măsurătoare, nu un octet
divulgat, și trăiește la §4.3.

### 1.3 Retransmisiile

Coada de evenimente are semantică at-least-once, deci un eveniment poate pleca
de mai multe ori. Se numără fiecare plecare.

Comparația rămâne curată pentru că linia de bază e un **mod al aceluiași cod**,
nu o implementare separată: retransmite identic, prin același spool și același
dispecer. Efectul se anulează între numărător și numitor, deci simetria e
gratuită, iar afirmația de confidențialitate rămâne exactă.

### 1.4 Canalul de control

Heartbeat-urile sunt proporționale cu **timpul**, nu cu numărul de fișiere. Se
raportează pe o linie separată, ca **prag**, nu topite în totalul de divulgare.

Consecința e importantă pentru afirmația principală și se declară aici, nu se
descoperă la evaluare: **costul per endpoint nu poate coborî sub podeaua
canalului de control**, oricât ar crește parcul. Curba din §3.1 are o asimptotă
pe care nu o pune analiza, ci telemetria. Valoarea ei se estimează pe hârtie
înainte de măsurătoare.

---

## 2. Numitorul: modelul always-upload

**Definiție.** Ce ar fi părăsit endpoint-ul dacă fiecare fișier observat ar fi
fost urcat integral pentru analiză pe server.

**Numitorul include plicul, nu doar conținutul.** Un sistem always-upload nu
trimite fișiere goale: trimite fișierul *plus* aceleași metadate de identificare.
Numitorul e deci `conținut + plic`, cu același plic ca la numărător.

Corecția merge aparent în defavoarea protocolului — raportul devine
`plic / (plic + conținut)` în loc de `plic / conținut` — și tocmai de aceea
apără afirmația: nimeni nu poate susține că numitorul a fost umflat.

**Numitorul e definit doar pentru fișierele cu dimensiune cunoscută**, adică
`hash_status == "ok"`. Restul intră la §2.1.

### 2.1 Golul declarat

Fișierele cu `hash_status` diferit de `ok` nu au dimensiune cunoscută, deci nu
contribuie la numitor, dar metadatele lor au plecat și contribuie la numărător.
Efectul e o **subestimare** a economiei.

Golul se raportează întotdeauna, defalcat pe status: `unstable`, `unreadable`,
`too_large`, `vanished`, `skipped_capacity`, `skipped_shutdown`. Un raport de
divulgare fără el e un raport care ascunde câte fișiere n-a putut măsura.

**Golul de atribuire, a doua categorie.** Pe lângă fișierele fără dimensiune
cunoscută, se raportează separat evenimentele care au divulgat ceva fără să
poată fi puse pe o treaptă:

- `file_events_without_tier` — evenimente de fișier fără bloc `disclosure` sau
  fără `tier` în el. Un agent de dinainte de v5 produce exact acest caz.
- `disclosure_outside_file_events` — blocuri apărute pe evenimente care nu sunt
  de fișier, adică încălcări ale bicondiționalității `disclosure ⟺ file_path`.
  Numărul ar trebui să fie zero; raportat, încetează să fie o presupunere.
- `content_bytes_without_tier` — octeții de conținut din cele două categorii de
  mai sus.

Cele două goluri se raportează **separat**, nu contopite, din același motiv
pentru care statusurile de hash se țin distincte: un fișier hash-uit cu succes
dar fără treaptă și unul pe care hashing-ul l-a ratat spun ce mecanism diferit
a eșuat, iar amestecate n-ar mai spune niciunul.

Octeții din golul de atribuire intră **întotdeauna în numărător**. Un octet care
a părăsit endpoint-ul a plecat indiferent dacă știm cărei trepte să-l atribuim;
scos din numărător pentru că atribuirea lipsește, ar deplasa cifra exact în
direcția care flatează afirmația.

---

## 3. Eficiența

### 3.1 Principală — costul marginal per endpoint

**Definiție.** Octeții divulgați de al `N`-lea endpoint adăugat parcului, cu
aceeași sarcină de lucru, măsurați după stabilizarea lui.

E metrica ce poartă afirmația principală din §2.4.

**De ce marginalul și nu media.** Media scade și dacă protocolul n-ar face nimic
inteligent — un cost fix împărțit la mai mulți scade prin aritmetică, iar
obiecția e legitimă. Marginalul nu se poate explica altfel: al `N`-lea endpoint
trimite mai puțin decât primul **numai** pentru că serverul știe deja ce sunt
fișierele. Deduplicarea și prevalența nu au unde să se ascundă.

**Constrângere de montaj**, obligatorie la §2.11: endpoint-urile se adaugă
eșalonat, cu ordinea fixată înainte de măsurătoare. Pornite simultan, marginalul
nu se poate atribui.

**Predicție verificabilă.** Marginalul are o limită inferioară calculabilă pe
hârtie: podeaua canalului de control din §1.4, plus un eveniment de fișier
pentru fiecare fișier unic pe acel endpoint. Se estimează înainte de măsurătoare
și se compară după.

### 3.2 De sprijin — distribuția raportului per fișier

**Definiție.** Pentru fiecare fișier, `octeți divulgați / octeți sub
always-upload`. Se raportează **mediana** și **p95** peste corpus.

**De ce distribuția și nu agregatul.** Raportul agregat descrie în bună măsură
dimensiunea fișierelor din corpus, nu comportamentul protocolului: se schimbă
dacă schimbi directorul monitorizat, fără să atingi protocolul. O metrică pe care
o poți muta cu un ISO nu poate purta o afirmație.

**P95 contează cel puțin cât mediana.** E singurul loc în care apare predicția
din §2.4 — că sub un prag de dimensiune protocolul transferă *mai mult* decât
always-upload, fiindcă plicul plus vectorul de trăsături depășesc fișierul
însuși. Fișierele mici se adună în coada distribuției. Raportată doar mediana,
predicția proprie ar fi ascunsă exact acolo unde se verifică.

### 3.3 Secundare

**Media per endpoint** — total divulgat împărțit la numărul de agenți. Se
înțelege dintr-o privire, e utilă în rezumat, dar nu poartă afirmația.

**Raportul agregat** — suma peste sumă. Se raportează **întotdeauna însoțit de
mențiunea că depinde de compoziția de dimensiuni a corpusului**. Fără mențiune,
cifra sugerează o proprietate a protocolului pe care nu o are.

### 3.4 Atribuirea pe trepte

Pentru fiecare treaptă `T0`–`T3`: procentul de verdicte închise acolo, octeții
divulgați cumulat până acolo, și latența până la verdict.

**Numitorul tabelului este mulțimea evenimentelor care poartă o treaptă**, nu
mulțimea tuturor evenimentelor. Evenimentele de ciclu de viață (`agent_startup`,
`agent_shutdown`, `agent_restart`) nu sunt pe scara de divulgare prin
proiectare: scara `T0`–`T3` descrie cât s-a divulgat *dintr-un fișier*, iar un
eveniment de pornire nu divulgă un fișier, ci starea agentului. Ele nu apar în
tabel și nu îi umflă numitorul.

Fără declarația de mai sus, un procent citit ca „`T0` închide 100%" ar putea fi
înțeles ca fiind peste toate evenimentele. Orice tabel pe trepte se publică
împreună cu numitorul lui, `events_with_tier`.

Evenimentele de fișier care ar fi trebuit să poarte o treaptă și nu o poartă nu
dispar din raport: intră în golul de atribuire de la §2.1.

Se raportează chiar și când o singură treaptă există. Un tabel în care `T0`
închide 100% e o observație corectă despre stadiul de atunci, nu un rând gol.

---

## 4. Fidelitatea față de oracol

### 4.1 Oracolul

**Definiție.** Verdictul produs de **același motor de analiză** alimentat cu
fișierul integral, adică sistemul rulat în modul `always_upload`.

Oracolul **nu** e eticheta reală. Poate greși. Tocmai de aceea afirmația din
§2.4 e independentă de calitatea rulesetului: orice divergență e, prin
construcție, o eroare de calibrare a benzii — adică obiectul lucrării.

Comparația se face **pe amprentă**, nu pe cale: verdictele sunt legate de
conținut, deci același fișier văzut pe endpoint-uri diferite are un singur
verdict de comparat.

### 4.2 Categoriile

Pentru fiecare fișier din corpus, exact una dintre:

| Categorie | Înțeles |
|---|---|
| `acord` | protocolul și oracolul spun același lucru |
| `ratare` | oracolul spune malițios, protocolul spune curat |
| `alarmă` | oracolul spune curat, protocolul spune malițios |
| `abținere` | protocolul a epuizat plafonul de divulgare fără verdict |

**Divergența nu se raportează niciodată ca scalar.** „3% divergență" ascunde
dacă e vorba de malware ratat sau de timp de analist irosit, iar cele două nu se
compară. Funcția de cost din §2.6 le ponderează asimetric; metrica trebuie să
poată alimenta acea pondere.

**Abținerea are coloană proprie.** Contopită cu `ratare`, umflă divergența
artificial; ignorată, ascunde cazuri pe care protocolul nu le acoperă. Rata de
abținere e ea însăși o metrică: un protocol care spune „nu știu" la o fracțiune
mare din corpus nu e util, oricât de curată ar fi divergența pe restul.

### 4.3 Regiunea utilizabilă a frontierei

Baleiajul parametrului de cost trasează o curbă: octeți divulgați față de
divergență.

**Regiunea utilizabilă e zona cu zero `ratare`.** Pragul e un număr, nu un
procent: un procent ar fi ales după ce se văd datele, iar la scara acestui
corpus diferența dintre 0,5% și 1,5% e sub zgomotul măsurătorii.

Restul curbei se raportează ca informație — „dincolo de acest punct, fiecare
procent suplimentar de economie costă `N` ratări" — marcat explicit ca fiind în
afara regiunii utilizabile.

**Marginea superioară se raportează întotdeauna.** Zero ratări pe un corpus
finit nu înseamnă zero ratări în general: la zero eșecuri în `N` încercări, rata
reală se poate susține doar sub aproximativ `3/N`. Formularea corectă e „nicio
ratare observată pe `N` mostre malițioase, ceea ce mărginește rata sub `X`%".

Consecință directă asupra corpusului, la §1.6: pentru „sub 1%" sunt necesare cel
puțin **300 de mostre malițioase**. Sub 100, nu se poate susține nimic util
despre ratări, oricât de curat ar ieși rezultatul.

---

## 5. Context — nu afirmație

Rata de detecție și rata de alarme false față de **etichetele reale**, sub
ambele politici.

Se raportează pentru că întrebarea „dar detectează malware?" trebuie să aibă un
răspuns. Nu poartă afirmația: ele depind de calitatea rulesetului, unde lucrarea
nu concurează și nu pretinde nimic.

Poziția e explicită: **teza susține că protocolul reproduce verdictele
oracolului la o fracțiune din octeți, nu că oracolul e bun.**

---

## 6. Costul observației, separat de divulgare

`measurements` poartă ce a costat *observarea*; divulgarea poartă ce a *părăsit*
endpoint-ul. Separarea e impusă structural în contractul de fir, nu prin
convenție, și se păstrează și aici.

| Măsurătoare | Ce descrie |
|---|---|
| `settle_wait_ms` | latența introdusă de detecția stabilizării |
| `hash_duration_ms` | costul de citire al treptei T0 |
| latență per treaptă | timpul până la verdict, pe fiecare treaptă |

Niciuna nu e octet divulgat. Toate sunt intrări în funcția de cost din §2.6 —
motiv pentru care se instrumentează odată cu treapta pe care o măsoară, nu la
evaluare.

---

## 7. Precizia numărătorului

Numărătorul trebuie să fie **măsurat, nu estimat**.

Agentul raportează câți octeți a pus efectiv pe fir; serverul îi compară cu ce a
primit. Aproximarea prin reserializarea evenimentului stocat include câmpuri
adăugate de server și exclude anteturile — acceptabilă pentru un ordin de
mărime, insuficientă pentru numărătorul afirmației centrale.

**Discrepanța agent↔server se raportează ca metrică proprie.** O diferență
persistentă înseamnă fie un bug, fie un canal necontabilizat. Reconcilierea nu e
o verificare internă, e o proprietate verificabilă a măsurătorii — aceeași
logică prin care contractul de fir e testat, nu presupus.

### 7.1 Anteturile de raportare

| Antet | Ce poartă |
|---|---|
| `X-Agent-Wire-Attempted-Bytes` | octeți predați transportului de la începutul încarnării |
| `X-Agent-Wire-Delivered-Bytes` | octeți pentru care a venit orice răspuns HTTP |
| `X-Agent-Instance-Id` | încarnarea căreia îi aparțin cifrele de mai sus |

Primele două **exclud cererea în care sunt trimise**. Ca să declare dimensiunea
cererii curente, agentul ar trebui să pună acea dimensiune într-un antet al
aceleiași cereri — dar antetul face parte din cerere, deci îi schimbă
dimensiunea. Decalajul cu un mesaj rupe circularitatea fără nicio aproximare:
fiecare cifră descrie mesaje deja încheiate.

Sunt anteturi, nu câmpuri în corp, din același motiv pentru care cheia de agent
e antet (`AUTH.md` §1): descriu relația agent-server, nu evenimentul. Un câmp în
corp ar fi trebuit adăugat în fiecare schemă și validat de server, adică un
payload respins pentru contabilitate greșită. De aceea numele stau aici, nu în
`wire-contract.json`, care descrie corpuri.

Valoarea zero se trimite explicit. Prima cerere a unei încarnări poartă două
zerouri, iar asta e o afirmație verificabilă — un antet lipsă nu se distinge de
un agent vechi, de un intermediar care taie anteturi, sau de un bug.

**Încarnarea e antet pentru că serverul contabilizează în middleware**, adică
înainte de rutare, ca să nu piardă cererile respinse cu 401 — exact traficul
care contează cel mai mult. Un middleware nu poate citi corpul fără să consume
fluxul așteptat de ruta de după el, deci `agent_instance_id` trebuie să existe
și în afara corpului. Fără el, contoarele care repornesc de la zero la fiecare
pornire a agentului ar arăta ca direcția gravă din §7.2, la fiecare restart.

Agentul citește valoarea din payload-ul care o poartă oricum, ca antetul și
corpul să nu poată spune lucruri diferite.

**Excepția e înregistrarea**, iar ea merită citită cu atenție, pentru că arată
de ce antetul nu e doar o comoditate. `wire-contract.json` **interzice**
`agent_instance_id` în corpul înregistrării: la repornire, reînregistrarea
rulează înaintea primului heartbeat, iar `register_agent()` face `update()`
peste înregistrarea existentă, deci o încarnare venită prin corp ar suprascrie
baseline-ul înainte ca heartbeat-ul să-l poată compara — și `restart_detected`
n-ar mai fi True niciodată.

Interdicția e însă pe **corp**, nu pe cerere. Antetul nu trece prin schema
serverului, nu ajunge în `agents_store` și nu atinge baseline-ul; e citit doar
de contabilizare. Deci încarnarea pleacă și la înregistrare, pe antet, iar
agentul o primește ca parametru explicit — singurul loc unde sursa antetului nu
e payload-ul.

**Ce rămâne neatribuibil, și de ce e acceptabil.** La *prima* înrolare a unei
mașini nu există încă o cheie de agent: autentificarea se face cu secretul de
înrolare, iar `agent_id` trăiește doar în corp, pe care middleware-ul nu-l
citește. Octeții aceia rămân în găleata `no_key`. Atribuirea lor ar cere ca
serverul să creadă un `agent_id` nedovedit, ceea ce e exact ce refuză
`require_identity_match` peste tot altundeva — un endpoint ar putea umfla
cifrele altuia.

Diferența față de situația de dinainte nu e cosmetică: golul era **un mesaj la
fiecare încarnare**, adică la fiecare repornire, pentru totdeauna. Acum e **un
mesaj o singură dată în viața unei mașini**, în prima ei încarnare. Toate
încarnările următoare se încadrează exact.

Totalul e peste canale, deși §1.4 le ține separate local. Serverul vede corpuri
și rute, nu categoriile agentului; reconcilierea are nevoie de un singur număr
comparabil, iar separarea rămâne pentru afirmațiile din §3.

### 7.2 Proprietatea de încadrare

Cele două anteturi nu sunt redundante: împreună dau o **încadrare**, nu o
singură cifră de comparat. Pentru octeții primiți de server de la o încarnare,
socotiți înaintea cererii curente:

```
delivered_raportat  ≤  primit_de_server  ≤  attempted_raportat
```

Marginea de jos ține pentru că un mesaj cu răspuns a fost în mod necesar primit.
Marginea de sus ține pentru că serverul nu poate primi mai mult decât a plecat.

Fiecare margine spartă înseamnă altceva, iar direcțiile **nu** se topesc într-o
diferență absolută:

- **sub marginea de jos** — serverul a primit mai puțin decât știe agentul că a
  fost livrat. Contabilitate stricată de o parte sau de alta.
- **peste marginea de sus** — serverul a primit mai mult decât a trimis agentul.
  Ori cineva trimite în numele lui, ori există un canal pe care agentul nu-l
  contabilizează. Prima e o problemă de securitate, a doua invalidează
  numărătorul afirmației centrale. E direcția care trebuie să fie mult mai
  sensibilă.

### 7.3 Toleranța

Încadrarea e exactă doar dacă nimic nu e în zbor. Toleranța **nu** e „un mesaj",
ci **câte mesaje pot fi simultan în zbor** — azi dispecerul plus bucla de
heartbeat. Două mesaje pot pleca într-o ordine și ajunge în alta, deci decalajul
apare în ambele direcții și nu se poate elimina făcând fotografia contorului
atomică cu incrementarea.

Pragul se declară deci relativ, nu absolut: peste o dimensiune tipică de mesaj
(altfel e doar traficul în zbor) **și** peste o fracțiune din totalul livrat de
acea încarnare (altfel e zgomotos pentru un agent liniștit și orb pentru unul
care trimite gigaocteți). Dimensiunea tipică se calculează din contoarele de
mesaje, nu se ghicește.

### 7.4 Ce raportează reconcilierea

`GET /api/metrics/disclosure` poartă o secțiune `reconciliation`. Ea nu descrie
divulgarea, ci **dacă cifra de divulgare poate fi crezută** — de aceea stă
alături de metrică, nu topită în ea.

Fiecare încarnare primește un verdict, nu o diferență:

| Verdict | Ce înseamnă |
|---|---|
| `within_bounds` | încadrarea din §7.2 se respectă |
| `below_lower_bound` | serverul a primit mai puțin decât știe agentul că i-a fost livrat — contabilitate stricată de o parte sau de alta |
| `above_upper_bound` | serverul a primit mai mult decât a trimis agentul — trafic în numele lui, sau un canal necontabilizat |
| `unreported` | agentul n-a trimis încă un raport lizibil; stare de tranziție, nu violare |

`unreported` există separat pentru că un agent vechi, sau unul aflat la prima
cerere, nu e o problemă. Contopit cu violările, ar umple raportul cu zgomot
exact în timpul unei actualizări de parc — adică fix când cineva chiar se uită.

Diferențele se raportează **semnate**, cu numele scrise ca aritmetica lor:
`delivered_over_received` și `received_over_attempted`. Pozitiv înseamnă margine
ruptă. Nu există o „diferență absolută": ar face cele două direcții să arate
identic, iar ele nu sunt deloc la fel de grave.

Pe lângă ele se raportează `reported_undelivered_bytes` — ce a plecat de pe
endpoint fără să primească vreun răspuns, după propriile contoare ale agentului.
Nu e o discrepanță; e volumul plecat în gol, și explică o parte din jocul dintre
margini.

**Găleata de neatribuibil**, cu motivul păstrat:

| Motiv | Ce s-a întâmplat |
|---|---|
| `no_key` | cerere fără antet de cheie — tipic, o înrolare |
| `unknown_key` | cheie prezentă, nerecunoscută — traficul care nu ajunge la nicio rută |
| `no_instance` | agent cunoscut, încarnare nedeclarată — azi, re-înregistrarea |
| `unsized` | corp fără `Content-Length`; dimensiunea e **necunoscută, nu zero** |

Motivele nu se contopesc. `unknown_key` e un agent cu credențiale stricate care
divulgă tot ce trimite fără să scrie nimic nicăieri; `no_instance` e o
consecință cunoscută a payload-ului de înregistrare. Într-un singur număr,
niciunul n-ar mai fi lizibil.

Găleata apare **întreagă și când raportul e filtrat pe un agent**. Octeții de
acolo n-au proprietar prin definiție: dacă s-ar putea filtra pe agent, n-ar mai
fi neatribuibili. Un raport filtrat care i-ar ascunde ar sugera că pentru
agentul acela nu există trafic necontabilizat, ceea ce nu se poate ști.

### 7.5 Stadiul de azi

- `1.3a` — *livrat.* Blocul `disclosure` cu treapta și octeții ei de conținut,
  declarat, validat pe ambele părți și atribuit în tabelul de la §3.4.
- `1.3b` — *parțial.* Livrat: serializarea explicită în transportul agentului,
  registrul de fir pe canale (`services/wire_ledger.py`), anteturile de la §7.1,
  contabilizarea pe server în middleware (`app/wire_middleware.py`,
  `app/services/wire_accounting.py`) și reconcilierea de la §7.4.

  Rămân deschise **două** lucruri, iar primul e cel care poartă titlul acestei
  secțiuni:

  1. **Numărătorul publicat e încă estimatul.** Cifra măsurată există, dar stă
     alături, în `reconciliation`. Secțiunile `progressive`, `always_upload` și
     `ratio` vin în continuare din `disclosure_metrics.py::_payload_bytes`, iar
     factorul de calibrare din §7.6 — totalul măsurat față de suma estimărilor —
     nu se calculează nicăieri. Până atunci, cerința „măsurat, nu estimat" e
     pregătită, nu îndeplinită.
  2. **Alarma** la discrepanță persistentă.

**Reziduul cunoscut.** Prima înrolare a unei mașini rămâne neatribuibilă, din
motivul explicat la §7.1: nu există încă o cheie care să dovedească identitatea,
iar `agent_id` trăiește doar în corp. Agentul numără acei octeți în totalul
raportat, serverul îi pune în `no_key`, deci marginea de jos din §7.2 rămâne
ruptă cu mărimea unei înrolări — **o singură dată, în prima încarnare a
mașinii**.

Formularea de dinainte a acestei secțiuni descria același gol la **fiecare**
încarnare, pentru că înregistrarea nu purta deloc încarnarea (edr-agent#19).
Diferența contează pentru calibrarea alarmei: un mesaj o dată în viața unei
mașini stă sub condiția „peste o dimensiune tipică de mesaj" din §7.3, deci nu
cere prag lărgit. Un mesaj la fiecare repornire ar fi cerut.

Numărătorul publicat rămâne aproximarea din
`disclosure_metrics.py::_payload_bytes`: reserializarea evenimentului stocat,
care include câmpuri adăugate de server și exclude anteturile HTTP. Cifra
măsurată există acum lângă ea, în `reconciliation`, dar până când cele două se
închid una peste alta orice cifră publicată din estimator se declară ca
**estimare**, nu ca măsurătoare — vezi §8.

### 7.6 Ce rămâne estimat chiar și după 1.3b

Registrul dă un total exact pe încarnare, nu octeți per eveniment. Distribuția
per fișier cerută de §3.2 — mediană, p95 — are nevoie de o valoare pentru
fiecare mesaj în parte, iar singurul mod de a o avea în mesaj ar fi să pui
dimensiunea mesajului în mesajul însuși. Aceeași circularitate ca la §7.1, dar
fără decalaj care s-o rupă: acolo raportăm mesaje încheiate, aici ar trebui
raportat mesajul curent.

Asimetria se declară, nu se ascunde:

- numărătorul **agregat** e măsurat;
- cifrele **per fișier** rămân estimate prin reserializare.

Rezultatul util e că totalul măsurat, comparat cu suma estimărilor, dă eroarea
estimatorului. Cu factorul acela, cifrele per fișier se pot mărgini onest:
„mediana e X, cu un estimator care subestimează cu Y% în agregat". Registrul nu
înlocuiește estimatorul — îl **calibrează**.

---

## 8. Ce trebuie declarat lângă orice cifră

O metrică publicată fără contextul de mai jos e o cifră care poate fi citită
greșit, iar responsabilitatea e a celui care a publicat-o.

- corpusul: câte fișiere, ce distribuție de dimensiuni, ce fracțiune malițioasă
- suprapunerea între endpoint-uri, când cifra e per parc
- golul din §2.1, pe statusuri
- politica activă și calibrarea parametrului de cost
- pentru afirmații despre rate: marginea superioară din §4.3
