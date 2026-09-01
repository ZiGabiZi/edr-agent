# Corpusul — specificația de proiectare

Document valabil pentru ambele repo-uri (`edr-agent`, `edr-server`). Cele două
exemplare trebuie să fie identice; sincronizarea lor se verifică la fel ca la
`AUTH.md` și `METRICS.md`.

`METRICS.md` §8 spune ce trebuie declarat lângă orice cifră și observă că o
parte din listă — distribuția de dimensiuni, fracțiunea malițioasă, suprapunerea
între endpoint-uri — nu poate fi produsă de server, deci rămâne de declarat de
mână. Documentul acesta e locul unde acele valori se **fixează înainte** de
măsurătoare, ca declarația de mai târziu să fie o citire, nu o reconstrucție.

**Convenția de referințe.** `§N` fără altă calificare trimite întotdeauna la o
secțiune a **acestui** document. O secțiune din alt contract se scrie calificat,
`CORPUS.md §5.4`. O secțiune a **lucrării** poartă prefixul `L`: `§L2.4`.
Fără prefix, `§2.1` ar însemna două lucruri diferite în aceeași frază — iar
numerotarea lucrării se mută la fiecare secțiune inserată, a celui de aici nu.

---

## 1. Corpusul e un instrument, nu un set de date

Tentația firească e să aduni fișiere și să rulezi sistemul peste ele. Ar fi
greșit, și greșeala nu s-ar vedea în rezultate.

Criteriul 1 de infirmare — *afirmația e trivială dacă banda nu se declanșează
niciodată* — e, citit atent, o afirmație despre **corpus**, nu despre protocol.
Un corpus alcătuit din binare de sistem semnate și malware cunoscut se rezolvă
integral la T0: treptele superioare nu se execută, mecanismul original nu e
niciodată exercitat, iar raportul de divulgare iese spectaculos fiindcă nimic
n-a avut nevoie să urce.

Un experiment care nu poate eșua nu e o măsurătoare. Corpusul se proiectează
deci **înapoi, de la criteriile de infirmare**: fiecare alegere de mai jos
răspunde la întrebarea „ce compoziție ar da protocolului șansa reală să
piardă?".

### 1.1 Ce nu se face niciodată

**Nicio proporție din acest document nu se ajustează după ce se văd
rezultatele.** Un strat de stres mărit fiindcă „banda nu se declanșează destul"
înseamnă un corpus ales ca să iasă cifra.

Proporțiile se fixează în intrarea de tip `masuratoare` din jurnal, înainte de
rulare, sub aceeași disciplină de pre-înregistrare ca restul montajului.
Variantele se rulează ca **experimente separate**, fiecare cu eticheta lui de
rulare (`METRICS.md` §9), și se raportează toate — nu doar cea care iese bine.

---

## 2. Cele două straturi

| Strat | Ce conține | Ce susține |
|---|---|---|
| **realist** — 80% | ce se află pe o mașină obișnuită | afirmația principală: costul marginal per endpoint (`METRICS.md` §3.1) |
| **stres** — 20% | ce sistemul chiar trebuie să analizeze | frontiera și divergența (`METRICS.md` §4.3) |

**De ce nu unul singur.** Numai realist: aproape tot se închide la T0, banda nu
se declanșează, cifra arată bine și nu demonstrează nimic. Numai stres: totul
escaladează, economia dispare, iar rezultatul descrie o mașină care nu există.

**De ce 80/20 și nu altceva.** Proporția nu decide „ce e adevărat", ci care
afirmație e mai bine susținută. 80% e aproape de o mașină reală — nu identic,
unde ar fi peste 95%, dar destul de aproape încât compoziția să nu poată fi
acuzată că a fost aleasă în favoarea protocolului. 20% dă suficiente fișiere
escaladate cât să se traseze o curbă, nu câteva puncte.

### 2.1 Dimensiunea minimă, care rezultă din altceva

`METRICS.md` §4.3 cere cel puțin **300 de mostre malițioase** pentru a putea
susține o rată sub 1% la zero ratări observate (regula 3/N). Mostrele
malițioase trăiesc în stratul de stres.

Cu stresul la 20%, corpusul total nu poate coborî sub **~1500 de fișiere**.
Cifra nu e o preferință: e consecința aritmetică a unei cerințe statistice
decise anterior.

---

## 3. Stratul realist — de unde vin fișierele benigne

Din ~1200 de fișiere:

| Sursă | Pondere | Ce aduce |
|---|---|---|
| binare dintr-o instalare curată de Windows | ~60% | masa cunoscută, prezentă în RDS |
| instalatoare legitime descărcate oficial | ~25% | fișiere mari, amestec natural cunoscut/necunoscut |
| artefacte compilate proaspăt | ~15% | **benigne și necunoscute în același timp** |

