# Autentificarea API — contractul de antete

Document valabil pentru ambele repo-uri (`edr-agent`, `edr-server`). Cele două
exemplare trebuie să fie identice; sincronizarea lor este verificată de
`test_the_peer_repository_carries_the_same_auth_document`.

De ce aici și nu în `wire-contract.json`: acel fișier descrie **corpuri** —
modele, câmpuri, obligativitate. Autentificarea trăiește în **antete**, pe care
contractul de fir nu le modelează. Un `Authorization` n-ar fi avut unde să
încapă în el fără să-i schimbe rostul.

---

## 1. Antetele

| Antet | Cine îl trimite | Când |
|---|---|---|
| `X-Agent-Key` | agentul | la fiecare scriere, și la re-înregistrare |
| `X-Enrollment-Secret` | agentul | doar la prima înrolare |

Numele sunt definite în două locuri care trebuie să coincidă:
`edr-server/app/services/auth_service.py` și `edr-agent/services/transport.py`.

Deliberat **nu** `Authorization: Bearer`. Schema Bearer poartă în practică
semantică de token cu expirare și conținut verificabil prin semnătură. Aici e o
cheie opacă, de lungă durată, căutată într-un depozit — un nume propriu spune
adevărul despre mecanism.

---

## 2. Rutele

| Rută | Metodă | Cere | Verificare suplimentară |
|---|---|---|---|
| `/health` | GET | — | — |
| `/api/agents/register` | POST | secret de înrolare **sau** cheie de agent | cheia trebuie să fie a `agent_id`-ului cerut |
| `/api/agents/{id}/heartbeat` | POST | cheie de agent | cheia trebuie să fie a lui `{id}` |
| `/api/events` | POST | cheie de agent | cheia trebuie să fie a lui `agent_id` din corp |
| `/api/agents` | GET | **nimic** | gaură cunoscută, vezi §6 |
| `/api/events` | GET | **nimic** | gaură cunoscută, vezi §6 |

Verificarea „cheia trebuie să fie a lui X" este pasul fără de care restul nu
valorează mare lucru: altfel toți agenții sunt autentificați și oricare poate
scrie în numele oricui.

---

## 3. Fluxul

### Înrolare (o dată, la instalare)

1. Operatorul provizionează secretul de înrolare pe endpoint, în fișierul
   `enrollment_secret` (calea e configurabilă din `config.json`).
2. Agentul trimite `POST /api/agents/register` cu `X-Enrollment-Secret`.
3. Serverul emite o cheie proprie pentru acel `agent_id` și o întoarce în
   răspuns, în câmpul `agent_key`. **Este singurul moment din viața cheii în
   care valoarea în clar există** — serverul păstrează doar SHA-256.
4. Agentul scrie cheia în fișierul `agent_key`, cu permisiuni restrânse, apoi
   suprascrie și șterge fișierul cu secretul de înrolare.

### Re-înregistrare (repetat, normal)

Serverul are store volatil: la repornire pierde registrul de agenți și le cere
să se re-înregistreze (`directive.action == "reregister"`). Agentul nu mai are
secret de înrolare, deci se autentifică cu cheia lui. Serverul acceptă și **nu**
emite o cheie nouă — răspunsul nu conține `agent_key`.

Asta funcționează pentru că depozitul de chei este persistat separat de
registrul de agenți (`agent_keys.json`). Dacă cheile ar fi trăit în aceeași
memorie volatilă, prima repornire a serverului ar fi blocat tot parcul afară.

### Recuperare (cheia pierdută pe endpoint)

Se reprovizionează un secret de înrolare pe mașină. Următoarea înregistrare
emite o cheie nouă, iar cea veche devine invalidă în aceeași clipă.

---

## 4. Codurile de răspuns și ce face agentul cu ele

| Cod | Înseamnă | Excepția pe agent | Ce face agentul |
|---|---|---|---|
| 401 | identitate nerecunoscută | `AuthenticationError` | păstrează coada, backoff, alarmă progresivă |
| 403 | identitate acceptată, acțiune refuzată | `IdentityMismatchError` | păstrează coada, backoff, ERROR de la prima apariție |
| 404 | agent necunoscut de server | `AgentNotRegisteredError` | păstrează coada, se re-înregistrează |
| 409 | `agent_id` revendicat de altă mașină | `FatalTransportError` | abandonează înregistrarea |
| 422 | payload invalid | `FatalTransportError` | **aruncă evenimentul** (poison message) |

Distincția 401/403 față de 422 este esențială și a fost motivul principal
pentru care pasul acesta a cerut proiectare separată:

- **422 e o proprietate a mesajului.** Nu devine valid dacă îl mai trimiți o
  dată, deci păstrarea lui ar bloca la nesfârșit tot ce vine după el.
