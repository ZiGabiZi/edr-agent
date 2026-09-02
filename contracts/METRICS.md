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

**Convenția de referințe.** `§N` fără altă calificare trimite întotdeauna la o
secțiune a **acestui** document. O secțiune din alt contract se scrie calificat,
`CORPUS.md §5.4`. O secțiune a **lucrării** poartă prefixul `L`: `§L2.4`.
Fără prefix, `§2.1` ar însemna două lucruri diferite în aceeași frază — iar
numerotarea lucrării se mută la fiecare secțiune inserată, a celui de aici nu.

---

## 1. Octetul divulgat

**Definiție.** Un octet e divulgat dacă a părăsit endpoint-ul pe canalul
agent→server, indiferent dacă a ajuns, dacă a fost acceptat sau dacă a mai fost
trimis o dată.

Definiția vine din §L2.2, modelul de amenințări: datele care părăsesc
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
în direcția opusă și nu ating suprafața de atac descrisă în §L2.2. Costul lor se
raportează separat, la §L2.8.

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

E metrica ce poartă afirmația principală din §L2.4.

**De ce marginalul și nu media.** Media scade și dacă protocolul n-ar face nimic
inteligent — un cost fix împărțit la mai mulți scade prin aritmetică, iar
obiecția e legitimă. Marginalul nu se poate explica altfel: al `N`-lea endpoint
trimite mai puțin decât primul **numai** pentru că serverul știe deja ce sunt
fișierele. Deduplicarea și prevalența nu au unde să se ascundă.

**Constrângere de montaj**, obligatorie la §L2.12: endpoint-urile se adaugă
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
din §L2.4 — că sub un prag de dimensiune protocolul transferă *mai mult* decât
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

**„Închis" înseamnă aici treapta declarată de AGENT**, adică axa divulgării: cât
a plecat din fișier până la verdict. Din P2.3 serverul conchide și el ceva la
`T0` — dispoziția de reputație de la §3.5 — iar cele două nu sunt același lucru
și nu se adună. Un eveniment care poartă treapta `T0` și primește dispoziția
`unknown` a divulgat la T0 fără să se închidă acolo. Până la P2.3 fraza de mai
sus n-avea niciun referent, fiindcă nimic nu măsura închiderea; de atunci are
doi, iar orice cifră publicată sub numele „închis la T0" trebuie să spună care
dintre cele două axe o produce.

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

### 3.5 Dispoziția de la T0

**Definiție.** Pentru fiecare valoare a dispoziției de reputație, câte evenimente
ale rulării au primit-o. E ce a conchis **serverul** consultând depozitul, nu ce
a declarat agentul că a divulgat.

**Numitorul e `events_with_hash`** — evenimentele care poartă `sha256`, adică
exact acelea pentru care serverul avea ce căuta. Prin invarianta de la v3
(`sha256` prezent ⟺ `hash_status == 'ok'`), mulțimea e aceeași cu „evenimentele
cu hash reușit". Evenimentele de ciclu de viață și cele cu hash eșuat nu sunt în
ea, și nu ca excludere convenabilă: pentru ele n-a existat nicio interogare.

**Vocabularul are cinci valori**, și e o bijecție cu ce stochează depozitul —
cele patru celule ale 2×2-ului de la §8.1, plus indisponibilitatea:

| valoare | ce spune |
|---|---|
| `known_malicious` | dovadă externă de amenințare; poartă proveniența sursei |
| `known_software` | prezent într-o listă de software cunoscut; **nu** înseamnă curat |
| `both_axes` | prezent pe ambele axe; celula de suprapunere, care nu se colapsează |
| `unknown` | depozitul a fost întrebat și nu știe; e candidatul la treapta următoare |
| `reputation_unavailable` | depozitul **nu a putut fi întrebat** |

**Ultimele două nu au voie să fie contopite.** `unknown` e un răspuns cu
conținut — chiar cel care justifică escaladarea. `reputation_unavailable` spune
că nu s-a pus nicio întrebare. Adunate, o pană a instantaneului ar produce exact
cifra unui corpus complet nou, adică rezultatul cel mai favorabil afirmației
centrale, obținut dintr-o defecțiune. Un instantaneu lipsă ar imita perfect
brațul rece al ablației, fără nicio eroare vizibilă nicăieri.