### 3.1 De ce a treia categorie nu e opțională

Fără ea, corpusul are o proprietate ascunsă și fatală: **tot ce e necunoscut e
malițios, tot ce e cunoscut e curat.**

Banda de incertitudine ar învăța din date o regulă falsă — „necunoscut =
periculos" — iar rezultatele ar arăta excelent, fiindcă regula e perfectă pe
acel corpus și pe niciunul real. Cazul obișnuit într-o organizație e exact
opusul: fiecare build intern, fiecare script, fiecare unealtă proprie e un
fișier benign pe care nu-l cunoaște nimeni.

Cele ~180 de artefacte sunt puține ca număr și singurele care testează cazul
greu.

### 3.2 Cum se obțin ieftin

Nu e nevoie de 180 de programe distincte. Câteva programe compilate în variante
— cu și fără optimizări, cu și fără simboluri de depanare, cu șiruri diferite —
produc tot atâtea hash-uri distincte, la fel de necunoscute.

Beneficiu secundar: variantele apropiate ale aceluiași program testează dacă
vectorul de trăsături de la T1 le vede ca fiind similare, ceea ce e exact ce ar
trebui să facă.

---

## 4. Distribuția de dimensiuni

| Palier | Număr | De ce |
|---|---|---|
| sub 2 KB | ~50 | zona în care predicția din §L2.4 se verifică |
| 2 KB – 100 KB | ~300 | fișiere mici obișnuite |
| restul | ~1150 | distribuit natural, cum vine din surse |

### 4.1 De ce paliere atât de joase

Predicția pre-înregistrată spune că **sub un prag de dimensiune protocolul
transferă mai mult decât always-upload**: plicul plus vectorul de trăsături
depășesc fișierul însuși. Nu e un defect de implementare, e o proprietate a
oricărei scări de divulgare cu antet fix.

Din măsurătorile de până acum, un eveniment T0 are câteva sute de octeți, iar un
vector T1 va adăuga încă o sută. Pragul e deci undeva **sub 1 KB** — mult mai
jos decât sugerează intuiția. Fișiere de 100 KB nu-l ating; protocolul le bate
confortabil.

Cele ~50 de fișiere sub 2 KB sunt singurele care pot confirma sau infirma
predicția. Fără ele, coada distribuției de la `METRICS.md` §3.2 nu va arăta
niciodată ce s-a anunțat.

### 4.2 Limita de sus a acestei alegeri

~50 din ~1500 înseamnă ~3%, plauzibil pe o mașină reală. Umplerea corpusului cu
fișiere minuscule ca să iasă predicția spectaculos ar fi trucare: 300 de
fișiere sub 2 KB nu descriu niciun sistem real.

---

## 5. Etichetarea adevărului de bază

### 5.1 Sursele

| Sursă | Ce dă | Rol |
|---|---|---|
| MalwareBazaar (abuse.ch) | **fișiere** + etichetă | sursa primară a stratului malițios |
| VirusTotal | verdicte pe hash | verificare independentă, parțială |
| liste publice de MD5 (VirusShare) | doar hash-uri MD5 | a treia confirmare, gratuită |

**MalwareBazaar** oferă cheie gratuită și loturi orare/zilnice, sub principiul
fair use; utilizarea academică intră clar acolo.

**VirusTotal** nu livrează fișiere pe planul gratuit, iar limitele de rată fac
imposibilă acoperirea integrală a 1500 de hash-uri într-un timp rezonabil.
Verificarea va fi deci **parțială, iar fracțiunea se declară** — „din 1500 de
fișiere, N verificate suplimentar, M divergențe" e o afirmație onestă și mai
puternică decât un consens pretins.

**Listele de MD5** sunt publice, fără cont — dar dau doar hash-uri MD5 ale
mostrelor din torrente, nu mostrele. Nu pot alimenta nici corpusul, nici baza de
reputație, care e pe SHA-256 și nu se poate deriva dintr-un MD5. Sunt utile
strict ca a treia opinie: calculând și MD5 pentru mostrele deja descărcate, o
potrivire înseamnă că două colecții independente consideră fișierul malware.

### 5.2 De ce eticheta sursei e suficientă

Pentru că **eticheta nu poartă teza**.

Oracolul (`METRICS.md` §4.1) e propriul motor alimentat cu fișierele integrale,
nu eticheta reală. Afirmația centrală compară protocolul cu always-upload, iar
ambele folosesc același motor: o etichetă greșită afectează identic cele două
părți și se anulează.

Eticheta contează numai pentru `METRICS.md` §5, care e **context, nu
afirmație**. Investiția în consens complet ar întări partea care nu e apărată
oricum, în detrimentul protocolului, care e.

