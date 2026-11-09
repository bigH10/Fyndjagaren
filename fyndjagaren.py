"""
Fyndjägaren – en AI-auktionsexpert som bevakar Tradera åt dig.

Varje körning (var 15:e minut via GitHub Actions) gör skriptet så här:
  1. Hämtar auktionerna i dina bevakningar som slutar inom 45 minuter.
  2. Filtrerar bort skräp (reservdelar, tillbehör, kopior, annonser utan bild, dåliga säljare).
  3. Låter Claude snabbt sålla fram de mest lovande kandidaterna.
  4. Hämtar Traderas prisförslag (vad liknande saker brukar säljas för).
  5. Låter Claude göra en noggrann expertbedömning med bild, beskrivning och frakt.
  6. Sparar fynden och skickar dem som push-notis (via ntfy) när det är högst 25 minuter kvar,
     med uppdaterat pris. Fynd vars pris redan har passerat maxbudet skickas inte.

Eftersom auktionerna bedöms i god tid kommer tipset fram även om GitHub startar en körning sent.
AI:n körs via Claude Code och ditt Claude-abonnemang – inga API-krediter behövs.
Skriptet budar aldrig – det tipsar bara. Du bestämmer och budar själv.
"""

import html
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# ════════════════════════════════════════════════════════════════════
#  INSTÄLLNINGAR – ändra gärna här
# ════════════════════════════════════════════════════════════════════

# Berätta för AI:n vad du är ute efter. Skriv fritt på svenska.
OM_MIG = """
Jag letar efter riktiga fynd inom mina bevakningar: saker som går att köpa klart under vad de
är värda – för att använda själv eller sälja vidare med vinst.
"""

# ─── Bevakningar: det här letar Fyndjägaren efter ──────────────────
# Varje bevakning söker antingen i en huvudkategori på Tradera ("kategori") eller på sökord i
# alla kategorier ("sokord"). Sökord med flera ord, t.ex. "stone island", måste finnas med
# alla ord i titeln.
#   "uteslut_kategori": underkategorier som hoppas över (huvudkategorins namn räknas inte)
#   "uteslut_ord":      ord i titeln som gör att annonsen hoppas över
#   "for_ai":           vad AI:n ska leta efter inom bevakningen
BEVAKNINGAR = [
    {
        "namn": "Klockor",
        "kategori": "klockor",
        "uteslut_kategori": ["delar", "tillbehör"],
        "for_ai": "Armbandsur och andra klockor, särskilt kända märken (Rolex, Omega, Seiko, "
                  "Longines, Tissot, TAG Heuer m.fl.) och vintageur. Var vaksam mot kopior.",
    },
    {
        "namn": "Pokémonkort",
        "sokord": ["pokemon", "pokémon"],
        "uteslut_ord": ["gosedjur", "mjukis", "plysch", "figur", "t-shirt", "tröja", "pyjamas",
                        "mössa", "keps", "ryggsäck", "lampa", "poster", "nintendo", "gameboy",
                        "game boy", "switch", "3ds"],
        "for_ai": "Bara Pokémon-samlarkort: singelkort, samlingar, boosters, displayer och "
                  "graderade kort (PSA, BGS). Värdet beror på kort, utgåva, skick och gradering.",
    },
    {
        "namn": "Vin",
        "sokord": ["vin", "viner", "vinglas", "vinkaraff", "karaff", "dekanter", "vinkyl",
                   "vinkylskåp", "vinkylare", "vinställ", "vinhylla", "vinlåda", "vinöppnare",
                   "korkskruv", "sommelier", "champagne", "champagneglas", "riedel", "zalto",
                   "coravin", "wine"],
        "for_ai": "Allt som har med vin att göra: vinglas (Riedel, Zalto, Orrefors m.fl.), "
                  "karaffer, vinkylar, korkskruvar, vinlitteratur och samlarobjekt.",
    },
    {
        "namn": "Apple",
        "sokord": ["apple", "iphone", "ipad", "macbook", "imac", "airpods", "ipod", "homepod"],
        "uteslut_ord": ["skal", "fodral", "laddare", "kabel", "skärmskydd", "hållare", "adapter",
                        "dekal"],
        "for_ai": "Apple-produkter: iPhone, iPad, Mac, AirPods, Apple Watch m.m. Kontrollera "
                  "modell, lagring, skick och att enheten inte är spärrad (iCloud/aktiveringslås).",
    },
    {
        "namn": "Fordon",
        "kategori": "fordon",
        "uteslut_kategori": ["delar", "tillbehör", "däck", "fälg", "vård", "verktyg",
                             "litteratur"],
        "uteslut_ord": ["reservdel"],
        "for_ai": "Bara hela fordon: bilar, motorcyklar, mopeder, båtar, husvagnar m.m. – inga "
                  "delar. Bedöm modell, årsmodell, miltal, skick och besiktning.",
    },
    {
        "namn": "Märkeskläder",
        "sokord": ["moncler", "vuitton", "stone island", "margiela", "ralph lauren", "burberry",
                   "lindeberg", "j.lindeberg", "hermes", "hermès", "loro piana", "faloni",
                   "tom ford", "saint laurent", "ysl"],
        "for_ai": "Kläder, väskor, skor och accessoarer från Moncler, Louis Vuitton, Stone "
                  "Island, Maison Margiela, Ralph Lauren, Burberry, J.Lindeberg, Hermès, Loro "
                  "Piana, Luca Faloni, Tom Ford och Saint Laurent. De här märkena förfalskas "
                  "ofta: var misstänksam mot för lågt pris, lånade bilder och säljare utan historik.",
    },
]