Evenimentul se acceptă și atunci: refuzat cu 5xx, disponibilitatea telemetriei ar
fi cuplată de cea a reputației, iar coada at-least-once (§1.3) ar reîncerca la
nesfârșit un eveniment perfect valid.

**Nu se publică o rată de închidere.** Ar cere maparea *dispoziție → închis*, iar
aceea e decizia benzii de incertitudine (§L2.7), nu a unui tabel. Cazul care o
face nebanală e `known_software`: numărat ca închis, ar transforma apartenența la
o listă de software cunoscut în verdict de benignitate — exact ce interzice
`CORPUS.md` §5.4, doar că pe ușa din dos a unei metrici. Se publică deci
contoarele și numitorul; cine vrea rata o compune **declarând ce a numărat ca
închis**.

**Golul de atribuire, ca la §2.1.** Evenimentele care poartă hash dar n-au
dispoziție — scrise înainte ca serverul să consulte depozitul — se raportează
separat, ca `hashed_events_without_disposition`. Numărate ca `unknown`, o rulare
veche ar arăta ca un corpus complet nou; e aceeași confuzie ca mai sus, mutată pe
axa timpului. O valoare pe care vocabularul n-o cunoaște nu se absoarbe nici ea:
se numără sub numele ei.

**Ce se declară lângă cifră** (§8.1): amprenta instantaneului care a răspuns,
sursele consultate cu versiunile lor, și brațul ablației. Identitatea se
consemnează **pe rulare**, nu pe eveniment — o rulare vede exact un instantaneu,
fiindcă fișierul se deschide o dată per proces, `immutable=1` e promisiunea că nu
se schimbă dedesubt, iar o etichetă de rulare nu poate fi redeschisă. Repetată pe
fiecare eveniment, lista surselor ar fi aceeași repetiție pe care schema
depozitului o evită stocând sursa ca întreg.

Fără declarația asta cifra nu spune nimic: „61,5% necunoscut" e chiar diferența
dintre cele două brațe ale ablației, iar fără identitatea depozitului ar putea fi
oricare dintre ele.

### 3.6 Prevalența în parc

**Definiție.** Pentru fiecare conținut văzut în rulare, pe câte mașini distincte
îl știe parcul — publicat ca histogramă. E memoria PROPRIE a sistemului, spre
deosebire de §3.5, care descrie cunoașterea împrumutată dintr-un instantaneu
extern.

**Numitorul e `distinct_hashes` — CONȚINUTURI distincte, nu evenimente.** Un
fișier văzut pe cinci mașini produce cinci evenimente și un singur rând în
histogramă. E al treilea numitor al raportului, după `events_with_tier` (§3.4) și
`events_with_hash` (§3.5), iar cele trei nu se adună: un procent calculat peste
două dintre ele n-ar descrie niciuna.

**Histograma descrie starea de ACUM, nu valorile răspunse atunci.** Prevalența se
schimbă în timpul rulării, deci același fișier a primit `1` la primul endpoint și
`5` la ultimul. O distribuție peste răspunsuri ar descrie în bună parte ordinea
sosirii, nu parcul; iar starea finală e cea comparabilă cu proiectarea corpusului
(`CORPUS.md` §6), deci singura care poate fi infirmată.

Ce s-a răspuns atunci nu se pierde: se raportează separat, ca
`events_at_first_sighting` față de `events_with_prior_sighting` — câte evenimente
au sosit la un conținut pe care parcul îl vedea prima oară, față de unul pe care
îl știa deja.

**Nu se publică nicio economie.** „Câte escaladări a evitat parcul" cere să se
știe care evenimente ar fi escaladat, iar decizia aceea aparține benzii de
incertitudine (§L2.7). Tabelul numără; interpretarea rămâne a pasului care are
dreptul s-o facă.

**Registrul NU se amprentează.** Spre deosebire de instantaneul de reputație, se
schimbă în timpul rulării, deci o amprentă ar fi falsă înainte ca rularea să se
termine — ar arăta ca garanția de la §8.1 fără să o poată ține. Se declară în
schimb **poziția de plecare**: câte conținuturi și câte mașini erau în registru
când s-a deschis rularea, consemnate înainte de prima vedere a rulării. Registrul
e global peste rulări — memoria unui parc nu se golește când operatorul
redenumește experimentul — deci fără poziția de plecare două rulări cu aceleași
evenimente ar publica cifre incomparabile fără ca nimic să spună de ce.