### 5.3 Ce se declară obligatoriu

**Eticheta e a sursei, nu adevăr absolut.** MalwareBazaar acceptă contribuții
din comunitate; un fals pozitiv de acolo devine fals pozitiv aici.

**„Curat" înseamnă „provenit dintr-o sursă de încredere și necunoscut ca
malițios"**, nu „dovedit curat". Un binar de sistem compromis nu ar fi prins de
metoda aceasta.

**Consecința e mărginită.** Ratele de la §5 pot fi ușor deplasate; afirmația
centrală nu, fiindcă nu depinde de etichetă.

### 5.4 Capcana RDS — „cunoscut" nu înseamnă „bun"

NIST avertizează explicit că RDS conține valori de hash ale unor aplicații care
pot fi considerate malițioase. RDS e o listă de **software cunoscut și
trasabil**, nu de software bun.

Consecința e directă și trebuie fixată înainte să existe cod care o folosește:
**potrivirea în RDS nu poate produce verdictul „curat".** Ar deschide o cale
prin care un fișier malițios primește verdict curat la T0 — o ratare generată de
proiectare, nu de calibrare, iar `METRICS.md` §4.3 fixează pragul ratărilor la
zero.

Potrivirea în RDS poate însemna cel mult „cunoscut, deci nu necesită escaladare
din motiv de noutate" — o afirmație mult mai slabă.

Eticheta „curat" din corpus vine din **proveniență**: fișierul a fost luat
dintr-o instalare curată sau de pe situl oficial al producătorului. E o
afirmație pe care o controlezi, spre deosebire de apartenența la o listă.

---

## 6. Distribuția pe endpoint-uri

**70% comun tuturor mașinilor, 30% unic per mașină.**

### 6.1 De ce e parametrul care decide afirmația principală

Afirmația spune că al `N`-lea endpoint costă mai puțin decât primul. Motivul e
că serverul a văzut deja fișierele lui de la mașinile anterioare.

Dacă fiecare mașină ar avea fișiere complet diferite, serverul n-ar ști nimic
dinainte, al douăzecilea endpoint ar costa exact cât primul, **curba ar fi
plată și afirmația principală ar fi falsă**. Suprapunerea nu e detaliu de
montaj: e mecanismul.

### 6.2 De ce nu mai mult

Tocmai fiindcă e mecanismul, e ușor de trucat. Cu suprapunere totală, al doilea
endpoint ar costa aproape zero și curba ar arăta spectaculos — descriind un parc
de mașini identice, care nu există.

Într-o organizație reală, proporția de fișiere comune e probabil peste 85%.
Alegerea de 70% e deliberat **conservatoare**: curba tot coboară vizibil, iar
rezultatul nu poate fi acuzat că s-a sprijinit pe o suprapunere umflată. Un
rezultat obținut sub ipoteze mai grele e mai puternic, nu mai slab.

### 6.3 Unde stă stratul de stres

Malware-ul nu se distribuie uniform: prezent pe toate mașinile ar descrie o
infecție de parc, un caz special. Realist e ca mostrele malițioase să apară pe
una sau două mașini, deci stratul de stres e **preponderent unic**.

Consecința: cele 30% unice sunt disproporționat interesante. Acolo stau
necunoscutele și malware-ul, deci acolo se declanșează banda. Suprapunerea de
70% e masa ieftină; restul e unde se măsoară de fapt protocolul.

### 6.4 Prevalența nu poate fi binară

Împărțirea din §6 — comun tuturor sau unic pe o mașină — pare completă, dar are
o consecință care nu se vede până nu se numără: **prevalența ia exact două
valori.** Un fișier apare fie pe o mașină, fie pe toate.

Asta desființează, tăcut, jumătate din ce se pretinde. Deduplicarea și
escaladarea conștientă de prevalență sunt afirmații despre o *relație*: cu cât
un fișier e mai răspândit, cu atât costul lui marginal e mai mic. O relație
măsurată în două puncte e o dreaptă trasată prin ele — nu se poate distinge de
oricare altă formă care trece prin aceleași două puncte.

Cazul lipsă e și cel mai interesant: fișierul văzut pe două-trei mașini. Acolo
mecanismul chiar decide ceva. La o mașină nu are ce economisi; la toate,
economia e evidentă și neinteresantă.

**Cele 30% unice se distribuie deci pe 1-3 mașini, după o formă declarată:**

| Strat | pe 1 mașină | pe 2 | pe 3 |
|---|---|---|---|
| benign unic | 70% | 20% | 10% |
| malițios unic | 85% | 12% | 3% |