# ─── Filter för alla bevakningar ───────────────────────────────────
UTESLUT_ORD = ["defekt", "trasig", "kopia", "replika", "replica", "fake", "inspirerad", "inspired"]
MIN_PRIS_KR = 0             # lägsta nuvarande pris (0 = ingen gräns)
MAX_PRIS_KR = 0             # högsta nuvarande pris, din budget (0 = ingen gräns)
MIN_SALJARBETYG = 4.0       # säljarens betyg 0–5. Nya säljare utan betyg släpps igenom.
KRAV_BILD = True            # hoppa över annonser utan bild

# ─── Tider ─────────────────────────────────────────────────────────
# AI:n bedömer auktionerna i god tid och sparar fynden. Tipset skickas när det är högst
# TIPSA_NAR_KVAR_MINUTER kvar – med körning var 15:e minut normalt 10–25 minuter före slut.
BEDOM_MINUTER_FRAMAT = 45      # AI:n bedömer auktioner som slutar inom så här många minuter
TIPSA_NAR_KVAR_MINUTER = 25    # tipset skickas när det är högst så här många minuter kvar
TIPSA_MINST_KVAR_MINUTER = 2   # med mindre tid kvar än så skickas inget tips

# Tider (svensk tid) då AI:n letar nya fynd. Sparar på abonnemangets kvot. Redan hittade fynd
# skickas även utanför tiderna, och provkörningar du startar själv letar alltid.
AKTIV_FRAN_KL = 7
AKTIV_TILL_KL = 23

# ─── AI ────────────────────────────────────────────────────────────
# Modeller i Claude Code: "haiku" (sparar mest kvot), "sonnet" eller "opus" (bäst).
SALLNINGS_MODELL = "sonnet"   # steg 1: läser igenom alla auktioner som klarat filtren
EXPERT_MODELL = "opus"        # steg 2: noggrann bedömning av kandidaterna
SKICKA_BILDER_TILL_AI = True  # låt AI:n titta på annonsbilden i expertsteget
MAX_ANNONSER_TILL_AI = 200    # tak för hur många auktioner AI:n läser per körning
MAX_KANDIDATER = 8            # hur många AI:n får plocka ut för noggrann granskning
MAX_PRISKOLLAR = 3            # Traderas prisförslag tillåter 500 anrop/dygn → max 3 per körning
MIN_BETYG = 7                 # skicka bara fynd med minst detta betyg (1–10)
MAX_FYND_PER_NOTIS = 3        # fynd per notis (ntfy.sh tillåter 250 notiser/dygn)
PAUS_VID_KVOTSLUT_MIN = 30    # paus om abonnemangets gräns nås

# Tekniska gränser (behöver normalt inte ändras)
MAX_SIDOR_PER_SOKNING = 5   # 240 annonser per sida
MAX_SOKANROP = 80           # Tradera tillåter 10 000 sökningar/dygn → 80 per körning räcker gott
MINNESFIL = "sedda.json"    # minne över bedömda auktioner, väntande fynd och eventuell paus
BILDMAPP = "bilder"         # här sparas kandidaternas bilder tillfälligt

# ════════════════════════════════════════════════════════════════════

TRADERA_API = "https://api.tradera.com/v4"
STOCKHOLM = ZoneInfo("Europe/Stockholm")
BEVAKNING_PER_NAMN = {b["namn"]: b for b in BEVAKNINGAR}


class Stopp(Exception):
    """Ett fel med ett begripligt meddelande som avbryter körningen."""


class Kvotslut(Exception):
    """Abonnemangets användningsgräns är nådd."""


# ─── Hemligheter (läggs in som "Secrets" i GitHub) ─────────────────