**Golul de atribuire**, ca la §2.1: evenimentele care poartă hash dar n-au bloc
de prevalență — scrise înainte ca registrul să existe — se raportează ca
`hashed_events_without_prevalence`, niciodată contopite cu „prima vedere".

**Limitare declarată.** E prevalență *observată*, nu reală: numără mașinile care
au **raportat** conținutul, nu pe cele care îl au. Un endpoint oprit, unul cu
coada plină sau unul care n-a atins încă directorul monitorizat lipsesc din
număr, iar cifra e prin urmare o subestimare.

---

## 4. Fidelitatea față de oracol

### 4.1 Oracolul

**Definiție.** Verdictul produs de **același motor de analiză** alimentat cu
fișierul integral, adică sistemul rulat în modul `always_upload`.

**Unde rulează.** Modul `always_upload` rulează **pe endpoint**, nu pe server.
Rulesetul coboară deja (§L2.8), deci același motor poate fi alimentat cu fișierul
integral acolo unde fișierul se află; peste fir trece doar verdictul.

Precizarea nu e detaliu de implementare, e precondiție de montaj. Un oracol
calculat pe server ar cere ca fiecare fișier din corpus să ajungă acolo:
5 × 26 GB ≈ 130 GB către o mașină cu 66 GB liberi. Oracolul n-ar fi scump, ar fi
imposibil — iar imposibilitatea s-ar descoperi la prima rulare completă, nu aici.

Distincția decide de două ori dacă montajul există, și în sensuri opuse: numitorul
definit mai sus e contrafactual — „ce *ar fi* părăsit endpoint-ul" — deci nu
mișcă niciun octet; oracolul e o rulare adevărată și i-ar mișca pe toți.
Definiții care sună la fel, cost care diferă cu 130 GB.

Consecința pentru corpus: fișierele nu trebuie să fie rezidente simultan pe
endpoint — lotul în lucru îi ajunge oracolului la fel ca protocolului, fiindcă
amândouă citesc același fișier în aceeași trecere.

Oracolul **nu** e eticheta reală. Poate greși. Tocmai de aceea afirmația din
§L2.4 e independentă de calitatea rulesetului: orice divergență e, prin
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
compară. Funcția de cost din §L2.7 le ponderează asimetric; metrica trebuie să
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

Consecință directă asupra corpusului, la `CORPUS.md` §2.1: pentru „sub 1%" sunt necesare cel
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

Niciuna nu e octet divulgat. Toate sunt intrări în funcția de cost din §L2.7 —
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
care trimite gigaocteți). Cele două condiții trebuie îndeplinite **simultan**;
cu „sau" în loc de „și", fiecare ar anula apărarea celeilalte.

Valorile de azi:

| Parametru | Valoare | De ce |
|---|---|---|
| mesaje tolerate în zbor | 3 | dispecerul, bucla de heartbeat, plus decalajul de un mesaj al raportului |
| fracțiune, `below_lower_bound` | 5% | contabilitate stricată de o parte sau de alta |
| fracțiune, `above_upper_bound` | 1% | trafic în numele agentului sau canal necontabilizat |

Direcția gravă e de cinci ori mai sensibilă, deliberat.

**Ambele mărimi se calculează din măsurătorile serverului, nu din cifrele
raportate.** Dimensiunea tipică e `received_bytes / received_messages`, iar
fracțiunea se aplică peste `received_bytes`. Un prag calibrat pe cifre raportate
s-ar lărgi singur exact atunci când raportarea e stricată — adică ar tăcea când
trebuie să vorbească.

**Verdictul și depășirea de prag sunt lucruri diferite**, iar metrica le
raportează separat. Verdictul spune că încadrarea din §7.2 e ruptă; depășirea
spune că ruptura e mai mare decât zgomotul cererilor în zbor. O încadrare ruptă
sub prag rămâne vizibilă în `reconciliation`, dar nu ajunge în jurnal.

