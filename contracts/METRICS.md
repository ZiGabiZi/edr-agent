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

**Ce e livrat și ce e doar definit** (18 noiembrie 2026). Regula de aur cere ca
definiția să vină înaintea mecanismului, deci documentul conține și metrici
care încă nu au cod. Tabelul spune care sunt care, ca o definiție să nu fie
citită drept rezultat.

| Secțiune | Stadiu |
|---|---|
| §1–§2.2 octetul divulgat, numitorul, golurile, concentrarea | livrat; cifra în `GET /api/metrics/disclosure` |
| §3.1 costul marginal per endpoint | definit; măsurătoarea pe parc (etapa 2.5) nu s-a făcut |
| §3.2 mediana și p95 per fișier | definit; plicul per eveniment e consemnat, dar distribuția nu se calculează nicăieri |
| §3.3 media și raportul agregat | raportul agregat livrat (`ratio`); media per endpoint nu se publică separat |
| §3.4 atribuirea pe trepte | livrat, cu o singură treaptă existentă, T0 |
| §3.5 dispoziția de la T0 | livrat (P2.3) |
| §3.6 prevalența în parc | livrat (P2.4) |
| §3.7 banda minimă la T0 | livrat (P2.6), fără escaladare reală |
| §4 oracolul, categoriile, frontiera | definit; niciun motor de analiză nu există încă |
| §5 rata de detecție | definit; depinde de oracol |
| §6 costul observației | `settle_wait_ms` și `hash_duration_ms` livrate; latența per treaptă nu există fără trepte |
| §7 precizia numărătorului | livrat (pașii 1.3a, 1.3b), cu stadiul la §7.5 |
| §8 ce se declară lângă cifră | parțial automat: blocurile `run`, `reputation`, `prevalence`, `band`; restul se declară de mână |
| §9 rularea de măsurătoare | livrat (pasul 1.4) |

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
| Metadate de eveniment de ciclu de viață | da | `agent_startup`, `agent_shutdown` — aceeași rețea. `agent_restart` și `agent_monitoring_changed` nu: le creează serverul, din heartbeat, deci n-au traversat rețeaua ca eveniment |
| Conținut divulgat la T2 | da | regiuni cerute de server |
| Conținut divulgat la T3 | da | fișierul integral |
| Retransmisii | da | vezi §1.3 |
| Canalul de control (heartbeat) | separat | vezi §1.4 |

### 1.2 Ce nu se numără, și de ce

**Anteturile HTTP și cadrele TCP/TLS.** Sunt cost de transport, nu de protocol,
și sunt identice sub ambele politici. Includerea lor ar adăuga aceeași constantă
în numărător și numitor.

**Traficul server→agent.** Rulesetul coborât, directivele, verdictele, iar azi
blocurile `reputation` și `prevalence` din răspunsul la fiecare eveniment cu
hash. Ele merg în direcția opusă și nu ating suprafața de atac descrisă în
§L2.2. Costul lor aparține §L2.8, dar **azi nu se măsoară**: contabilizarea de
pe server (`wire_accounting`) numără doar octeții primiți. Golul e declarat, nu
rezolvat, și crește cu fiecare bloc adăugat pe răspuns.

**Fișierele pe care agentul le-a citit local.** Un fișier de 200 MB hash-uit pe
endpoint costă I/O, nu divulgare. Costul acela e o măsurătoare, nu un octet
divulgat, și trăiește la §6.

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

**Câmpul `monitoring` (contract v10) ridică podeaua.** Fiecare heartbeat poartă
starea supravegherii: `, "monitoring": "active"` adaugă **24 B** pe fir
(serializarea implicită a agentului, cu spații), iar stările de eșec până la
**37 B** (`monitor_unavailable`). La cadența de 10 s, adică 8 640 de
heartbeat-uri pe zi, înseamnă circa **207 kB pe zi per endpoint** în stare
normală. Se adună la estimarea podelei, nu se lasă în afara ei: e un cost al
observației, nu al divulgării, și crește cu timpul, nu cu fișierele.

---

## 2. Numitorul: modelul always-upload

**Definiție.** Ce ar fi părăsit endpoint-ul dacă fiecare fișier observat ar fi
fost urcat integral pentru analiză pe server.

