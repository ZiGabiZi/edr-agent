"""
Credențialele agentului — secretul de înrolare și cheia de API.
===============================================================

Două secrete, cu vieți foarte diferite:

    enrollment_secret — comun pe parc, provizionat la instalare, folosit exact
        o dată: la prima înregistrare, în schimbul unei chei proprii. Se șterge
        imediat după. Cât timp există pe disc, e o capacitate permanentă de a
        cere serverului o cheie nouă pentru orice agent_id — deci ștergerea nu
        e igienă, e reducerea ferestrei de expunere de „permanent" la „durata
        instalării".

    agent_key — unică per mașină, de lungă durată, trimisă la fiecare scriere.
        Compromiterea unui endpoint dă acces în numele acelui endpoint, nu al
        parcului, iar revocarea atinge o singură mașină.

De ce fișiere separate de config.json:
    config.json e citit de operator, comparat între mașini, lipit în tichete și
    urcat în repo. Un secret în el ar călători odată cu el. Separat, fișierul de
    cheie are propriile permisiuni și propriul motiv de a nu fi copiat nicăieri.

Limitare declarată explicit pentru Etapa 0:
    Cheia stă în clar pe disc, protejată doar prin permisiuni de fișier. Nu e
    criptată la repaus și nu e legată de mașină (DPAPI, TPM). Un atacator cu
    drepturi de administrator local o poate citi — dar un astfel de atacator
    poate oricum citi memoria procesului agent, unde cheia trebuie să existe ca
    să poată fi trimisă. Permisiunile apără împotriva unui utilizator local
    obișnuit, nu împotriva administratorului mașinii; asta e granița, și se
    spune, nu se sugerează.
"""

import getpass
import logging
import os
import platform
import subprocess
import threading
from pathlib import Path
from typing import Optional


_BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_AGENT_KEY_PATH = _BASE_DIR / "agent_key"
DEFAULT_ENROLLMENT_SECRET_PATH = _BASE_DIR / "enrollment_secret"

# SID-uri bine cunoscute, folosite în locul numelor. Pe un Windows românesc
# grupul se numește „Administratori", iar un icacls cu numele englezesc ar
# eșua tăcut pe exact mașinile pe care rulează agentul.
_WELL_KNOWN_SYSTEM_SID = "*S-1-5-18"
_WELL_KNOWN_ADMINISTRATORS_SID = "*S-1-5-32-544"


class AgentCredentialsError(Exception):
    """Eroare la citirea sau scrierea unui fișier de credențiale."""