Alarma emite la nivel `error`, cel mult o dată la interval, **per încarnare și
per direcție**. Per direcție pentru că o încarnare care trece de la o margine la
cealaltă spune ceva nou, nu repetă. Verificarea se face la fiecare cerere —
discrepanța n-are eveniment propriu, e o stare care se poate schimba doar când
sosesc cifre noi — dar emiterea nu, pentru că un log care se repetă la nesfârșit
e un log pe care înveți să-l filtrezi, și atunci alarma a dispărut fără să fie
ștearsă.

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

**Adopția: încarnări prinse din mers.**

Contabilizarea trăiește în memoria procesului server. Când serverul repornește
iar agentul nu, încarnarea dispare de pe server, dar contoarele agentului cresc
mai departe — ele sunt per încarnare, nu per conexiune. Prima cerere de după
repornire aduce un total mare lângă un cont cu zero măsurat.

Aceea **nu e o discrepanță**: nu se poate reconcilia ce n-ai apucat să măsori. E
o linie de bază. Primul raport nenul al unei încarnări necunoscute se adoptă ca
punct zero, iar comparațiile ulterioare se fac pe **creșterea** de la adopție
încoace — pe ambele părți deodată.

Condiția „nenul" desparte cele două cazuri: un agent care tocmai a pornit
raportează zerouri, iar acolo comparația cu zero măsurat e corectă, fiindcă e
chiar începutul încarnării.