**Numitorul include plicul, nu doar conținutul.** Un sistem always-upload nu
trimite fișiere goale: trimite fișierul *plus* aceleași metadate de identificare.
Numitorul e deci `conținut + plic`.

Corecția mută raportul **în favoarea** protocolului — `plic / (plic + conținut)`
e mai mic decât `plic / conținut` — și tocmai de aceea plicul din numitor nu are
voie să fie nici cu un octet mai mare decât cel de la numărător. Orice octet în
plus acolo e o umflare a numitorului, adică exact obiecția pe care definiția
trebuie s-o facă imposibilă.

**Plicul se măsoară cu rigla numărătorului.** Regula, nu o egalitate descrisă:

| numărătorul (`numerator_source`) | plicul din numitor (`envelope_source`) |
|---|---|
| `measured` — corpul cererilor numărat pe fir | corpul cererii care a creat fiecare eveniment, consemnat de server la primire |
| `estimated` — reserializarea evenimentelor stocate | reserializarea aceluiași eveniment |

Nicio cifră nu amestecă rigle. Formularea anterioară — „același plic ca la
numărător" — era adevărată cât timp ambele părți reserializau evenimentul
stocat, și a devenit falsă fără să se schimbe un rând de aici când numărătorul a
trecut pe măsurătoare (edr-server#14). Evenimentul stocat poartă câmpuri pe care
agentul nu le-a trimis (`event_id`, `received_at`, `run_id`, `status`,
`reputation`, `prevalence`, nulurile explicite), deci reserializarea lui e mai
lungă decât corpul de pe fir. Pe un fișier de 900 B, cu 811 B reserializați și
490 B pe fir, raportul publicat ieșea 0,286 în loc de 0,353 — eroare invizibilă
la fișierele mari, de ordinul a 19% din numitor sub 2 KB.

Plicul de pe fir per eveniment se consemnează o singură dată, la prima sosire.
Retransmisiile rămân doar în numărătorul măsurat (§1.3), deci asimetria care
rămâne poate face protocolul să pară cel mult mai scump, niciodată mai ieftin.
Regula o apără `app/tests/test_measured_envelope.py`.

**Numitorul e definit doar pentru fișierele cu dimensiune cunoscută**, adică
`hash_status == "ok"`. Restul intră la §2.1.

### 2.1 Golul declarat

Fișierele cu `hash_status` diferit de `ok` nu au dimensiune cunoscută, deci nu
contribuie la numitor, dar metadatele lor au plecat și contribuie la numărător.
Efectul e o **subestimare** a economiei.

Golul se raportează întotdeauna, defalcat pe status: `unstable`, `unreadable`,
`too_large`, `vanished`, `skipped_capacity`, `skipped_shutdown`. Un raport de
divulgare fără el e un raport care ascunde câte fișiere n-a putut măsura.

**Plicul nemăsurat, tot în golul de dimensiune.** Cu numărătorul măsurat, un
fișier hash-uit al cărui plic de pe fir nu s-a consemnat — evenimente mai vechi
decât consemnarea, cereri fără `Content-Length` — iese din numitor cu totul,
conținut și plic, iar numărul lor se raportează ca `envelope_unmeasured`.
Completat cu plicul reserializat, ar fi readus exact octeții adăugați de server;
scos, numitorul scade, ceea ce e tot o subestimare a economiei. Cu numărătorul
estimat, `envelope_unmeasured` e zero: orice eveniment are un plic
reserializabil.

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

### 2.2 Concentrarea numitorului

Numitorul nu e măsurat, e **declarat**: `file_size` vine din `stat()`-ul
agentului. Numărătorul se poate măsura pe fir (§7), numitorul nu. Până la
contract v12 câmpul n-avea nicio limită, iar un singur eveniment cu
`file_size = 10^15` lângă zece fișiere de 1 MB a dus raportul de la 0,000336 la
0,0, fără nicio urmă că un eveniment din unsprezece îl produsese.

De la v12 schema respinge cu 422 orice `file_size` negativ sau ne-întreg. Limita
de sus nu se impune: plafonul real e `hash_max_file_size_bytes` din configul
agentului, pe care serverul nu-l cunoaște. Un prag ales pe server ar fi ori prea
larg ca să prindă ceva, ori ar respinge un fișier real. Mai mult, niciun prag
n-ar prinde un agent care minte în limite plauzibile, de exemplu 200 MiB la
fiecare fișier.

Apărarea e deci o declarație lângă cifră, nu un prag. `always_upload.concentration`
raportează:

- `largest_event` — evenimentul cu cea mai mare contribuție la numitor
  (`event_id`, `agent_id`, `bytes`, `share`);
- `by_agent` — pentru fiecare agent, câte evenimente, câți octeți și ce cotă din
  numitor.

Contribuția e `file_size + plic`, cu plicul măsurat cu rigla numitorului (§2),
deci cotele din `by_agent` se adună la 1. Un `share` apropiat de 1 la
`largest_event` înseamnă că raportul descrie un singur fișier, nu rularea; unul
apropiat de 1 la un agent, într-un parc, înseamnă că raportul descrie acel
endpoint.

**Ce nu se calculează: raportul fără evenimentul cel mai mare.** Scos din
numitor, evenimentul ar trebui scos și din numărător, iar sub numărătorul
măsurat serverul are doar totalul de pe fir, nu octeți per eveniment (§3.2).
Cele două părți ar ajunge pe rigle diferite, exact greșeala închisă la
edr-server#14.

**Evenimentele de dinainte de v12.** Depozitul păstrează evenimente primite când
`-5000000`, `true` și `4096.0` treceau. Un fișier hash-uit a cărui dimensiune
stocată nu e un întreg nenegativ iese din numitor cu totul și se numără ca
`unmeasured.file_size_invalid`. Lăsat înăuntru, un negativ ar micșora
numitorul, iar un `true` l-ar crește cu un octet inventat.

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
dintre cele două axe o produce. Dispoziția singură nu e o închidere: decizia de
închidere la T0 e a benzii minime (P2.6, §3.7), care primește dispoziția și
prevalența și publică tabelul ei separat.

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

**Nu se publică o rată de închidere aici.** Ar cere maparea *dispoziție → închis*, iar
aceea e decizia benzii de incertitudine (§L2.7), nu a acestui tabel. De la P2.6
banda minimă are cifra ei, la §3.7, cu politica declarată lângă ea. Cazul care o
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

**„Prima oară" înseamnă că registrul nu avea conținutul înainte de eveniment**,
indiferent de mașină — nu `agents == 1`. A doua vedere a aceluiași conținut pe
aceeași mașină tot `1` dă, deci regula după `agents` ar număra două prime vederi
pentru un singur conținut, iar invarianta „pe memorie goală, primele vederi sunt
cât conținuturile distincte" ar cădea exact când o mașină trimite două
evenimente pentru același fișier. Valoarea se consemnează la ingestie, în
`event_band` (§3.7); evenimentele fără rând păstrează regula veche, iar câte au
fost decise așa se publică, ca `first_sighting_inferred_from_agents`.

**Nu se publică nicio economie aici.** „Câte escaladări a evitat parcul" cere să se
știe care evenimente ar fi escaladat, iar decizia aceea aparține benzii de
incertitudine (§L2.7). Tabelul numără; interpretarea rămâne a benzii, care o
publică la §3.7, ca `inherit`.

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

### 3.7 Banda minimă la T0

**Definiție.** Pentru fiecare eveniment cu hash, ce ar face protocolul la T0: se
închide sau urcă pe scară, și din care rând al tabelului de decizie. Nu se
escaladează nimic în realitate: pe fir `next_action` rămâne `"none"`, iar decizia
stă în depozit, în `event_band`, nu în răspuns. Contractul de fir nu se schimbă.

**Tabelul de decizie, politica `t0-min-1`.** Primul rând care se potrivește
câștigă; „văzut înainte" înseamnă că registrul de prevalență avea conținutul
înainte de eveniment (§3.6).

| rând | dispoziție | văzut înainte | decizie |
|---|---|---|---|
| 1 | `known_malicious`, `both_axes` | oricum | `close_threat` |
| 2 | `reputation_unavailable` | oricum | `hold` |
| 3 | `unknown` | nu | `escalate` |
| 4 | `unknown` | da | `inherit` |
| 5 | `known_software` | oricum | `close_known` |

O dispoziție pe care tabelul n-o cunoaște nu primește decizie: evenimentul
rămâne fără rând și se numără ca gol, sub numele ei.

**Ordinea rândurilor e decizia de securitate.** Amenințarea vine prima, ca
prevalența să n-o poată depăși. Indisponibilitatea vine înaintea lui `unknown`,
ca o pană a depozitului să nu arate ca un corpus nou (§3.5).

**Prevalența nu închide benign nimic.** `CORPUS.md` §5.4 interzice ca RDS să dea
„curat", deci singura dovadă care ar putea închide benign la T0 ar fi
prevalența. Regula „pe `k` mașini ⇒ închis" e respinsă: într-un parc de cinci,
`k = 3` transformă coada de 3% a malware-ului (`CORPUS.md` §6.4) în ratări
construite prin proiectare, iar §4.3 fixează ratările la zero. Prevalența e aici
dovada că parcul a plătit deja escaladarea conținutului — `inherit`. Verdictul
vine din escaladare (T1 și mai sus). Singura ieșire de la T0 care se termină fără
semnalare e `close_known`, care spune doar „nu necesită escaladare din motiv de
noutate" (`CORPUS.md` §5.4) și nu depinde de prevalență.

**Un fișier malițios pe toate mașinile.** Pe axa de amenințare: `close_threat` la
fiecare sosire. Necunoscut: o escaladare și restul moșteniri, deci un verdict,
comparat pe amprentă (§4.1) — o eventuală ratare e una, nu una per mașină. Doar
în RDS: `close_known` pe toate, la fel ca pe una. Corpusul nu conține cazul
(malware-ul stă pe 1–3 mașini), deci e acoperit de teste, nu de măsurătoare.

**Vechimea nu intră.** În laborator toate vârstele sunt de minute sau ore, deci
niciun rând pe vechime n-ar fi exercitat. `first_seen` rămâne stocat și intră
odată cu T1.

**Cifra publicată e tabelul, nu rata.** Secțiunea `band` din
`GET /api/metrics/disclosure` dă celulele — dispoziție × văzut înainte ×
`agents_at_arrival` — pe numitorul `events_with_hash` de la §3.5. Orice politică
de forma *decizie = f(dispoziție, văzut înainte)* se citește din ele fără altă
rulare, iar `agents` brut păstrează și politicile pe prevalență. Alături:
politicile, rând cu rând; deciziile fiecărei politici, calculate din celule;
deciziile stocate și dacă ele coincid cu tabelul (`stored_matches_table` — un
tabel schimbat sub o rulare deja făcută ar descrie altă politică decât cea care a
decis).

**Două politici, pre-înregistrate amândouă.** `t0-min-1`, cea declarată și
stocată, și `t0-min-1-strict`, care tratează `known_software` ca `unknown`. A
doua se calculează din aceleași celule. Scrise amândouă înainte de măsurătoare,
niciuna nu poate fi aleasă după ce se văd datele.

**Golurile, ca la §2.1.** `hashed_events_without_band` — evenimente cu hash fără
rând: scrise înainte de P2.6 sau cu o dispoziție necunoscută tabelului. Se
defalcă pe dispoziție, în `without_band_by_disposition`; cele fără nicio
dispoziție, scrise înainte de P2.3, apar ca `without_disposition`, numele
golului de la §3.5.
`inherited_without_escalation` — `inherit` al cărui conținut n-are niciun
`escalate` în toată baza: prima vedere a fost `hold`, a fost scrisă înainte de
bandă, registrul e murdar, sau prima sosire a eșuat după ce își înregistrase
vederea. Ultimul caz vine din ordinea ingestiei: vederea se scrie în registru,
cu commit, înaintea evenimentului, deci dacă inserarea evenimentului pică,
retransmisia își găsește propria vedere și primește `inherit` în loc de
`escalate`. Ordinea e de dinainte de bandă; tot ea face ca, la §3.6, aceeași
retransmisie să se numere ca vedere anterioară, nu ca primă vedere. Moștenire
fără de la cine.

**Criteriile de infirmare 1 și 5 au aici doar o margine superioară.** Ce
escaladează la T0 e plafonul pentru ce ajunge la T2 și pentru ce se poate abține
mai sus pe scară. Dacă plafonul e deja sub pragul criteriului 1, afirmația e
trivială fără T1. Pragurile se fixează pe acest tabel, înaintea oricărui rezultat
T1, ca să nu poată fi alese după ce se vede cât ajunge la T2.

**Pragurile, fixate pe 24 noiembrie 2026**, după prima rulare cu tot corpusul pe
cele cinci endpoint-uri (eticheta `masuratoare-2.5-parc-esalonat-r2`), înainte de
orice cod T1:

- **Unitatea:** conținuturile distincte ale corpusului (1494), nu evenimentele.
  Parcul plătește escaladarea unui conținut o singură dată, iar `inherit` nu mai
  urcă. Evenimentele se raportează alături.
- **Brațul:** cel rece (§8.1). În brațul semiînzestrat tot stratul malițios se
  închide la T0, deci nicio mostră n-ar ajunge la T2, iar pragurile ar măsura
  instantaneul, nu protocolul. Brațul semiînzestrat se raportează alături.
- **Politica:** `t0-min-1`, cea declarată; `t0-min-1-strict` se raportează ca
  verificare de sensibilitate.
- **Criteriul 1, trivialitatea:** afirmația e trivială dacă ajung la T2 mai
  puțin de **100** de conținuturi distincte. Pragul e un număr, nu un procent,
  din același motiv ca la §4.3: sub 100 de cazuri nu se poate susține nimic util
  despre felul în care decide banda.
- **Criteriul 5, abținerea:** protocolul e inutilizabil dacă se abține pe mai
  mult de **10%** din conținuturile corpusului, adică pe peste 149 din 1494. Se
  raportează și ca fracțiune din conținuturile escaladate. Valoarea e o
  judecată, nu vine dintr-o sursă; legitimă o face doar faptul că e fixată
  înainte de T1.

**Marginile din rulare.** Brațul rece escaladează 919 conținuturi distincte
(61,5% din corpus; 919 din 5784 de evenimente, 15,9%), cel semiînzestrat 589
(39,4%; 10,2% din evenimente). Amândouă stau peste pragul criteriului 1 și peste
cel al criteriului 5, deci la T0 niciunul dintre criterii nu e decis: decide T1.

**O rulare cu `hold > 0` nu e comparabilă cu una fără**: o parte din corpus n-a
primit nicio decizie, iar partea aceea nu e aleasă la întâmplare.

**Predicția, înainte de măsurătoare** (braț semiînzestrat, bază goală, o rulare
cu tot corpusul): `escalate` = 589, câte un eveniment per conținut necunoscut
(164 compilate, 290 instalatoare, 135 de sistem); `close_threat` = plasările
celor 330 de mostre; `close_known` = plasările celor 575 de conținuturi din RDS;
`inherit` = evenimentele conținuturilor necunoscute minus 589; `hold` și
`inherited_without_escalation` = 0. În brațul rece, `escalate` = 919.

---

## 4. Fidelitatea față de oracol

### 4.1 Oracolul

**Definiție.** Verdictul produs de **același motor de analiză** alimentat cu
fișierul integral, adică sistemul rulat în modul `always_upload`.

**Unde rulează.** Modul `always_upload` rulează **pe endpoint**, nu pe server.
Prin proiectare rulesetul coboară pe endpoint (§L2.8), deci același motor poate fi
alimentat cu fișierul integral acolo unde fișierul se află; peste fir trece doar
verdictul. Azi niciun motor de analiză nu există în cod, nici pe agent, nici pe
server: oracolul e o cerință de montaj, nu un mecanism livrat.

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
| `X-Agent-Instance-Id` | încarnarea procesului care trimite, căreia îi aparțin cifrele de mai sus |

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

Valoarea antetului e **încarnarea procesului care trimite**, nu cea din corp.
Cele două coincid pentru orice mesaj produs de încarnarea curentă, dar nu și
pentru un eveniment retransmis din spool după o repornire: corpul lui păstrează
`agent_instance_id` al încarnării care l-a **creat**, în timp ce octeții
retransmiterii sunt numărați de registrul încarnării care îl **trimite**.
Antetul trebuie să numească proprietarul contoarelor. Altfel serverul pune
octeții la o încarnare și raportul la alta, iar reconcilierea arată un
`below_lower_bound` fals exact după repornirile cu spool nevid (edr-agent#21).

Prima formă a contractului cerea citirea din payload, ca antetul și corpul să
nu se poată contrazice. Pentru evenimentele din spool, regula aceea dădea
antetului încarnarea greșită.

Diferența antet ≠ corp nu e deci o contradicție, ci informație: corpul spune
cine a observat evenimentul, antetul cine l-a pus pe fir. Serverul
contabilizează după antet și stochează evenimentul după corp, iar niciuna
dintre cele două nu o corectează pe cealaltă.

**Excepția e înregistrarea**, iar ea merită citită cu atenție, pentru că arată
de ce antetul nu e doar o comoditate. `wire-contract.json` **interzice**
`agent_instance_id` în corpul înregistrării: la repornire, reînregistrarea
rulează înaintea primului heartbeat, iar `register_agent()` face `update()`
peste înregistrarea existentă, deci o încarnare venită prin corp ar suprascrie
baseline-ul înainte ca heartbeat-ul să-l poată compara — și `restart_detected`
n-ar mai fi True niciodată.

Interdicția e însă pe **corp**, nu pe cerere. Antetul nu trece prin schema
serverului, nu ajunge în `agents_store` și nu atinge baseline-ul; e citit doar
de contabilizare. Deci încarnarea pleacă și la înregistrare, pe antet, din
aceeași sursă ca la orice altă cerere: încarnarea procesului.

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
| `no_instance` | agent cunoscut, încarnare nedeclarată — azi doar un agent de dinainte de edr-agent#19 |
| `unsized` | corp fără `Content-Length`; dimensiunea e **necunoscută, nu zero** |

Motivele nu se contopesc. `unknown_key` e un agent cu credențiale stricate care
divulgă tot ce trimite fără să scrie nimic nicăieri; `no_instance` era
consecința cunoscută a înregistrării, care pleca fără antet de încarnare până la
edr-agent#19. De atunci agentul pune antetul la orice cerere, deci un
`no_instance` nenul înseamnă un agent vechi sau un client care nu e agentul.
Într-un singur număr, niciunul n-ar mai fi lizibil.

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
diferența să fie declarată, nu presupusă.

Estimatorul din `disclosure_metrics.py::_payload_bytes` — reserializarea
evenimentului stocat, cu câmpurile adăugate de server și fără anteturile HTTP —
rămâne numărătorul doar când măsurătoarea lipsește sau nu se aplică rulării
cerute, și atunci se declară ca **estimare** (`numerator_source: estimated`),
nu ca măsurătoare — vezi §8.

**Numitorul urmează numărătorul.** Când numărătorul a trecut pe măsurătoare,
plicul din numitor a rămas reserializarea, deci cele două părți ale raportului
foloseau rigle diferite, iar cea a numitorului era mai lungă (edr-server#14).
Din acel moment serverul consemnează corpul fiecărei cereri de eveniment la
primire, în tabelul `event_wire`, iar numitorul măsurat ia plicul de acolo.
Regula și golul ei sunt la §2 și §2.1.

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

**Circularitatea e a mesajului, nu a serverului.** Ea apare doar dacă valoarea
trebuie să călătorească *în* mesaj. Serverul o poate observa din afara lui: cum
agentul trimite un eveniment pe cerere, `Content-Length` al cererii e plicul
exact al acelui eveniment, iar din edr-server#14 serverul îl consemnează la
primire, în `event_wire`. Cu el, raportul per fișier
`plic / (file_size + plic)` devine măsurabil la T0 — măsurat la prima sosire,
fără retransmisii. Mediana și p95 nu se calculează încă nicăieri; până atunci,
asimetria de mai sus rămâne cea publicată, dar nu mai e inevitabilă.

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
- concentrarea numitorului din §2.2: cota evenimentului celui mai mare și a
  fiecărui agent
- dacă numărătorul e măsurat sau estimat, și — când e estimat lângă o
  contabilizare nenulă — motivul (§7.5)
- politica activă și calibrarea parametrului de cost; de la P2.6, politica
  benzii minime (`band.declared_policy`), contorul `hold` și golul
  `inherited_without_escalation` (§3.7)
- pentru afirmații despre rate: marginea superioară din §4.3
- **amprenta instantaneului de reputație**, sursele consultate cu versiunile
  lor, și **brațul ablației** — rece sau semiînzestrat (§8.1)
- **endpoint-urile care n-au fost supravegheate** pe toată durata rulării:
  orice eveniment `agent_monitoring_changed` din rulare. Un endpoint orb
  produce zero evenimente de fișier și arată exact ca unul liniștit, deci
  într-o rulare cu mai multe endpoint-uri trage în jos curba din §3.1 fără ca
  cifra să lase vreo urmă. Un astfel de endpoint se exclude din marginal sau se
  declară lângă cifră. Din contract v11 intră aici și endpoint-ul `partial`,
  pornit cu doar o parte din directoarele configurate sau care a pierdut unul
  în timpul rulării: produce evenimente, dar doar din o parte a zonei, deci
  trage curba în jos la fel, doar mai greu de observat. Starea nu ține până la
  repornire: agentul pune directorul înapoi sub observer când reapare, deci
  perechea `-> partial` / `partial -> active` din aceeași incarnare
  delimitează fereastra neacoperită, iar endpoint-ul se exclude sau se declară
  pentru acea fereastră. Evenimentul e emis de server la schimbarea câmpului
  `monitoring` din heartbeat și e persistat cu eticheta rulării. Registrul de
  agenți e volatil (§9.5), așa că starea curentă nu ajunge pentru o rulare deja
  încheiată. Ambele se citesc prin `GET /api/agents/monitoring?run_id=…`, cu
  secretul de operator. Rutele publice nu le arată (AUTH.md §6).

  **Ce poate afirma câmpul: defecte, nu sabotaj.** `monitoring` e declarat de
  agent despre el însuși. Un **defect** e o defecțiune fără intenție: spool-ul
  nu se deschide, un director lipsește, un fir moare. Agentul e onest și o
  raportează. **Sabotajul** e oprirea intenționată a supravegherii de către
  cineva care controlează endpoint-ul. Acela poate opri pipeline-ul și lăsa
  heartbeat-ul să trimită în continuare `active`. Deci un `active` nu dovedește
  supraveghere. Arată doar că agentul nu a detectat la el niciun defect. O
  rulare fără `agent_monitoring_changed` e curată de defecte, nu și de
  sabotaj. Sabotajul se poate detecta doar printr-o verificare pe care agentul
  n-o poate falsifica: de exemplu un fișier-momeală creat de server, al cărui
  eveniment trebuie să apară într-o fereastră cunoscută. E direcție viitoare,
  nu mecanism existent.

**Din 1.4, prima jumătate a listei călătorește cu cifra.** Răspunsul rutei de
metrică poartă pe prima poziție blocul `run`, cu eticheta, sursa ei, ora
deschiderii și numărul de evenimente; la agregat, `runs_covered` numește
rulările componente. Un cititor care sare peste documentul acesta primește
totuși declarația, iar unul care copiază cifra fără bloc se vede că a tăiat-o.

Obligația nu dispare, se mută: ce nu poate spune serverul — distribuția de
dimensiuni a corpusului, fracțiunea malițioasă, suprapunerea între endpoint-uri
— rămâne de declarat de mână, în fișa de măsurătoare din jurnal (§9.2).

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

Eticheta de operator trebuie să fie **numele fișei de măsurătoare din
jurnal**. Atunci legătura dintre ce s-a promis că se măsoară și ce date au ieșit
devine verificabilă, nu declarată: oricine ia numele din jurnal, îl dă
serverului și primește exact cifrele acelui experiment. Fără ea, legătura e pe
încredere.

**Ce înseamnă „jurnal" aici.** Documentația proiectului în care fișa de
măsurătoare se scrie *înainte* de rulare: ce se măsoară, corpusul, ordinea de
adăugare a endpoint-urilor, predicțiile. Până pe 13 noiembrie 2026 fișa era o
intrare de tip `masuratoare` în repo-ul `edr-journal`, unde s-au pre-înregistrat
corpusul și prevalența; de atunci documentația se ține în afara repo-urilor de
cod. Regula nu se schimbă: eticheta rulării e numele fișei, iar fișa e scrisă
înaintea rulării.

Prefixul `auto-` e refuzat operatorului, ca întrebarea „cine a numit rularea" să
aibă răspuns și peste șase luni.

**O etichetă folosită o dată nu se mai poate redeschide.** E singurul mod în
care mecanismul poate minți: date noi turnate în cifre deja citate, cu un
răspuns care arată identic — același nume, alte numere. Aceeași regulă ca la
fișa de măsurătoare, care nu se rescrie după rulare, și ca la commit-urile de
montaj, care nu se modifică prin `amend` sau `push --force` după ce au fost
publicate.
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