def hamta_hemligheter():
    namn = ["TRADERA_APP_ID", "TRADERA_APP_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "NTFY_TOPIC"]
    saknas = [n for n in namn if not os.environ.get(n, "").strip()]
    if saknas:
        raise Stopp(
            "Följande hemligheter saknas: " + ", ".join(saknas) + ".\n"
            "Lägg in dem under Settings → Secrets and variables → Actions i GitHub "
            "(se guiden)."
        )
    return {n: os.environ[n].strip() for n in namn}


# ─── Tradera ───────────────────────────────────────────────────────

class Tradera:
    def __init__(self, app_id, app_key):
        self.session = requests.Session()
        self.session.headers.update({
            "X-App-Id": app_id,
            "X-App-Key": app_key,
            "Accept": "application/json",
        })
        self.antal_anrop = 0

    def _anropa(self, metod, sokvag, **kwargs):
        self.antal_anrop += 1
        svar = self.session.request(metod, TRADERA_API + sokvag, timeout=30, **kwargs)
        if svar.status_code == 401:
            raise Stopp("Tradera godkände inte TRADERA_APP_ID/TRADERA_APP_KEY. "
                        "Kontrollera att du kopierat dem rätt (se guiden).")
        if svar.status_code == 429:
            raise Stopp("Traderas dygnsgräns för anrop är nådd. Fyndjägaren fortsätter "
                        "automatiskt när gränsen återställs.")
        svar.raise_for_status()
        return svar.json()

    def get(self, sokvag, **params):
        return self._anropa("GET", sokvag, params=params)

    def post(self, sokvag, data):
        return self._anropa("POST", sokvag, json=data)


def tolka_tid(text):
    """Tolkar Traderas tidsstämplar. Tider utan tidszon antas vara svensk tid."""
    if not text:
        return None
    try:
        tid = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if tid.tzinfo is None:
        tid = tid.replace(tzinfo=STOCKHOLM)
    return tid.astimezone(timezone.utc)


def matchar(text, ordlista):
    text = (text or "").lower()
    return any(o.lower() in text for o in ordlista)


def hamta_kategorier(tradera):
    """Returnerar (huvudkategorier som [(id, namn)], {kategori-id: "Huvud > Under"})."""
    trad = tradera.get("/categories")
    vagar = {}

    def besok(kategori, forald):
        namn = kategori.get("name") or "?"
        vag = f"{forald} > {namn}" if forald else namn
        vagar[kategori["id"]] = vag
        for barn in kategori.get("childCategories") or []:
            besok(barn, vag)

    for kategori in trad:
        besok(kategori, "")
    return [(k["id"], k.get("name") or "?") for k in trad], vagar


def bygg_sokningar(huvudkategorier):
    """Gör om bevakningarna till sökningar hos Tradera: [(bevakning, sökvillkor)]."""
    sokningar = []
    for bevakning in BEVAKNINGAR:
        kategori_idn = [0]  # 0 = alla kategorier
        if bevakning.get("kategori"):
            traffar = [(i, n) for i, n in huvudkategorier if matchar(n, [bevakning["kategori"]])]
            # Matchar flera huvudkategorier (t.ex. "Fordon" och "Fordonsdelar & tillbehör"),
            # hoppa över de vars namn innehåller något av bevakningens uteslutna ord.
            utan = [(i, n) for i, n in traffar
                    if not matchar(n, bevakning.get("uteslut_kategori") or [])]
            if len(traffar) > 1 and utan:
                traffar = utan
            if not traffar:
                print(f"  ! Ingen huvudkategori matchar '{bevakning['kategori']}' "
                      f"({bevakning['namn']}).")
                continue
            print(f"  {bevakning['namn']}: söker i " + ", ".join(f'"{n}"' for _, n in traffar))
            kategori_idn = [i for i, _ in traffar]

        sokord = [o.strip() for o in bevakning.get("sokord") or [] if o.strip()]
        enkla = [o for o in sokord if " " not in o]
        flerord = [o for o in sokord if " " in o]
        villkor = []
        if enkla:
            villkor.append({"searchWords": " ".join(enkla), "mode": "Or"})
        villkor += [{"searchWords": fras, "mode": "And"} for fras in flerord]

        for kategori_id in kategori_idn:
            if not villkor and kategori_id:
                sokningar.append((bevakning, {"categoryId": kategori_id}))
            for v in villkor:
                sokningar.append((bevakning, {"categoryId": kategori_id, **v}))
    return sokningar


def hamta_auktioner(tradera, sokningar, nu, slut):
    """Kör sökningarna, sorterat på sluttid, och samlar auktioner som slutar mellan nu och slut."""
    hittade = {}
    for bevakning, villkor in sokningar:
        for sida in range(1, MAX_SIDOR_PER_SOKNING + 1):
            if tradera.antal_anrop >= MAX_SOKANROP:
                print(f"  ! Taket på {MAX_SOKANROP} sökningar nått – hoppar över resten.")
                return list(hittade.values())
            svar = tradera.post("/search/advanced", {
                **villkor,
                "itemStatus": "Active",
                "itemType": "Auction",
                "orderBy": "EndDateAscending",
                "itemsPerPage": 240,
                "pageNumber": sida,
            })
            for fel in svar.get("errors") or []:
                print(f"  ! Tradera ({bevakning['namn']}): {fel.get('code')} {fel.get('message')}")
            annonser = svar.get("items") or []
            for annons in annonser:
                slutar = tolka_tid(annons.get("endDate"))
                if (slutar and nu < slutar <= slut and not annons.get("isEnded")
                        and annons["id"] not in hittade):
                    annons["_slutar"] = slutar
                    annons["_bevakning"] = bevakning["namn"]
                    hittade[annons["id"]] = annons
            if len(annonser) < 240:
                break
            sista = tolka_tid(annonser[-1].get("endDate"))
            if sista is None or sista > slut:
                break
    return list(hittade.values())


def nuvarande_pris(annons):
    """Högsta bud om det finns bud, annars utropspriset."""
    if annons.get("hasBids") and annons.get("maxBid"):
        return annons["maxBid"]
    return annons.get("nextBid") or 0


def filtrera(annonser, vagar):
    """Tar bort skräp enligt filtren. Returnerar (kvar, antal bortfiltrerade per skäl)."""
    kvar, skal = [], Counter()
    for a in annonser:
        bevakning = BEVAKNING_PER_NAMN[a["_bevakning"]]
        titel = a.get("shortDescription") or ""
        pris = nuvarande_pris(a)
        saljarbetyg = a.get("sellerDsrAverage") or 0
        # Jämför bara underkategorierna – huvudkategorin heter t.ex. "Fordon, båtar & delar".
        underkategori = " > ".join(vagar.get(a.get("categoryId"), "").split(" > ")[1:])
        if matchar(underkategori, bevakning.get("uteslut_kategori") or []):
            skal["kategori"] += 1
        elif matchar(titel, UTESLUT_ORD + (bevakning.get("uteslut_ord") or [])):
            skal["ord i titeln"] += 1
        elif pris < MIN_PRIS_KR or (MAX_PRIS_KR and pris > MAX_PRIS_KR):
            skal["pris"] += 1
        elif saljarbetyg and saljarbetyg < MIN_SALJARBETYG:
            skal["säljarbetyg"] += 1
        elif KRAV_BILD and not (a.get("thumbnailLink") or a.get("imageLinks")):
            skal["ingen bild"] += 1
        else:
            kvar.append(a)
    return kvar, skal


def prisforslag(tradera, sokfras, kategori_id):
    """Traderas prisförslag: medianpriset för liknande sålda saker (eller None)."""
    sokfras = (sokfras or "").strip()[:100]
    if len(sokfras) < 3 or not kategori_id:
        return None
    try:
        svar = tradera.get("/pricing/suggestion", query=sokfras, categoryId=kategori_id)
    except requests.HTTPError as fel:
        print(f"  ! Prisförslag misslyckades för '{sokfras}': {fel}")
        return None
    return svar if svar.get("suggested") else None


def rensa_html(text, max_tecken=1200):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text[:max_tecken] + ("…" if len(text) > max_tecken else "")


# ─── Claude (via Claude Code och ditt abonnemang) ──────────────────

class Claude:
    def __init__(self):
        if not shutil.which("claude"):
            raise Stopp("Claude Code är inte installerat. Kontrollera steget "
                        "'Installera Claude Code' i arbetsflödet.")
        self.antal_anrop = 0

    def fraga(self, modell, system, fraga, schema, effort, verktyg=""):
        """Ställer en fråga och får tillbaka ett JSON-svar som följer schemat."""
        kommando = [
            "claude", "-p",
            "--model", modell,
            "--effort", effort,
            "--tools", verktyg,
            "--no-session-persistence",
            "--output-format", "json",
            "--system-prompt", system,
            "--json-schema", json.dumps(schema),
        ]
        if modell != "sonnet":
            # Om modellen är överbelastad eller inte ingår i ditt abonnemang används Sonnet.
            kommando += ["--fallback-model", "sonnet"]

        self.antal_anrop += 1
        try:
            korning = subprocess.run(kommando, input=fraga, capture_output=True,
                                     text=True, timeout=600)
        except subprocess.TimeoutExpired:
            print("  ! AI:n svarade inte inom 10 minuter.")
            return None

        try:
            svar = json.loads(korning.stdout)
        except json.JSONDecodeError:
            svar = {}
        if svar.get("subtype") == "success" and not svar.get("is_error"):
            resultat = svar.get("structured_output")
            if resultat is None:
                print("  ! AI:n gav inget svar i rätt format.")
            return resultat

        felet = str(svar.get("result") or korning.stderr or korning.stdout or "okänt fel").strip()
        smatt = felet.lower()
        if "limit" in smatt:
            raise Kvotslut(felet)
        if any(o in smatt for o in ("authenticat", "401", "oauth", "/login", "not logged in",
                                    "invalid api key", "credential")):
            raise Stopp("Claude godkände inte inloggningen (CLAUDE_CODE_OAUTH_TOKEN). Händer det "
                        "vid flera körningar i rad: skapa en ny nyckel med 'claude setup-token' "
                        "och lägg in den igen (se guiden).\n"
                        f"Felmeddelande: {felet[:300]}")
        print(f"  ! AI-anropet misslyckades: {felet[:500]}")
        return None


SALLNING_SCHEMA = {
    "type": "object",
    "properties": {
        "kandidater": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "sokfras": {"type": "string"},
                    "uppskattat_varde_kr": {"type": "integer"},
                    "varfor": {"type": "string"},
                },
                "required": ["id", "sokfras", "uppskattat_varde_kr", "varfor"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["kandidater"],
    "additionalProperties": False,
}

EXPERT_SCHEMA = {
    "type": "object",
    "properties": {
        "fynd": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "betyg": {"type": "integer"},
                    "uppskattat_varde_kr": {"type": "integer"},
                    "max_bud_kr": {"type": "integer"},
                    "motivering": {"type": "string"},
                    "risker": {"type": "string"},
                },
                "required": ["id", "betyg", "uppskattat_varde_kr", "max_bud_kr",
                             "motivering", "risker"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["fynd"],
    "additionalProperties": False,
}

BEVAKNINGAR_TEXT = "\n".join(f"- {b['namn']}: {b['for_ai']}" for b in BEVAKNINGAR)

SALLNING_INSTRUKTION = f"""Du är en erfaren auktionsexpert och opportunist som letar fynd på Tradera, \
Sveriges största auktionssajt. Du har god känsla för andrahandsvärden i Sverige.

Användaren bevakar bara det här:
{BEVAKNINGAR_TEXT}

Du får en lista med auktioner som slutar inom kort, var och en märkt med sin bevakning. Sålla \
snabbt fram de få som kan vara riktiga fynd: där ett troligt slutpris ligger klart under vad saken \
är värd på andrahandsmarknaden.

Tänk på:
- Ta bara med sådant som passar bevakningens beskrivning.
- Nuvarande bud är inte slutpriset. Auktioner får ofta flera bud de sista minuterna, särskilt \
populära märken. Ett riktigt fynd har marginal även efter det.
- Typiska fynd: felstavade eller vaga titlar, säljare som inte vet vad de har, kända märken utan \
bud, partier och samlingar som är värda mer styckvis, lågt utrop och få bud.
- Många annonser är vanliga varor till normalpris. Hoppa över dem utan att tveka.
- Undvik kopior och förfalskningar och saker där vinsten blir under ungefär 200 kr.
- Hellre inga kandidater än dåliga. En tom lista är ett helt okej svar.

Välj högst {MAX_KANDIDATER} kandidater, bästa först. Ange för varje kandidat:
- id: auktionens id.
- sokfras: 3–100 tecken som beskriver saken som en annonstitel (t.ex. "Omega Seamaster automatic"). \
Den används för att slå upp vad liknande saker har sålts för.
- uppskattat_varde_kr: ditt uppskattade andrahandsvärde i kronor.
- varfor: en kort mening om varför den kan vara ett fynd.

Om användaren:
{OM_MIG.strip()}"""

EXPERT_INSTRUKTION = f"""Du är en erfaren auktionsexpert och opportunist. Du gör den slutliga \
bedömningen av några utvalda Tradera-auktioner. Användaren får dina bästa tips som en push-notis i \
mobilen strax innan auktionen slutar och budar själv.

Användaren bevakar bara det här:
{BEVAKNINGAR_TEXT}

För varje auktion har du bevakning, titel, kategori, pris, bud, frakt, säljare, beskrivning, och \
ibland Traderas prisförslag (medianpriset för liknande sålda saker). Prisförslaget bygger bara på en \
sökfras och kan vara missvisande om saken skiljer sig från det typiska – väg det mot din egen kunskap.

När det står "Bild:" med en filsökväg: öppna alla bilder med verktyget Read innan du bedömer \
(gärna alla på en gång). Använd bilden för att bedöma skick, äkthet och vad saken faktiskt är.

Bedöm för varje auktion:
- Vad saken troligen är värd på andrahandsmarknaden i Sverige.
- Troligt slutpris – priset stiger ofta i slutet.
- Total kostnad inklusive frakt.
- Skick, äkthet och risker.

Ta bara med auktioner som är riktiga fynd. Ange:
- betyg: 1–10, där 10 är ett självklart fynd. Var sträng; under {MIN_BETYG} visas inte för användaren.
- uppskattat_varde_kr: vad saken är värd på andrahandsmarknaden.
- max_bud_kr: det högsta bud (utan frakt) som fortfarande ger ett riktigt bra köp.
- motivering: högst två korta meningar på svenska om varför det är ett fynd.
- risker: högst en kort mening om vad som kan gå fel.

Motivering och risker visas i en mobilnotis, så skriv kort och konkret. Hellre inga fynd än tveksamma.

Om användaren:
{OM_MIG.strip()}"""


def formatera_kr(belopp):
    return f"{int(belopp):,} kr".replace(",", " ")


def minuter_kvar(slutar, nu):
    return max(0, int((slutar - nu).total_seconds() // 60))


def salla(claude, annonser, vagar, nu):
    rader = []
    for a in annonser:
        delar = [
            str(a["id"]),
            f"[{a['_bevakning']}]",
            (a.get("shortDescription") or "").strip(),
            vagar.get(a.get("categoryId"), "?"),
            f"nu {formatera_kr(nuvarande_pris(a))} ({a.get('bidCount', 0)} bud)",
        ]
        if a.get("buyItNowPrice"):
            delar.append(f"köp nu {formatera_kr(a['buyItNowPrice'])}")
        delar.append(f"slutar om {minuter_kvar(a['_slutar'], nu)} min")
        if a.get("sellerDsrAverage"):
            delar.append(f"säljarbetyg {a['sellerDsrAverage']:.1f}/5")
        rader.append(" | ".join(delar))

    fraga = (f"Här är {len(rader)} auktioner som slutar inom kort "
             f"(id | bevakning | titel | kategori | pris | övrigt):\n\n" + "\n".join(rader))
    svar = claude.fraga(SALLNINGS_MODELL, SALLNING_INSTRUKTION, fraga, SALLNING_SCHEMA, "low")
    if not svar:
        return []
    kanda = {a["id"]: a for a in annonser}
    return [k for k in svar["kandidater"] if k["id"] in kanda][:MAX_KANDIDATER]


def valj_bild(detaljer, annons):
    for lank in detaljer.get("detailedImageLinks") or []:
        if (lank.get("format") or "").lower() in ("medium", "normal", "large") and lank.get("url"):
            return lank["url"]
    bilder = detaljer.get("imageLinks") or []
    return bilder[0] if bilder else (detaljer.get("thumbnailLink") or annons.get("thumbnailLink"))


def ladda_ner_bild(url, annons_id):
    """Sparar bilden i bildmappen så att AI:n kan öppna den. Returnerar sökvägen eller None."""
    try:
        svar = requests.get(url, timeout=15)
        typ = svar.headers.get("Content-Type", "")
        if not svar.ok or not typ.startswith("image/") or len(svar.content) > 5_000_000:
            return None
    except requests.RequestException:
        return None
    andelse = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(typ, ".jpg")
    sokvag = os.path.join(BILDMAPP, f"{annons_id}{andelse}")
    with open(sokvag, "wb") as fil:
        fil.write(svar.content)
    return sokvag


def expertbedom(claude, tradera, kandidater, annonser, vagar, nu):
    kanda = {a["id"]: a for a in annonser}
    shutil.rmtree(BILDMAPP, ignore_errors=True)
    os.makedirs(BILDMAPP, exist_ok=True)
    stycken, antal_bilder = [], 0

    for nr, kandidat in enumerate(kandidater, start=1):
        annons = kanda[kandidat["id"]]
        try:
            detaljer = tradera.get(f"/items/{annons['id']}")
        except requests.HTTPError:
            detaljer = {}

        pris = None
        if nr <= MAX_PRISKOLLAR:
            kategori = detaljer.get("categoryId") or annons.get("categoryId")
            pris = prisforslag(tradera, kandidat["sokfras"], kategori)

        frakter = [o.get("cost") for o in detaljer.get("shippingOptions") or []
                   if o.get("cost") is not None]
        saljare = detaljer.get("seller") or {}
        annons["_frakt"] = min(frakter) if frakter else None
        annons["_bild"] = valj_bild(detaljer, annons)

        rader = [
            f"### Auktion {nr}",
            f"id: {annons['id']}",
            f"Bevakning: {annons['_bevakning']}",
            f"Titel: {annons.get('shortDescription')}",
            f"Kategori: {vagar.get(annons.get('categoryId'), '?')}",
            f"Nuvarande pris: {formatera_kr(nuvarande_pris(annons))} "
            f"({annons.get('bidCount', 0)} bud). Nästa bud: {formatera_kr(annons.get('nextBid') or 0)}.",
        ]
        if annons.get("buyItNowPrice"):
            rader.append(f"Köp nu-pris: {formatera_kr(annons['buyItNowPrice'])}")
        rader.append(f"Slutar: kl {annons['_slutar'].astimezone(STOCKHOLM):%H:%M} "
                     f"(om {minuter_kvar(annons['_slutar'], nu)} min)")
        rader.append("Frakt: " + (", ".join(formatera_kr(f) for f in sorted(set(frakter)))
                                  if frakter else "ej angiven"))
        rader.append(f"Avhämtning möjlig: {'ja' if detaljer.get('acceptsPickup') else 'nej'}")
        if saljare:
            rader.append(f"Säljare: {saljare.get('alias')}, omdöme {saljare.get('totalRating')}, "
                         f"{saljare.get('city') or 'okänd ort'}")
        if pris:
            rader.append(f"Traderas prisförslag: {formatera_kr(pris['suggested'])} "
                         f"(spann {formatera_kr(pris.get('low') or 0)}–"
                         f"{formatera_kr(pris.get('high') or 0)})")
        rader.append(f"Första bedömningen: värde ca {formatera_kr(kandidat['uppskattat_varde_kr'])}"
                     f" – {kandidat['varfor']}")
        if SKICKA_BILDER_TILL_AI and annons["_bild"]:
            bildfil = ladda_ner_bild(annons["_bild"], annons["id"])
            if bildfil:
                rader.append(f"Bild: {bildfil}")
                antal_bilder += 1
        beskrivning = rensa_html(detaljer.get("longDescription") or annons.get("longDescription"))
        if beskrivning:
            rader.append(f"Beskrivning: {beskrivning}")
        stycken.append("\n".join(rader))

    fraga = (f"Klockan är nu {nu.astimezone(STOCKHOLM):%H:%M}. "
             f"Här är {len(kandidater)} kandidater att bedöma.\n\n" + "\n\n".join(stycken))
    svar = claude.fraga(EXPERT_MODELL, EXPERT_INSTRUKTION, fraga, EXPERT_SCHEMA, "medium",
                        verktyg="Read" if antal_bilder else "")
    if not svar:
        return []
    return [f for f in svar["fynd"]
            if f["id"] in kanda
            and f["betyg"] >= MIN_BETYG
            and f["max_bud_kr"] >= (kanda[f["id"]].get("nextBid") or 0)]


# ─── Fynd som väntar på att skickas ────────────────────────────────

def annonslank(annons):
    return annons.get("itemUrl") or f"https://www.tradera.com/item/{annons['id']}"


def spara_fynd(fynd, annonser, vantande):
    kanda = {a["id"]: a for a in annonser}
    for f in fynd:
        a = kanda[f["id"]]
        vantande[str(f["id"])] = {
            "id": f["id"],
            "titel": a.get("shortDescription") or "?",
            "bevakning": a["_bevakning"],
            "lank": annonslank(a),
            "bild": a.get("_bild"),
            "frakt": a.get("_frakt"),
            "slutar": a["_slutar"].isoformat(),
            "pris": nuvarande_pris(a),
            "bud": a.get("bidCount", 0),
            "nasta_bud": a.get("nextBid") or 0,
            "betyg": f["betyg"],
            "varde": f["uppskattat_varde_kr"],
            "max_bud": f["max_bud_kr"],
            "motivering": f["motivering"],
            "risker": f["risker"],
        }


def uppdatera_fynd(tradera, f):
    """Hämtar aktuellt pris. Returnerar False om auktionen redan har slutat."""
    try:
        d = tradera.get(f"/items/{f['id']}")
    except requests.HTTPError:
        return True  # behåll de gamla uppgifterna
    if (d.get("status") or {}).get("ended"):
        return False
    if d.get("totalBids"):
        f["bud"] = d["totalBids"]
        f["pris"] = d.get("maxBid") or f["pris"]
    if d.get("nextBid"):
        f["nasta_bud"] = d["nextBid"]
    slutar = tolka_tid(d.get("endDate"))
    if slutar:
        f["slutar"] = slutar.isoformat()
    return True


def skicka_klara_fynd(tradera, amne, vantande, nu):
    """Skickar de väntande fynd som nu har högst TIPSA_NAR_KVAR_MINUTER kvar."""
    klara = []
    for nyckel, f in list(vantande.items()):
        kvar = (tolka_tid(f["slutar"]) - nu).total_seconds() / 60
        if kvar > TIPSA_NAR_KVAR_MINUTER:
            continue
        del vantande[nyckel]
        if kvar < TIPSA_MINST_KVAR_MINUTER:
            print(f"  ! Hann inte tipsa om '{f['titel']}' – den slutar för snart.")
        elif not uppdatera_fynd(tradera, f):
            print(f"  '{f['titel']}' har redan slutat.")
        elif f["nasta_bud"] > f["max_bud"]:
            print(f"  '{f['titel']}': priset har passerat maxbudet – inget tips.")
        else:
            klara.append(f)
    klara.sort(key=lambda f: f["betyg"], reverse=True)
    for i in range(0, len(klara), MAX_FYND_PER_NOTIS):
        skicka_fynd(amne, klara[i:i + MAX_FYND_PER_NOTIS], nu)
    return len(klara)


# ─── Notiser via ntfy ──────────────────────────────────────────────

def skicka_notis(amne, titel, meddelande, lank=None, bild=None, knappar=None,
                 prioritet=3, taggar=None):
    data = {"topic": amne, "title": titel, "message": meddelande, "priority": prioritet}
    if lank:
        data["click"] = lank
    if bild:
        data["attach"] = bild
    if knappar:
        data["actions"] = [{"action": "view", "label": etikett, "url": url}
                           for etikett, url in knappar[:3]]
    if taggar:
        data["tags"] = taggar
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/") + "/"
    try:
        svar = requests.post(server, json=data, timeout=20)
        if not svar.ok:
            print(f"  ! Notisen kunde inte skickas: {svar.status_code} {svar.text[:200]}")
    except requests.RequestException as fel:
        print(f"  ! Notisen kunde inte skickas: {fel}")


def skicka_fynd(amne, fynd, nu):
    stycken, knappar = [], []
    for nr, f in enumerate(fynd, start=1):
        slutar = tolka_tid(f["slutar"])
        frakt = f" (+ frakt {formatera_kr(f['frakt'])})" if f.get("frakt") else ""
        stycken.append("\n".join([
            f"{nr}. {f['titel']} – {f['betyg']}/10 ({f['bevakning']})",
            f"Nu {formatera_kr(f['pris'])} ({f['bud']} bud) · "
            f"slutar {slutar.astimezone(STOCKHOLM):%H:%M} (om {minuter_kvar(slutar, nu)} min)",
            f"Värt ca {formatera_kr(f['varde'])} · buda max {formatera_kr(f['max_bud'])}{frakt}",
            f"Varför: {f['motivering']}",
            f"Risk: {f['risker']}",
        ]))
        knappar.append((f"Öppna {nr}" if len(fynd) > 1 else "Öppna annonsen", f["lank"]))

    basta = fynd[0]
    titel = (f"{basta['bevakning']} – fynd {basta['betyg']}/10: {basta['titel']}"[:120]
             if len(fynd) == 1 else f"{len(fynd)} fynd på Tradera som slutar snart")
    skicka_notis(amne, titel, "\n\n".join(stycken), lank=basta["lank"], bild=basta.get("bild"),
                 knappar=knappar, prioritet=4, taggar=["moneybag"])


# ─── Minne: bedömda auktioner, väntande fynd och paus ──────────────

def las_minne(nu):
    try:
        with open(MINNESFIL, encoding="utf-8") as fil:
            minne = json.load(fil)
    except (FileNotFoundError, json.JSONDecodeError):
        minne = {}
    if "sedda" not in minne:  # äldre format: bara sedda auktioner
        minne = {"sedda": minne}
    minne.setdefault("vantande", {})
    grans = nu - timedelta(hours=1)
    minne["sedda"] = {i: t for i, t in minne["sedda"].items() if (tolka_tid(t) or nu) > grans}
    return minne


def spara_minne(minne):
    with open(MINNESFIL, "w", encoding="utf-8") as fil:
        json.dump(minne, fil, ensure_ascii=False)


# ─── Huvudprogram ──────────────────────────────────────────────────

def leta_fynd(tradera, minne, nu):
    """Söker, filtrerar och låter AI:n bedöma nya auktioner. Nya fynd läggs bland de väntande."""
    claude = Claude()
    sedda = minne["sedda"]
    tidigast = nu + timedelta(minutes=TIPSA_MINST_KVAR_MINUTER)
    slut = nu + timedelta(minutes=BEDOM_MINUTER_FRAMAT)

    print(f"Letar auktioner som slutar mellan {tidigast.astimezone(STOCKHOLM):%H:%M} "
          f"och {slut.astimezone(STOCKHOLM):%H:%M} …")
    huvudkategorier, vagar = hamta_kategorier(tradera)
    alla = hamta_auktioner(tradera, bygg_sokningar(huvudkategorier), nu, slut)
    per_bevakning = Counter(a["_bevakning"] for a in alla)
    print("  Hittade per bevakning: "
          + ", ".join(f"{b['namn']} {per_bevakning.get(b['namn'], 0)}" for b in BEVAKNINGAR))

    obedomda = [a for a in alla if str(a["id"]) not in sedda and a["_slutar"] >= tidigast]
    kvar, skal = filtrera(obedomda, vagar)
    if len(kvar) > MAX_ANNONSER_TILL_AI:
        print(f"  ! {len(kvar)} annonser klarade filtren, men bara {MAX_ANNONSER_TILL_AI} "
              "skickas till AI:n nu (MAX_ANNONSER_TILL_AI). Resten tas i nästa körning.")
    kvar = sorted(kvar, key=lambda a: a["_slutar"])[:MAX_ANNONSER_TILL_AI]
    bortfiltrerade = ", ".join(f"{s} {n}" for s, n in skal.most_common()) or "inga"
    print(f"  {len(alla)} auktioner totalt, {len(obedomda)} nya. "
          f"Filtren tog bort {sum(skal.values())} ({bortfiltrerade}). "
          f"{len(kvar)} går vidare till AI:n. ({tradera.antal_anrop} anrop till Tradera.)")

    kandidater, fynd = [], []
    if kvar:
        kandidater = salla(claude, kvar, vagar, nu)
        for a in kvar:
            sedda[str(a["id"])] = a["_slutar"].isoformat()
        print(f"  Sållningen gav {len(kandidater)} kandidater.")
        for k in kandidater:
            print(f"    - {k['id']}: {k['sokfras']} (värde ca {k['uppskattat_varde_kr']} kr)")

    if kandidater:
        fynd = expertbedom(claude, tradera, kandidater, kvar, vagar, nu)
        spara_fynd(fynd, kvar, minne["vantande"])
        print(f"  Expertbedömningen gav {len(fynd)} fynd med betyg {MIN_BETYG} eller högre.")

    print(f"  AI-anrop i den här körningen: {claude.antal_anrop} (ingår i ditt abonnemang).")
    return {"alla": len(alla), "bort": sum(skal.values()), "bortfiltrerade": bortfiltrerade,
            "granskade": len(kvar), "kandidater": len(kandidater), "fynd": len(fynd)}


def kor(hemligheter, minne, nu):
    manuell = os.environ.get("MANUELL_KORNING") == "true"
    amne = hemligheter["NTFY_TOPIC"]
    lokal = nu.astimezone(STOCKHOLM)
    tradera = Tradera(hemligheter["TRADERA_APP_ID"], hemligheter["TRADERA_APP_KEY"])
    paus_till = tolka_tid(minne.get("paus_till"))
    resultat = None

    if not manuell and not (AKTIV_FRAN_KL <= lokal.hour < AKTIV_TILL_KL):
        print(f"Klockan är {lokal:%H:%M} – utanför aktiva tider "
              f"({AKTIV_FRAN_KL}–{AKTIV_TILL_KL}). Letar inga nya fynd.")
    elif not manuell and paus_till and paus_till > nu:
        print(f"Pausad till {paus_till.astimezone(STOCKHOLM):%H:%M} eftersom abonnemangets "
              "användningsgräns nåddes. Letar inga nya fynd.")
    else:
        try:
            resultat = leta_fynd(tradera, minne, nu)
        except Kvotslut as fel:
            minne["paus_till"] = (nu + timedelta(minutes=PAUS_VID_KVOTSLUT_MIN)).isoformat()
            print(f"  ! Abonnemangets användningsgräns är nådd ({str(fel)[:200]}). "
                  f"Pausar i {PAUS_VID_KVOTSLUT_MIN} minuter.")
            if manuell:
                skicka_notis(amne, "Fyndjägaren pausar ⏸",
                             "Ditt Claude-abonnemangs användningsgräns är nådd. "
                             f"Fyndjägaren försöker igen om {PAUS_VID_KVOTSLUT_MIN} minuter.",
                             taggar=["pause_button"])

    skickade = skicka_klara_fynd(tradera, amne, minne["vantande"], nu)
    if skickade:
        print(f"  Skickade tips om {skickade} fynd.")
    vantar = len(minne["vantande"])
    if vantar:
        print(f"  {vantar} fynd väntar och skickas när det är högst "
              f"{TIPSA_NAR_KVAR_MINUTER} minuter kvar.")

    if manuell and resultat:
        skicka_notis(amne, "Fyndjägaren fungerar ✅",
                     f"{resultat['alla']} auktioner i dina bevakningar slutar inom "
                     f"{BEDOM_MINUTER_FRAMAT} min. Filtren tog bort {resultat['bort']} "
                     f"({resultat['bortfiltrerade']}). AI:n granskade {resultat['granskade']}, "
                     f"valde ut {resultat['kandidater']} kandidater och hittade "
                     f"{resultat['fynd']} fynd. Tips skickade nu: {skickade}. "
                     f"Väntar på att skickas: {vantar}.",
                     taggar=["white_check_mark"])


def main():
    nu = datetime.now(timezone.utc)
    minne = las_minne(nu)
    try:
        kor(hamta_hemligheter(), minne, nu)
    except Stopp as fel:
        print(f"\nSTOPP: {fel}")
        sys.exit(1)
    finally:
        spara_minne(minne)


if __name__ == "__main__":
    main()