Proporția 70/30 din §6 rămâne neschimbată — ea se referă la **numărul de
fișiere**, nu la numărul de plasări. Ce se schimbă e că suprapunerea se declară
de acum în două feluri: câte fișiere sunt comune și câte plasări totale există
în parc.

**De ce malițiosul are altă formă decât benignul.** Un fișier benign prezent pe
două-trei mașini din cinci e banal: aceeași aplicație instalată de câțiva
oameni. Un fișier malițios pe trei din cinci mașini e o infecție de parc — cazul
special pe care §6.3 îl exclude tocmai fiindcă nu descrie o organizație
obișnuită. Coada de 3% există ca acel caz să apară totuși, o dată, ca să poată fi
arătat; nu ca să fie tipic.

**Ce susține și ce nu susține alegerea asta.** Susține demonstrarea
mecanismului: la a doua apariție a aceluiași hash, serverul cere mai puțin, iar
diferența e măsurabilă. Nu susține o rată și nici o curbă netedă — zece fișiere
la prevalență 3 sunt un studiu de caz, nu o statistică, și se raportează ca
atare. Un grafic „economie în funcție de prevalență" trasat prin patru puncte,
dintre care unul se sprijină pe zece fișiere, trebuie să arate câte fișiere stau
sub fiecare punct.

### 6.5 Ordinea de adăugare

`METRICS.md` §3.1 cere ca endpoint-urile să fie adăugate **eșalonat, cu ordinea
fixată înainte de măsurătoare**. Pornite simultan, costul marginal nu se poate
atribui.

Cerința e de montaj, nu de corpus, și tocmai de aceea e ușor de uitat.

---

## 7. Manifestul

Mostrele de malware nu se pot publica — nici în lucrare, nici în repo. Se poate
publica însă **manifestul**: lista fișierelor din corpus, prin hash.

Coloane minime:

| Coloană | De ce |
|---|---|
| `sha256` | identitatea de conținut, aceeași pe care o folosește protocolul |
| `md5` | pentru verificarea în listele publice (§5.1) |
| `size_bytes` | reconstituie distribuția din §4 |
| `label` | `malicious` sau `benign` |
| `label_source` | de unde vine eticheta |
| `stratum` | `realist` sau `stres` |
| `origin` | sistem / instalator / compilat / MalwareBazaar |
| `endpoints` | pe ce mașini apare — reconstituie suprapunerea din §6 |
| `vt_checked`, `vt_detections` | verificarea parțială din §5.1 |
| `vs_md5_present` | a treia confirmare |

**Manifestul e ce face afirmația verificabilă de altcineva.** Cine vrea să
reproducă rezultatul ia lista de hash-uri, își reconstituie corpusul din
aceleași surse și rulează. Fără el, „am testat pe 1500 de fișiere" rămâne o
afirmație pe încredere.

Lanțul e complet: intrarea `masuratoare` din jurnal poartă eticheta rulării,
eticheta identifică datele în serverul de măsurare, manifestul identifică ce a
intrat în corpus.

---

## 8. Manipularea mostrelor

Documentul acesta descrie descărcarea de malware real pe o mașină de lucru.
Regulile de mai jos nu sunt formalități.

**Nu se execută nimic, niciodată.** Toată analiza e statică prin proiectare;
nicio mostră nu trebuie rulată pentru niciun pas al lucrării.

**Mostrele stau într-un director dedicat, în afara oricărui repo**, iar
directorul nu se sincronizează în cloud și nu intră în backup-uri partajate.
Regulile din `.gitignore` sunt a doua linie de apărare, nu prima.

**Mostrele se păstrează neutralizate cât timp nu sunt analizate** — arhivate cu
parolă sau cu extensia schimbată — ca un dublu-clic accidental sau un scanner
automat să nu le atingă.

**Antivirusul mașinii va reacționa.** O excepție pe directorul de mostre e
necesară pentru ca fișierele să nu fie șterse sub picioarele măsurătorii, dar e
o slăbire reală a apărării mașinii și trebuie tratată ca atare: excepție cât mai
îngustă, ridicată după terminarea lucrului.

**Ideal, pe o mașină separată sau într-o mașină virtuală** dedicată, fără acces
la restul rețelei. Dacă nu e posibil, limitarea se declară ca atare.

---

## 9. Ce se declară lângă orice cifră

Completează lista din `METRICS.md` §8, pentru partea pe care serverul nu o poate
produce:

- numărul total de fișiere și fracțiunea malițioasă
- proporția realist / stres
- distribuția de dimensiuni pe cele trei paliere din §4
- suprapunerea între endpoint-uri și ordinea de adăugare
- fracțiunea verificată la VirusTotal și numărul de divergențe
- identificatorul manifestului folosit