- **401 e o proprietate a relației** dintre agent și server la momentul cererii.
  Poate fi adevărat acum și fals peste zece minute — o rotație de cheie, un
  server restaurat din backup, o greșeală de deploy.

Înainte de acest pas, agentul le trata identic: orice 4xx în afară de
404/408/429 era `FatalTransportError`, iar `EventDispatcher` ștergea evenimentul
din spool. Cu autentificarea pornită, cinci minute de cheie greșită ar fi șters
ireversibil tot ce era în coadă. Spool-ul persistent, construit ca să garanteze
at-least-once, ar fi devenit at-most-once exact când ceva era stricat.

Consecința de securitate e și mai directă: dacă spargerea autentificării ar duce
la golirea cozii, atunci pe un endpoint compromis „strică cheia" ar deveni o
metodă de a face dovezile să dispară de la sine.

---

## 5. Alarma progresivă

Un eșec de autentificare nu e nici temporar ca o pană de rețea, nici definitiv
ca un payload invalid — **durata** e singura care le departajează. De aceea
escaladarea se face pe timp scurs, nu pe număr de încercări: încercările sunt
rărite de backoff exponențial, deci contorul lor descrie retry-urile, nu durata.

Praguri, configurabile în `config.json`:

| Cheie | Implicit | Rol |
|---|---|---|
| `auth_alarm_first_seconds` | 300 (~5 min) | primul semnal `CRITICAL` |
| `auth_alarm_repeat_seconds` | 1800 (~30 min) | repetare, cu adâncimea cozii |

Alarma raportează câte evenimente așteaptă pe disc — nu ca ornament: „eșuează de
32 de minute" e o informație de operare, dar „…și 4200 de evenimente așteaptă"
spune cât de urgent e și ce se pierde dacă endpoint-ul e reinstalat înainte de
reparație.

Ceasul folosit e monoton, nu de perete: un ceas de sistem corectat de NTP nu are
voie nici să declanșeze o alarmă instantaneu, nici să o amâne la nesfârșit.

---

## 6. Ce NU apără acest pas

Declarat explicit, nu presupus.

- **Nu există TLS.** `config.json` indică `http://`. O cheie care călătorește la
  fiecare cerere e vizibilă pentru cine ascultă rețeaua, iar conținutul
  evenimentelor circulă oricum în clar. Mecanismul apără împotriva unui client
  care nu are secretul, nu împotriva unui ascultător pe traseu. TLS e un pas
  separat, declarat pentru Etapa 6.
- **Rutele de citire sunt deschise.** `GET /api/agents` și `GET /api/events` nu
  cer nicio credențială. Analistul nu are încă un secret propriu — decizie
  luată explicit la închiderea proiectării Etapei 0. Ce garantează totuși
  proiectarea de acum: prin ele **nu se scurg credențiale**, pentru că depozitul
  de chei e separat de `agents_store`, iar cheile sunt stocate hash-uite.
- **Cheia nu e criptată la repaus** pe endpoint și nu e legată de mașină (fără
  DPAPI, fără TPM). E protejată doar prin permisiuni de fișier. Un administrator
  local o poate citi — dar el poate oricum citi memoria procesului agent, unde
  cheia trebuie să existe ca să poată fi trimisă. Granița e utilizatorul local
  obișnuit, nu administratorul.
- **Secretul de înrolare e comun pe parc** cât timp e provizionat. Cine îl are
  poate cere o cheie pentru orice `agent_id`. Problema nu dispare, se mută
  într-un loc mult mai mic: un secret folosit o dată la instalare, în loc de
  unul care circulă la fiecare eveniment. De aceea ștergerea lui după prima
  folosire reușită nu e igienă, ci reducerea ferestrei de expunere de
  „permanent" la „durata instalării".
- **Nu există anti-replay.** Fără semnătură pe payload, o cerere capturată poate
  fi retrimisă. `agent_instance_id` face replay-ul inofensiv pentru detecția de
  repornire, dar nu imposibil.

---

## 7. Operare

Secretul de înrolare al serverului, în ordinea de precedență:

1. variabila de mediu `EDR_ENROLLMENT_SECRET`;
2. fișierul `enrollment_secret.txt` din rădăcina serverului;
3. dacă nu există niciunul, serverul generează unul și îl scrie în fișier,
   logând **calea**, niciodată valoarea.

Nu există mod „fără secret": un server care ar accepta înrolări neautentificate
când configurarea lipsește ar transforma o omisiune de operator într-o gaură
tăcută.

Rotația și revocarea sunt operații explicite, făcute direct pe server:

```python
from app.services import auth_service

auth_service.revoke_agent_key("endpoint-01")   # invalidează imediat
auth_service.list_key_records()                # amprente, nu chei
```

Fișiere care nu au ce căuta în git sau în backup-uri partajate:
`enrollment_secret.txt`, `agent_keys.json` (server), `agent_key`,
`enrollment_secret` (agent).