Adopția e oglinda tratamentului pentru repornirea **agentului**: acolo,
încarnarea nouă face contoarele să pornească de la zero pe ambele părți
simultan; aici doar o parte s-a resetat, deci diferența se scoate din comparație
explicit. Fără ea, fiecare repornire de server declanșa alarma pentru fiecare
agent care rula (edr-server#11).

**Ce s-a pierdut se declară.** Rândul poartă `adoption.adopted_mid_flight` și
`adoption.unmeasured_before_adoption_bytes`, iar rezumatul le adună. Cifra e
divulgare reală pe care *acest* server n-a măsurat-o, și vine de la agent, deci
e raportată, nu măsurată. Fără ea, „am prins încarnarea târziu" și „totul se
potrivește" ar arăta identic — am fi înlocuit o alarmă falsă cu o tăcere falsă.

Consecință pentru §7.5: un numărător `measured` nenul, lângă un
`unmeasured_before_adoption_bytes` nenul, e **incomplet, nu greșit**. Cine
publică cifra trebuie să citească ambele.

Aceeași mecanică acoperă și reziduul primei înrolări: octeții de dinaintea
emiterii cheii apar acum ca linie de bază declarată, nu ca margine de jos ruptă.

### 7.5 Stadiul de azi

- `1.3a` — *livrat.* Blocul `disclosure` cu treapta și octeții ei de conținut,
  declarat, validat pe ambele părți și atribuit în tabelul de la §3.4.
- `1.3b` — *livrat.* Serializarea explicită în transportul agentului,
  registrul de fir pe canale (`services/wire_ledger.py`), anteturile de la §7.1,
  contabilizarea pe server în middleware (`app/wire_middleware.py`,
  `app/services/wire_accounting.py`), reconcilierea de la §7.4 și **numărătorul
  măsurat** cu factorul de calibrare de la §7.6.

  `progressive.total_bytes` vine acum din octeții numărați de server pe canalul
  de evenimente, iar `progressive.numerator_source` spune, în același obiect,
  dacă cifra e `measured` sau `estimated`. Nimeni nu mai poate publica una
  crezând că e cealaltă.

  **Zero măsurat nu e o măsurătoare.** Contabilizarea trăiește în memoria
  procesului; un agent care nu-și declară încarnarea, sau un server repornit,
  lasă zero octeți măsurați lângă evenimente reale. Zero publicat ca numărător
  ar însemna „endpoint-ul acesta n-a divulgat nimic" — cea mai flatantă
  minciună posibilă despre un sistem de confidențialitate. Se tratează deci ca
  absență de măsurătoare, iar cifra revine la estimare, declarată ca atare.

  **Conținutul nu se adună peste cifra măsurată.** Estimatul e `plic + conținut`
  pentru că plicul reserializează evenimentul stocat, iar conținutul e declarat
  separat. Măsurătoarea e corpul întreg al cererii: când T2/T3 vor trimite
  conținut, el va călători *în* corp, deci e deja înăuntru. `content_bytes`
  rămâne raportat, dar ca **defalcare** a numărătorului măsurat, nu ca termen al
  lui.

  Alarma la discrepanță persistentă e livrată (`app/services/wire_alarm.py`),
  cu pragurile de la §7.3. Cu ea, `1.3b` e **închis**.

- `1.4` — *livrat.* Evenimentele persistă în SQLite, iar noțiunea de **rulare de
  măsurătoare** face persistența utilizabilă în loc de dăunătoare. Definițiile
  și cele trei decizii sunt la §9.

  **Măsurătoarea nu se lipește pe orice rulare.** Contabilizarea de fir numără
  octeți de la pornirea procesului și nu știe nimic despre rulări; numărătorul
  măsurat vine de acolo. Publicat lângă o rulare pe care procesul n-a
  observat-o, ar fi numărul altcuiva purtând autoritatea unei măsurători — o
  rudă mai gravă a lui „zero măsurat nu e o măsurătoare": nu e zero, e cifra
  altui experiment.

  Regula: numărătorul e `measured` doar dacă procesul n-a primit evenimente în
  nicio altă rulare decât cea cerută. Altfel cifrele măsurate rămân raportate,
  dar ca diagnostic al procesului — `measured.applies_to_numerator` e fals,
  `measured.attribution` spune de ce, iar numărătorul revine la estimare.

  Consecință operațională, nu ocolibilă prin cod: **citește metrica înainte să
  deschizi altă rulare în același proces.** O măsurătoare urmată de o sesiune de
  depanare, fără repornire între ele, pierde atribuirea măsurătorii pentru
  amândouă.

**Reziduul cunoscut.** Prima înrolare a unei mașini rămâne neatribuibilă, din
motivul explicat la §7.1: nu există încă o cheie care să dovedească identitatea,
iar `agent_id` trăiește doar în corp. Agentul numără acei octeți în totalul
raportat, serverul îi pune în `no_key`.

Nu mai apare însă ca margine ruptă: mecanica de adopție din §7.4 îi tratează ca
linie de bază declarată, cu mărimea raportată la
`adoption.unmeasured_before_adoption_bytes`. E o descriere mai onestă a
aceleiași realități — octeții nu dispar, dar nici nu sunt acuzați ca discrepanță
ceea ce e doar o parte a încarnării pe care serverul n-a apucat s-o măsoare.

**Ce rămâne nemăsurat, și de ce e vizibil.** Două situații lasă octeți reali în
afara numărătorului măsurat: prima înrolare a unei mașini, și tot ce a trimis un
agent înainte de ultima repornire a serverului. Amândouă se raportează, la
`no_key` respectiv la `adoption`. Un numărător `measured` lângă un
`unmeasured_before_adoption_bytes` nenul e incomplet, nu greșit — iar §8 cere ca
diferența să fie declarată, nu presupusă. Un mesaj la fiecare repornire ar fi cerut.

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
„mediana e X, cu un estimator care greșește cu Y% în agregat". Registrul nu
înlocuiește estimatorul — îl **calibrează**.

**Direcția erorii se citește din factor, nu se presupune.** O formulare
anterioară a acestei secțiuni dădea ca exemplu un estimator care *subestimează*.
Primele măsurători au arătat inversul: factorul iese sub 1, adică reserializarea
dă **mai mult** decât a plecat pe fir. Cauza e vizibilă în ce se reserializează —
evenimentul *stocat* poartă câmpuri adăugate de server (`event_id`,
`received_at`, `run_id`, `status`) și câmpurile nule declarate explicit, pe care
agentul nu le-a trimis niciodată.

`run_id` s-a alăturat listei odată cu §9 și crește plicul reserializat cu
aproximativ 25 de octeți per eveniment. Merge în aceeași direcție ca celelalte —
împinge factorul mai jos — deci nu schimbă concluzia, dar schimbă valoarea, iar
o comparație cu factori măsurați înainte de 1.4 trebuie să știe asta.

Consecința practică: cine mărginește cifrele per fișier cu factorul trebuie să se
uite la valoarea lui, nu la o intuiție despre sensul erorii. Un factor sub 1 mută
cifrele per fișier în jos, nu în sus.

Factorul se publică în secțiunea `calibration`, ca `measured_bytes /
estimated_bytes`. Fără măsurătoare e `null`, nu `1.0`: un factor de unu ar
spune că estimatorul e exact, ceea ce e o afirmație, nu o absență.

**Factorul nu e curat, și confundătorul se raportează lângă el.** Măsurătoarea
numără fiecare plecare — §1.3, coada e at-least-once — și numără și cererile
respinse; estimatorul reserializează evenimentele *stocate*, adică o dată
fiecare. Deci factorul amestecă două lucruri: cât greșește reserializarea și de
câte ori a plecat același eveniment.

`calibration.messages_per_metered_event` desparte cele două. Cât timp e ~1,
factorul descrie estimatorul și poate mărgini cifrele per fișier. Când crește,
o parte din el descrie retransmisiile, iar o cifră „calibrată" cu el ar
transfera costul retransmisiilor asupra fiecărui fișier — inclusiv asupra celor
care au plecat o singură dată. Cine publică o cifră calibrată trebuie să vadă
amândouă numerele, de aceea stau în același obiect.

---

## 8. Ce trebuie declarat lângă orice cifră

O metrică publicată fără contextul de mai jos e o cifră care poate fi citită
greșit, iar responsabilitatea e a celui care a publicat-o.

- **rularea de măsurătoare** din care vine cifra, și dacă eticheta ei a fost
  dată de un om sau inventată de server (§9)
- corpusul: câte fișiere, ce distribuție de dimensiuni, ce fracțiune malițioasă
- suprapunerea între endpoint-uri, când cifra e per parc
- golul din §2.1, pe statusuri
- dacă numărătorul e măsurat sau estimat, și — când e estimat lângă o
  contabilizare nenulă — motivul (§7.5)
- politica activă și calibrarea parametrului de cost
- pentru afirmații despre rate: marginea superioară din §4.3
- **amprenta instantaneului de reputație**, sursele consultate cu versiunile
  lor, și **brațul ablației** — rece sau semiînzestrat (§8.1)

**Din 1.4, prima jumătate a listei călătorește cu cifra.** Răspunsul rutei de
metrică poartă pe prima poziție blocul `run`, cu eticheta, sursa ei, ora
deschiderii și numărul de evenimente; la agregat, `runs_covered` numește
rulările componente. Un cititor care sare peste documentul acesta primește
totuși declarația, iar unul care copiază cifra fără bloc se vede că a tăiat-o.

Obligația nu dispare, se mută: ce nu poate spune serverul — distribuția de
dimensiuni a corpusului, fracțiunea malițioasă, suprapunerea între endpoint-uri
— rămâne de declarat de mână, în intrarea de tip `masuratoare` din jurnal.

### 8.1 Depozitul de reputație

Ce știe sistemul înainte de orice analiză decide direct cât se poate închide la
T0, deci decide cifra din titlu. O măsurătoare care nu declară asta descrie un
sistem care nu se poate reconstitui.

**Amprenta instantaneului.** SHA-256 peste octeții fișierului, calculată la
cerere și verificabilă din afară cu `sha256sum`. Ea răspunde la o singură
întrebare — *ce a citit serverul când a produs cifra asta* — și e singura care
o poate face, fiindcă fișierul e sigilat și deschis `mode=ro&immutable=1`.

Nu se confundă cu amprenta de **conținut**, care trece peste rânduri în ordinea
hash-ului și sare peste tot ce e ceas. Aceea dovedește că un import e idempotent
și că altcineva a reconstruit același lucru; nu spune ce fișier a fost deschis.
Două importuri identice rulate la ore diferite au aceeași amprentă de conținut
și amprente de fișier diferite. Lângă o cifră merge prima.

**Sursele consultate, cu versiunile lor.** Nu ce conține depozitul, ci ce s-a
citit din el: selecția surselor e parametru de rulare, nu proprietate a
fișierului. Versiunea contează fiindcă amprenta acoperă fișierul livrat, nu
procesul care l-a produs — sursele externe se schimbă, ediții de RDS se retrag,
inventarele se rotesc. Versiunea consemnată transformă „nu se poate reproduce"
în „se poate reproduce dacă mai există ediția asta", ceea ce e o afirmație
onestă în loc de una tăcută.

**Brațul ablației, și de ce nu e opțional.** Selecția corpusului a fost făcută
DIN inventarul de amenințări, deci mulțimea malițioasă a corpusului e submulțime
a acelei surse. Un instantaneu care o conține închide **tot** stratul malițios la
T0, iar raportul de divulgare iese spectaculos fără ca protocolul să fi făcut
ceva. Asta e brațul **semiînzestrat**; brațul **rece** se obține excluzând sursa
la interogare.

Diferența dintre cele două nu e o verificare de robustețe, e chiar măsurătoarea
care separă contribuția protocolului de arta anterioară. **O cifră de divulgare
raportată fără să spună care braț a fost rulat nu înseamnă nimic**, fiindcă
poate fi produsă și de o simplă listă de hash-uri.

**Acoperirea peste corpus**, o dată per instantaneu: ce fracțiune din stratul
realist e cunoscută ca software, ce fracțiune din stratul de stres e cunoscută
ca amenințare, și contorul de suprapunere. E numitorul moral al rezultatului —
fără el, un raport de divulgare bun nu se poate deosebi de un corpus care se
întâmplă să fie deja cunoscut.

**Registrul de prevalență nu se amprentează, și cerința asta ar fi imposibilă.**
Depozitul importat e un fișier sigilat; registrul de prevalență e derivat din
evenimente, deci starea lui e o funcție de câte evenimente au intrat până în
acel moment. Se declară ca **stare la începutul rulării** — număr de hash-uri
distincte și număr de agenți distincți — nu ca amprentă. Fără distincția asta,
secțiunea de față ar cere ceva ce nu există pentru jumătate din reputație.

Prevalența se numără **pe agenți distincți, nu pe evenimente**. O singură mașină
care atinge un fișier de cinci sute de ori nu e un parc, iar greșeala nu produce
nicio eroare — doar o cifră mai mare în direcția favorabilă, exact felul de
eroare pe care evaluarea nu o prinde.

---

## 9. Rularea de măsurătoare

### 9.1 De ce există

Cât timp depozitul de evenimente murea la repornirea serverului, igiena era
gratuită și nimeni n-o proiectase: porneai serverul, făceai experimentul,
citeai cifra — iar cifra descria exact experimentul, fiindcă nu mai era nimic
altceva înăuntru. Corpusul cerut de §8 era implicit corect.

Persistența desființează accidentul. Evenimentele se adună peste zile și peste
experimente diferite, iar o metrică nefiltrată ar amesteca o probă cu 444 de
fișiere, o rulare de depanare cu trei și un test de parc cu douăzeci de agenți
într-o singură medie care nu descrie niciunul dintre ele — arătând exact ca
înainte.

Deci persistența fără noțiunea de rulare face **rău net**: schimbă o cifră care
descrie ceva într-una care nu descrie nimic, păstrându-i aparența. Măsurat pe o
probă de trei fișiere, o singură sesiune de depanare cu un fișier de 12 octeți
umflă raportul experimentului cu circa treizeci la sută.

**Definiție.** O rulare de măsurătoare e o etichetă lipită pe fiecare eveniment
**la ingestie**, care spune din ce experiment face parte. Serverul are exact o
rulare curentă; evenimentele primite până la următoarea schimbare o poartă pe ea.

Eticheta se pune la ingestie, nu la citire. Aplicată la citire, fiecare
experiment nou ar goli experimentele vechi mutându-le evenimentele la el, iar
cifrele publicate ieri s-ar schimba azi fără ca nimeni să fi atins datele.

### 9.2 Cum se numește o rulare — decizia D1

Două surse, amândouă necesare:

- **generată**, la prima nevoie de o etichetă, cu prefixul rezervat `auto-`. E
  plasa de siguranță: niciun eveniment nu rămâne neetichetat, iar comportamentul
  de dinainte de persistență — o repornire = un experiment nou — se păstrează
  fără nicio muncă în plus.
- **dată de operator**, prin `POST /api/runs/{eticheta}`. E instrumentul.

Eticheta de operator trebuie să fie **numele intrării de tip `masuratoare` din
jurnal**. Atunci legătura dintre ce s-a promis că se măsoară și ce date au ieșit
devine verificabilă, nu declarată: oricine ia numele din jurnal, îl dă
serverului și primește exact cifrele acelui experiment. Fără ea, legătura e pe
încredere.

Prefixul `auto-` e refuzat operatorului, ca întrebarea „cine a numit rularea" să
aibă răspuns și peste șase luni.

**O etichetă folosită o dată nu se mai poate redeschide.** E singurul mod în
care mecanismul poate minți: date noi turnate în cifre deja citate, cu un
răspuns care arată identic — același nume, alte numere. Aceeași regulă ca în
jurnal, unde un commit de montaj nu se modifică prin `amend` sau `push --force`.
Refuzul e 409, iar registrul etichetelor trăiește în aceeași bază cu
evenimentele: în memorie s-ar fi golit exact la repornirea care face refolosirea
probabilă.

**Rularea curentă NU supraviețuiește repornirii, deliberat.** Registrul e
persistent, indicatorul spre rularea deschisă nu. Alternativa — o rulare de
operator rămasă deschisă peste restarturi — ar aduna tăcut, peste săptămâni, tot
ce trimite parcul, inclusiv sesiuni de depanare fără legătură cu experimentul;
adică exact refolosirea de etichetă interzisă mai sus, doar automatizată.

Prețul se declară: **un experiment întrerupt de o repornire nu se poate relua
sub aceeași etichetă.** Se continuă sub una nouă, iar analiza le adună explicit
pe amândouă. Ruptura vizibilă e preferabilă lipiturii invizibile.

### 9.3 Ce descrie o cifră, implicit — decizia D2

Implicit, **rularea curentă**. Agregatul peste tot depozitul se cere explicit,
prin `all_runs`.

Implicitul nu e comoditate. Tot istoricul e mai util operațional și mai
periculos pentru o lucrare: o medie peste experimente cu distribuții diferite de
fișiere nu descrie niciunul dintre ele. Se poate cere — dar se cere, iar
răspunsul își numește componentele în `runs_covered`.

**O etichetă necunoscută primește 404, nu un rezultat gol.** Un răspuns gol
pentru o etichetă scrisă greșit ar arăta exact ca un experiment care n-a
divulgat nimic — cea mai flatantă cifră posibilă despre un sistem de
confidențialitate, obținută dintr-o greșeală de tastare. O rulare care există și
chiar n-a produs evenimente se deosebește: eticheta ei e în registru, deci
primește zero, cu rularea declarată alături.

### 9.4 Un singur proces — decizia D3

Serverul rulează cu un singur worker, iar asta e **declarat**, nu presupus.

Ce garantează SQLite peste procese: modul WAL permite mai mulți cititori
simultan cu un scriitor, iar scrierile concurente se serializează prin blocajele
lui proprii — baza nu se corupe dacă cineva pornește un al doilea proces. Ce
**nu** rezultă de aici e că sistemul ar funcționa. Partea care se rupe e
contabilitatea din memorie:

- `wire_accounting` ține contoarele per proces, deci aceeași încarnare ar fi
  numărată în două locuri, iar reconcilierea de la §7.4 ar raporta două jumătăți
  ca și cum ar fi două întregi;
- rularea curentă e tot per proces, deci doi workeri porniți în aceeași secundă
  ar eticheta evenimente cu două nume diferite.

WAL e ales pentru cititor-care-nu-blochează-scriitorul: metrica citește tot
tabelul rulării și altfel ar bloca ingestia chiar în timpul unei măsurători.

### 9.5 Ce nu persistă, și de ce

- **registrul de agenți** rămâne volatil. Cheile au fost scoase din el tocmai
  pentru că se golește la repornire, iar agenții se re-înregistrează singuri;
  persistat, ar readuce credențialele în raza rutei publice de inventar.
- **contabilizarea de fir** rămâne per încarnare, în memorie (D3). Consecința
  ei asupra numărătorului măsurat e la §7.5.
- **mulțimea rulărilor observate de proces**, folosită de aceeași regulă,
  trăiește tot în memorie: întrebarea la care răspunde e despre procesul curent,
  nu despre istoric.

### 9.6 Ce rămâne deschis

- Dacă serverul va rula vreodată cu mai mulți workeri, `wire_accounting` trebuie
  mutat în bază, iar blocajul de proces al depozitului nu mai e suficient.
- Deschiderea unei rulări se autorizează azi cu secretul de înrolare, singura
  credențială de nivel de operator existentă. E o reutilizare declarată, nu o
  scăpare, dar un secret de operator propriu e pasul care o închide, odată cu
  rutele de citire.