class AgentCredentials:
    """
    Deține credențialele rulării curente și le sincronizează cu discul.

    Obiectul e partajat între firele agentului (bucla de heartbeat, dispatcher-ul
    de evenimente, înregistrarea), pentru că o cheie reemisă la o re-înrolare
    trebuie să fie văzută imediat de toate. De aceea accesul e sub lock, iar
    consumatorii citesc proprietatea la fiecare cerere, nu o dată la pornire.
    """

    def __init__(
        self,
        agent_key_path: Optional[Path] = None,
        enrollment_secret_path: Optional[Path] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.agent_key_path = Path(agent_key_path or DEFAULT_AGENT_KEY_PATH)
        self.enrollment_secret_path = Path(
            enrollment_secret_path or DEFAULT_ENROLLMENT_SECRET_PATH
        )
        self.logger = logger or logging.getLogger(__name__)

        self._lock = threading.Lock()
        self._agent_key: Optional[str] = None
        self._enrollment_secret: Optional[str] = None

    # -- citire ------------------------------------------------------------

    @property
    def agent_key(self) -> Optional[str]:
        with self._lock:
            return self._agent_key

    @property
    def enrollment_secret(self) -> Optional[str]:
        with self._lock:
            return self._enrollment_secret

    @property
    def has_any_credential(self) -> bool:
        with self._lock:
            return bool(self._agent_key or self._enrollment_secret)

    def load(self) -> None:
        """
        Citește ambele fișiere. Absența oricăruia NU e o eroare.

        Un agent proaspăt instalat are doar secretul de înrolare; unul deja
        înrolat are doar cheia. Amândouă lipsă e o stare validă de citit, dar
        invalidă de rulat — cine o constată decide ce face (agent.py loghează
        și continuă să încerce, ca să nu oprească monitorizarea locală).
        """
        agent_key = self._read_secret_file(self.agent_key_path, "agent key")
        enrollment_secret = self._read_secret_file(
            self.enrollment_secret_path, "enrollment secret"
        )

        with self._lock:
            self._agent_key = agent_key
            self._enrollment_secret = enrollment_secret

        if agent_key:
            self.logger.info("Agent API key loaded from %s.", self.agent_key_path)
        elif enrollment_secret:
            self.logger.info(
                "No agent API key yet; will enroll using the enrollment secret at %s.",
                self.enrollment_secret_path,
            )
        else:
            self.logger.error(
                "No credentials found: neither %s nor %s exists. The server will "
                "refuse every write until one of them is provisioned. Events keep "
                "accumulating in the local spool in the meantime.",
                self.agent_key_path,
                self.enrollment_secret_path,
            )

    def refresh_from_disk(self) -> bool:
        """
        Recitește fișierele și adoptă ce găsește, fără să șteargă nimic.

        De ce e nevoie de ea, deși load() rulează deja la pornire:
            Agentul nu abandonează la un eșec de autentificare tocmai ca
            operatorul să poată repara problema, iar agentul să-și revină
            singur. Dar când reparația ESTE punerea fișierului la locul lui —
            cazul cel mai frecvent, un endpoint proaspăt instalat fără secret —
            un agent care citește credențialele o singură dată nu o observă
            niciodată. Bucla reîncearcă la infinit exact în scenariul pentru
            care a fost proiectată, iar singura ieșire e repornirea procesului.

        Regula e ADOPTĂ, nu sincroniza. O valoare nevidă de pe disc înlocuiește
        ce e în memorie; o valoare absentă NU golește memoria. Diferența e
        esențială: adopt_agent_key() păstrează cheia în memorie chiar dacă
        scrierea pe disc a eșuat, iar o sincronizare oarbă ar fi aruncat singura
        copie funcțională a credențialei la prima recitire — transformând o
        eroare de disc recuperabilă într-un agent mut.

        Se apelează doar după un eșec de autentificare, deci de cel mult câteva
        ori pe minut: încercările sunt distanțate de backoff exponențial.

        Returns:
            True dacă vreo valoare s-a schimbat. Apelantul decide dacă loghează,
            ca să nu producă o linie la fiecare reîncercare.
        """
        agent_key = self._read_secret_file(self.agent_key_path, "agent key")
        enrollment_secret = self._read_secret_file(
            self.enrollment_secret_path, "enrollment secret"
        )

        with self._lock:
            changed = False

            if agent_key and agent_key != self._agent_key:
                self._agent_key = agent_key
                changed = True

            if enrollment_secret and enrollment_secret != self._enrollment_secret:
                self._enrollment_secret = enrollment_secret
                changed = True

            return changed

    def _read_secret_file(self, path: Path, description: str) -> Optional[str]:
        """
        Citește un secret dintr-un fișier provizionat de un om, pe Windows.

        utf-8-sig, nu utf-8: fiecare unealtă obișnuită de pe Windows scrie
        UTF-8 CU BOM. Set-Content -Encoding utf8 din PowerShell 5.1, Notepad,
        redirectarea în fișier — toate pun EF BB BF la început. Citit ca utf-8
        curat, BOM-ul devine caracterul \\ufeff la începutul secretului, iar
        .strip() NU îl elimină: nu e spațiu alb, e un caracter de format.

        Ce urmează e mult mai rău decât un secret greșit. Valoarea ajunge
        într-un antet HTTP, iar http.client codează antetele ca latin-1, unde
        \\ufeff nu există. Rezultatul e un UnicodeEncodeError aruncat din
        adâncul lui urllib3, care nu e nici TransportError, nici ConfigError,
        deci trece prin toate ramurile de tratare a erorilor și oprește agentul
        cu un traceback despre codecuri. Cauza reală — un fișier salvat cu BOM
        — nu apare nicăieri în el.

        Aceeași convenție e deja folosită de încărcătorul de contract al
        serverului, din exact același motiv.
        """
        if not path.exists():
            return None

        try:
            content = path.read_text(encoding="utf-8-sig")
        except OSError as error:
            self.logger.error("Could not read the %s at %s: %s", description, path, error)
            return None
        except UnicodeDecodeError as error:
            # Tipic: fișier salvat ca UTF-16 (Out-File fără -Encoding pe unele
            # versiuni de PowerShell). Nu poate fi reparat aici prin ghicit.
            self.logger.error(
                "The %s at %s is not valid UTF-8 (%s). Save it as plain ASCII "
                "text, without a byte order mark.",
                description,
                path,
                error,
            )
            return None

        # Plasă pentru BOM-uri care nu stau chiar pe prima poziție: un fișier
        # editat de două ori cu unelte diferite poate purta unul după un spațiu.
        content = content.replace("﻿", "").strip()

        if not content:
            # Fișier gol = secret consumat, nu secret absent. Distincția nu
            # schimbă comportamentul, dar schimbă ce caută operatorul în log.
            self.logger.info(
                "The %s file at %s is empty (already consumed).", description, path
            )
            return None

        # Ultima gardă, înaintea rețelei. Antetele HTTP se codează latin-1
        # (http.client), deci orice caracter în afara lui ar exploda abia în
        # urllib3, cu un traceback care nu numește nici fișierul, nici cauza.
        # Aici eșecul e localizat: numele fișierului, motivul, și ce trebuie
        # făcut. Agentul continuă fără credențială — adică se comportă exact ca
        # unul neînrolat: reîncearcă, escaladează, și NU pierde evenimente.
        try:
            content.encode("latin-1")
        except UnicodeEncodeError:
            self.logger.error(
                "The %s at %s contains characters that cannot be sent in an HTTP "
                "header. The file was most likely saved with a byte order mark or "
                "in an encoding other than ASCII. Rewrite it as plain ASCII text; "
                "until then the agent has no usable credential.",
                description,
                path,
            )
            return None

        return content

    # -- scriere -----------------------------------------------------------

    def adopt_agent_key(self, agent_key: str) -> bool:
        """
        Preia cheia emisă de server: o ține în memorie și o scrie pe disc.

        Ordinea e memorie-întâi, deliberat. Dacă scrierea pe disc eșuează,
        agentul poate funcționa complet pe durata acestei rulări și doar va
        trebui să se reînroleze la următoarea pornire. Invers — disc întâi și
        abandon la eroare — ar transforma un disc plin într-un agent mut.

        Reînrolarea aceea nu e însă gratuită: are nevoie de secretul de
        înrolare, pe care apelantul îl șterge imediat după. De aceea eșecul se
        RAPORTEAZĂ, nu doar se loghează. Cât timp funcția returna None pe
        ambele căi, apelantul arunca singura cale de recuperare exact în cazul
        în care ea devenea necesară, iar promisiunea din logul de mai jos era
        falsă: agentul repornea fără cheie și fără secret, adică mut.

        Returns:
            True dacă cheia a ajuns pe disc și supraviețuiește repornirii.
            False dacă trăiește doar în memoria acestui proces.
        """
        with self._lock:
            self._agent_key = agent_key

        try:
            self._write_secret_file(self.agent_key_path, agent_key)
            self.logger.info("Agent API key stored at %s.", self.agent_key_path)
            return True
        except AgentCredentialsError as error:
            self.logger.error(
                "%s The key is held in memory for this run only; the agent will "
                "need to enroll again after a restart.",
                error,
            )
            return False

    def consume_enrollment_secret(self) -> None:
        """
        Șterge secretul de înrolare după prima folosire reușită.

        Se suprascrie conținutul înainte de ștergere. E o măsură best-effort și
        se declară ca atare: pe NTFS sau pe un SSD cu wear leveling, blocurile
        vechi pot supraviețui suprascrierii logice. Ce garantează pasul ăsta e
        că secretul nu mai e accesibil prin citirea normală a fișierului — nu că
        a dispărut fizic de pe suport.
        """
        with self._lock:
            had_secret = self._enrollment_secret is not None
            self._enrollment_secret = None

        path = self.enrollment_secret_path

        if not path.exists():
            return

        try:
            length = max(path.stat().st_size, 1)

            with path.open("r+b") as file:
                file.write(b"\x00" * length)
                file.flush()
                os.fsync(file.fileno())

            path.unlink()
        except OSError as error:
            self.logger.error(
                "Could not remove the enrollment secret at %s: %s. It remains a "
                "standing capability to request a new key for any agent_id; delete "
                "it manually.",
                path,
                error,
            )
            return

        if had_secret:
            self.logger.info(
                "Enrollment secret consumed and removed from %s.", path
            )

    def _write_secret_file(self, path: Path, content: str) -> None:
        """
        Scrie un secret cu permisiuni restrânse, fără fereastră de expunere.

        Fișierul temporar e creat ȘI restricționat înainte să primească
        conținutul, apoi mutat peste destinație. Scrierea directă ar lăsa
        secretul, pentru o clipă, într-un fișier cu permisiunile moștenite de
        la director — scurt, dar suficient pe o mașină cu mai mulți utilizatori.
        """
        temporary_path = path.with_name(path.name + ".tmp")

        try:
            path.parent.mkdir(parents=True, exist_ok=True)

            descriptor = os.open(
                str(temporary_path),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(content + "\n")
                file.flush()
                os.fsync(file.fileno())

            self._restrict_permissions(temporary_path)
            os.replace(temporary_path, path)
        except (OSError, ValueError) as error:
            # ValueError pe langa OSError: calea vine din config.json prin
            # agent_key_path, iar un octet nul acolo face ca os.open sa ridice
            # ValueError inainte sa atinga discul. Netratata, ar fi iesit din
            # adopt_agent_key si ar fi oprit agentul chiar in clipa in care
            # inrolarea reusise.
            try:
                temporary_path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass

            raise AgentCredentialsError(
                f"Could not write the credential file {path}: {error}"
            ) from error

    def _restrict_permissions(self, path: Path) -> None:
        """
        Restrânge accesul la fișier: contul care rulează agentul, SYSTEM și
        administratorii locali. Nimeni altcineva.

        Pe Windows os.chmod nu face decât să comute atributul read-only — nu e
        un control de acces. ACL-ul real se pune cu icacls:
            /inheritance:r  taie drepturile moștenite de la director (unde de
                            regulă „Users" are citire);
            /grant:r        înlocuiește, nu adaugă, ca o rulare repetată să nu
                            acumuleze intrări.

        Un eșec se loghează la ERROR și nu oprește nimic: o cheie cu permisiuni
        prea largi e o slăbiciune, un agent care refuză să pornească e o gaură
        de monitorizare.
        """
        if platform.system() != "Windows":
            try:
                os.chmod(path, 0o600)
            except OSError as error:
                self.logger.error(
                    "Could not restrict permissions on %s: %s", path, error
                )
            return

        try:
            account = getpass.getuser()
        except Exception:  # pragma: no cover — mediu fără utilizator identificabil
            account = os.environ.get("USERNAME", "")

        if not account:
            self.logger.error(
                "Could not determine the current Windows account; leaving inherited "
                "permissions on %s.",
                path,
            )
            return

        command = [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{_WELL_KNOWN_SYSTEM_SID}:(F)",
            "/grant:r",
            f"{_WELL_KNOWN_ADMINISTRATORS_SID}:(F)",
            "/grant:r",
            f"{account}:(R,W)",
        ]

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            self.logger.error(
                "Could not run icacls to restrict %s: %s. The file keeps the "
                "permissions inherited from its directory.",
                path,
                error,
            )
            return

        if result.returncode != 0:
            self.logger.error(
                "icacls failed to restrict %s (exit %d): %s. The file keeps the "
                "permissions inherited from its directory.",
                path,
                result.returncode,
                (result.stderr or result.stdout or "").strip(),
            )
