"""
e2e_endurance.py -- Phase 4 Endurance Test: 200 turns, one user.

Simulates a realistic 4-week work period for a single sales manager
using chat_id=80010. Verifies that:
  - State size does not grow unbounded (compression is working)
  - No crashes over 200 consecutive turns
  - Critical facts survive in long-term memory
  - Key business facts planted early are still recalled much later

Key facts planted and verified:
  Turn 5  : "OmikronAvto budget max 700k"
             -> verified at turns 50, 100, 150, 200
  Turn 25 : "TetaMedia deadline April 1"
             -> verified at turns 50, 100, 150, 200
  Turn 75 : "EtaFinans increased to 600k"
             -> verified at turns 100, 150, 200

Run:
    cd /path/to/ai-native-crm
    PYTHONIOENCODING=utf-8 python e2e_endurance.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

# ---------------------------------------------------------------------------
# Bootstrap: sys.path + env before any project imports
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/5")

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logging.getLogger("ai_native_crm").setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Project imports (after sys.path is patched)
# ---------------------------------------------------------------------------
import redis.asyncio as aioredis

from ai_native_crm.adapters.bitrix import BitrixAdapter
from ai_native_crm.config import settings
from ai_native_crm.core.action_router import ActionRouter
from ai_native_crm.core.compressor import StateCompressor
from ai_native_crm.core.drift_detector import DriftDetector
from ai_native_crm.core.engine import AgentEngine
from ai_native_crm.core.response_validator import ResponseValidator
from ai_native_crm.core.state_store import StateStore
from ai_native_crm.services.llm_client import LLMClient
from ai_native_crm.services.lock import DistributedLock
from ai_native_crm.services.metrics import MetricsService
from ai_native_crm.services.pii_anonymizer import PIIAnonymizer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHAT_ID: int = 80010
TOTAL_TURNS: int = 200
INTER_TURN_DELAY_SEC: float = 0.5
RESULTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "e2e_endurance_results.json",
)

# gpt-4o-mini pricing (USD per 1 000 000 tokens)
_PRICE_INPUT_PER_1M = 0.15
_PRICE_OUTPUT_PER_1M = 0.60

# Turns at which we print a state-size progress report
STATE_REPORT_INTERVAL = 20

# Turns at which we run memory verification checks
VERIFY_TURNS = {50, 100, 150, 200}

# ---------------------------------------------------------------------------
# 200-turn scenario (4 weeks)
# ---------------------------------------------------------------------------

TURNS: list[str] = [
    # -----------------------------------------------------------------------
    # Week 1 (turns 1-50): Onboarding, reviewing deals, setting priorities
    # -----------------------------------------------------------------------
    # Turn 1
    "Dobroe utro! Pokaji mne vse aktualynye sdelki",
    # Turn 2
    "Kakie sdelki samye krupnye po summe? Daj top-5",
    # Turn 3
    "Rasskaji podrobnee pro sdelku OmikronAvto -- fleet management",
    # Turn 4
    "Rasschi-tay obshchuyu summu vsekh aktivnykh sdelok v voronke",
    # Turn 5  [KEY FACT #1: OmikronAvto budget 700k]
    "Klient OmikronAvto skazal chto byudzhet maksimum 700000 rubley. Zafiksiruy eto kak kriticheskiy fakt.",
    # Turn 6
    "Perevedi sdelku AlfaTekh v stadiyu kvalifikacii",
    # Turn 7
    "Kakie sdelki nakhodatsya v stadii NEW?",
    # Turn 8
    "Napomni mne pozvonitj po sdelke ZetaLogistik cherez 2 chasa",
    # Turn 9
    "Klient KappaRiteyl otkazalsya -- skazal nashli deshevle u konkurentov. Zafiksiruy otkaz.",
    # Turn 10
    "Sdelaj otchyot po voronke -- skolyko sdelok na kazhdoy stadii",
    # Turn 11
    "Chto mne delatj v pervuyu ocheredj? Kakie sdelki prioritetnye?",
    # Turn 12
    "Prishel novyy lid: OOO TestovayaKompaniya, byudzhet 1 million, nuzhna integraciya 1S. Sozdaj sdelku.",
    # Turn 13
    "Pokaji obnovlennyy spisok vsekh sdelok",
    # Turn 14
    "Kakey byudzhet byl u OmikronAvto? Napomni mne.",
    # Turn 15
    "Obsudim strategiyu po EtaFinans -- billing na 500k. Kakie riski vidish?",
    # Turn 16
    "A chto po YotaTekh -- mobilnoe prilozhenie za 620k? Stoit li davatj skidku?",
    # Turn 17
    "Obnoviy summu sdelki GammaPro na 120000 rubley",
    # Turn 18
    "Pokaji kontakty vsekh klientov",
    # Turn 19
    "Kakie sdelki na stadii UC_INVOICE sejchas?",
    # Turn 20
    "Rasskaji chto my obsuzhdali za poslednie 10 khodov",
    # Turn 21
    "Vspomni vse fakty po otkazam klientov",
    # Turn 22
    "Prishel eshche odin lid: IP Testov, avtomatizaciya dokumentooborota, byudzhet 200k",
    # Turn 23
    "Perevedi sdelku BetaSoft v stadiyu UC_QUALIFIED",
    # Turn 24
    "Klient LyambdaStroy podtverdil byudzhet 750000 i gotov podpisatj na sleduyushchey nedele",
    # Turn 25  [KEY FACT #2: TetaMedia deadline April 1]
    "Klient TetaMedia skazal: dedlayn 1 aprelya, posle etogo kontrakt avtomaticheski otmenyaetsya. Zafiksiruy kak kriticheskiy fakt.",
    # Turn 26
    "Kakaya obshchaya summa vsekh aktivnykh sdelok sejchas?",
    # Turn 27
    "Zakroj sdelku PiDizayn kak vyigrannuyu",
    # Turn 28
    "Pokaji vse critical facts chto my sokhranili",
    # Turn 29
    "Kakey byudzhet byl u OmikronAvto i kakoy dedlayn u TetaMedia?",
    # Turn 30
    "Pokaji metriki kachestva sistemy",
    # Turn 31
    "Proverj drejft steyata i dolozhiy rezultat",
    # Turn 32
    "Skolyko sdelok my obrabotali za etu pervuyu nedelyu?",
    # Turn 33
    "Klient NyuFarma khochet uvelichitj byudzhet do 500000. Obnoviy summu sdelki.",
    # Turn 34
    "Perevedi EtaFinans v stadiyu UC_INVOICE",
    # Turn 35
    "Kakie sdelki blizki k zakrytiyu v blizhayshee vremya?",
    # Turn 36
    "Sozdaj sdelku: OOO NovyyKlient, razrabotka portala, 350000 rubley",
    # Turn 37
    "Obnoviy sdelku DeltaInzh -- summu na 75000 rubley",
    # Turn 38
    "Klient ZetaLogistik podtverdil oplatu. Perevedi v stadiyu UC_PAYMENT.",
    # Turn 39
    "Pokaji vse fakty po sdelkam kotorye my sohranili",
    # Turn 40
    "Kakie sdelki na stadii NEW sejchas?",
    # Turn 41
    "Sdelaj promezhutochnyy otchyot po vsem sdelkam",
    # Turn 42
    "Vspomni byudzhet OmikronAvto i dedlayn po TetaMedia",
    # Turn 43
    "Klient MyuKonsalt otkazalsya -- byudzhet urezali. Zafiksiruy otkaz.",
    # Turn 44
    "Perevedi KsiEnergo v stadiyu UC_QUALIFIED",
    # Turn 45
    "Kakie sdelki my zakryli na etoy nedele?",
    # Turn 46
    "Pokaji statistiku: skolyko sdelok sozdano, obnovleno, zakryto",
    # Turn 47
    "Obnoviy sdelku YotaTekh -- summu na 580000 so skidkoy 6%",
    # Turn 48
    "Kakie sdelki eshche nuzhno obrabotatj do konca pervoy nedeli?",
    # Turn 49
    "Pokaji itogovyy otchyot po voronke za pervuyu nedelyu",
    # Turn 50
    "Proverj: pomnish li ty byudzhet OmikronAvto i dedlayn TetaMedia?",

    # -----------------------------------------------------------------------
    # Week 2 (turns 51-100): Active deal management, stage changes, updates
    # -----------------------------------------------------------------------
    # Turn 51
    "Nachinayem vtoruyu nedelyu. Pokaji vse otkrytye sdelki.",
    # Turn 52
    "Rasstavj prioritety na etu nedelyu",
    # Turn 53
    "Zvonok s OmikronAvto: oni podtverdili byudzhet 700k, prosyat demonstraciyu. Zafiksiruy.",
    # Turn 54
    "Perevedi AlfaTekh v stadiyu UC_IN_PROCESS",
    # Turn 55
    "Klient SigmaAuto zaprosil KP na 400000. Sozdaj sdelku.",
    # Turn 56
    "Kakie kontakty est u LyambdaStroy?",
    # Turn 57
    "Obnoviy EtaFinans -- summu na 520000 posle peresmotra",
    # Turn 58
    "Pokaji vse sdelki stoymostyu vyshe 500000",
    # Turn 59
    "Klient BetaSoft khochet vstrechu v sredu. Zapishi v fakty.",
    # Turn 60
    "Sdelaj analiz voronki: gde samye bolshie potoperi?",
    # Turn 61
    "Napomni mne otpravitj KP OmikronAvto segodnya v 18:00",
    # Turn 62
    "Perevedi NyuFarma v stadiyu UC_QUALIFIED",
    # Turn 63
    "Klient ThetaRetail otkazalsya -- reshili sdelat vnutri kompanii. Zafiksiruy.",
    # Turn 64
    "Kakoy obshchiy ob\"em voronki sejchas v rublyakh?",
    # Turn 65
    "Pokaji vse fakty kotorye my sobranili pro otkazy",
    # Turn 66
    "Sozdaj sdelku: ZAO ImpulsMedia, razrabotka sajta, byudzhet 180000",
    # Turn 67
    "Obnoviy DeltaInzh -- perevedi v stadiyu UC_IN_PROCESS",
    # Turn 68
    "Kakie sdelki dolzhny zakrytsya v etom mesyace?",
    # Turn 69
    "Klient LyambdaStroy poprosil otlozhitj podpisanie na nedelyu. Zafiksiruy.",
    # Turn 70
    "Vspomni vse chto my znaem pro OmikronAvto i TetaMedia",
    # Turn 71
    "Pokaji top-3 sdelki po veroyanosti zakrytiya",
    # Turn 72
    "Obnoviy YotaTekh -- oni gotovy podpisatj umovy na 580000",
    # Turn 73
    "Klient GammaPro khochet skidku 10%. Stoit li soglasitsya?",
    # Turn 74
    "Napomni mne pro dedlayn TetaMedia -- kogda on?",
    # Turn 75  [KEY FACT #3: EtaFinans increased to 600k]
    "EtaFinans uvelichil byudzhet do 600000 posle odobreniya soveta direktorov. Zafiksiruy eto kak kriticheskiy fakt.",
    # Turn 76
    "Pokaji vse critical facts po vsem sdelkam",
    # Turn 77
    "Perevedi KsiEnergo v stadiyu UC_IN_PROCESS",
    # Turn 78
    "Klient NovyyKlient prosit demonstraciyu produkta. Zaplaniruj na sleduyushchuyu nedelyu.",
    # Turn 79
    "Kakie sdelki my ne trogali uzhe bolshe nedeli?",
    # Turn 80
    "Sdelaj otchyot po rabote za vtoruyu nedelyu",
    # Turn 81
    "Pokaji summu EtaFinans -- ona uzhe obnovlena?",
    # Turn 82
    "Obnoviy BetaSoft -- oni podchinilis byudzhetnomu soglasheniyu na 300000",
    # Turn 83
    "Kakie sdelki trebuyut dejstviy segodnya?",
    # Turn 84
    "Perevedi SigmaAuto v stadiyu UC_QUALIFIED",
    # Turn 85
    "Klient ImpulsMedia zaprosil vstrechu. Zafiksiruy.",
    # Turn 86
    "Pokaji analiz effektivnosti: skolyko sdelok zakryto vs otkryto",
    # Turn 87
    "Obnoviy OmikronAvto -- perevedi v stadiyu UC_IN_PROCESS posle demonstracii",
    # Turn 88
    "Klient KsiEnergo podtverdil byudzhet 450000. Zafiksiruy.",
    # Turn 89
    "Pokaji vse izmeneniya za vtoruyu nedelyu",
    # Turn 90
    "Kak obstoit delo s TetaMedia -- ostalos eshche vremya do dedlayna?",
    # Turn 91
    "Obnoviy NyuFarma -- oni uvelichili byudzhet do 550000",
    # Turn 92
    "Sozdaj sdelku: OOO DigitalPuls, CRM-integraciya, byudzhet 260000",
    # Turn 93
    "Perevedi LyambdaStroy v stadiyu UC_INVOICE posle podtverzhdeniya",
    # Turn 94
    "Kakie kontakty est u TetaMedia?",
    # Turn 95
    "Pokaji metriiku kachestva i drift-score",
    # Turn 96
    "Klient DeltaInzh zaprosil skidku 15%. Zafiksiruy zaprosy na skidku.",
    # Turn 97
    "Obnoviy GammaPro -- skidka 8% soglasovana, novaya summa 110400",
    # Turn 98
    "Kakie sdelki na stadii UC_INVOICE?",
    # Turn 99
    "Sdelaj svodnyy otchyot po vtoroy nedele",
    # Turn 100
    "Proverj pamyatj: pomnish li byudzhet OmikronAvto 700k, dedlayn TetaMedia 1 aprelya, i novyy byudzhet EtaFinans?",

    # -----------------------------------------------------------------------
    # Week 3 (turns 101-150): Follow-ups, reporting, rejections, new leads
    # -----------------------------------------------------------------------
    # Turn 101
    "Trejya nedelya nachinalas. Pokaji prioritetnye sdelki.",
    # Turn 102
    "Zvonok YotaTekh: gotovy podpisatj dogovor na 580000 segodnya",
    # Turn 103
    "Perevedi YotaTekh v stadiyu UC_WON -- sdelka zakryta!",
    # Turn 104
    "Klient AlfaTekh prosit otlozhitj na mesyac iz-za vnutrennego audita. Zafiksiruy.",
    # Turn 105
    "Pokaji vse sdelki kotorye zakryty kak vyigrannye",
    # Turn 106
    "Sozdaj sdelku: OOO TechFront, oblachnaya infrastruktura, 800000",
    # Turn 107
    "Klient LyambdaStroy podpisal dogovor! Perevedi v UC_WON.",
    # Turn 108
    "Kakie sdelki v etom mesyace principialnye?",
    # Turn 109
    "Klient SigmaAuto uvelichil byudzhet do 480000 posle demonstracii. Obnoviy.",
    # Turn 110
    "Vspomni byudzhet OmikronAvto i status sdelki",
    # Turn 111
    "Obnoviy TetaMedia -- im otpravleno KP. Zafiksiruy.",
    # Turn 112
    "Klient NyuFarma otkazalsya v posledniy moment -- prichina byudzhetnye soblazny. Zafiksiruy.",
    # Turn 113
    "Sozdaj sdelku: IP Aleksandrov, vnedrenie 1S, 320000 rubley",
    # Turn 114
    "Pokazi otchyot: skolyko sdelok zakryto v etom mesyace",
    # Turn 115
    "Perevedi KsiEnergo v stadiyu UC_INVOICE",
    # Turn 116
    "Klient DigitalPuls zaprosil tekhnicheskoe soglasovanie. Zafiksiruy etap",
    # Turn 117
    "Kakoy obshchiy byudzhet EtaFinans -- pomnish poslednee obnovlenie?",
    # Turn 118
    "Obnoviy TechFront -- oni khodyat k konkurentam, nuzhno bystro reagirovatj",
    # Turn 119
    "Kakie sdelki riskyut sорvaться esli ne dejstvovat segodnya?",
    # Turn 120
    "Pokazi svodku: vsego sdelok, aktivnykh, zakrytykh, otklonenikh",
    # Turn 121
    "Zvonok OmikronAvto: im ponravilas demonstraciya, byudzhet ostaetsya 700k",
    # Turn 122
    "Sozdaj sdelku: ZAO MediaGrup, reklamnaya platforma, 420000",
    # Turn 123
    "Perevedi BetaSoft v stadiyu UC_IN_PROCESS",
    # Turn 124
    "Klient ImpulsMedia otkazalsya -- ne smoglis soglasovat s rukovodством. Zafiksiruy.",
    # Turn 125
    "Pokazi vse otkazy za poslednie tri nedeli",
    # Turn 126
    "Obnoviy OmikronAvto -- perevedi v stadiyu UC_INVOICE",
    # Turn 127
    "Klient DeltaInzh podpisal dogovor na 75000. Perevedi v UC_WON.",
    # Turn 128
    "Kakie kontakty nuzhno prozvonit segodnya?",
    # Turn 129
    "Pokazi metriiku gallyucinacii i kachestvo otvetov",
    # Turn 130
    "Sdelaj analiz: pochemu klienty otkazyvayutsya -- samye chastnye prichiny",
    # Turn 131
    "Sozdaj sdelku: OOO FastDev, mobilnoe prilozhenie, 390000",
    # Turn 132
    "Perevedi SigmaAuto v stadiyu UC_IN_PROCESS",
    # Turn 133
    "Klient TetaMedia podtverdil chto dedlayn vse eshche 1 aprelya i oni gotovy k peregovoram",
    # Turn 134
    "Kakie sdelki dolzhny zakrytsya do konca mesyaca?",
    # Turn 135
    "Obnoviy EtaFinans -- oni ozhidayut finansovoe soglasovanie",
    # Turn 136
    "Pokazi top-5 sdelok po veroyanosti zakrytiya",
    # Turn 137
    "Klient Aleksandrov poprosil demonstraciyu systemy. Zafiksiruy.",
    # Turn 138
    "Perevedi MediaGrup v stadiyu UC_QUALIFIED",
    # Turn 139
    "Sdelaj weekly-otchyot za trejyu nedelyu",
    # Turn 140
    "Pokazi vse critical facts naby-tye za vse tri nedeli",
    # Turn 141
    "Klient TechFront reshil rabotaj s nami! Perevedi v UC_QUALIFIED.",
    # Turn 142
    "Obnoviy FastDev -- oni uvelichili byudzhet do 440000",
    # Turn 143
    "Kakie novye sdelki my sozdali za trejyu nedelyu?",
    # Turn 144
    "Pokazi status po OmikronAvto -- gde oni sejchas?",
    # Turn 145
    "Klient DigitalPuls prosit skidku 5%. Stoit li soglashatsya?",
    # Turn 146
    "Perevedi KsiEnergo v UC_PAYMENT -- oni podtverdili oplatu",
    # Turn 147
    "Sdelaj prognoz na sleduyushchuyu nedelyu: kakie sdelki zakrepsya",
    # Turn 148
    "Pokazi drift-score i sostoyaniye pamyati",
    # Turn 149
    "Obnoviy TetaMedia -- urgently, ostalosy malo vremeni do dedlayna 1 aprelya",
    # Turn 150
    "Proverj pamyatj: nazovi byudzhet OmikronAvto, dedlayn TetaMedia i byudzhet EtaFinans",

    # -----------------------------------------------------------------------
    # Week 4 (turns 151-200): Closing deals, final reports, strategy
    # -----------------------------------------------------------------------
    # Turn 151
    "Poslednyaya nedelya mesyaca. Pokazi vse sdelki gotovye k zakrytiyu.",
    # Turn 152
    "OmikronAvto gotov podpisatj na 700000. Oformlyaj sdelku k zakrytiyu.",
    # Turn 153
    "Perevedi OmikronAvto v UC_WON -- sdelka zakryta!",
    # Turn 154
    "Klient EtaFinans podtverdil byudzhet 600000 i gotov podpisatj",
    # Turn 155
    "Perevedi EtaFinans v UC_INVOICE",
    # Turn 156
    "Klient TetaMedia -- zvonok segodnya, napomni mne dedlayn i status",
    # Turn 157
    "TetaMedia podpisali dogovor do dedlayna! Perevedi v UC_WON.",
    # Turn 158
    "Pokazi vse sdelki kotorye my zakryli za etot mesyac",
    # Turn 159
    "Sozdaj itogovy finansovyy otchyot za mesyac",
    # Turn 160
    "Klient FastDev gotov k podpisaniyu. Perevedi v UC_INVOICE.",
    # Turn 161
    "Perevedi SigmaAuto v UC_INVOICE posle podtverzhdeniya byudzheta",
    # Turn 162
    "Klient Aleksandrov otkazalsya -- prishli k vyvodu chto eto ne prioriet. Zafiksiruy.",
    # Turn 163
    "Pokazi kakie sdelki ostayutsya otkrytymi",
    # Turn 164
    "Obnoviy TechFront -- oni priblizilis k resheniyu, vstrecha naznachena",
    # Turn 165
    "Klient MediaGrup prosit tekhnicheskoe KP. Otpravil im vchera. Zafiksiruy.",
    # Turn 166
    "EtaFinans podtverdil oplatu! Perevedi v UC_WON.",
    # Turn 167
    "Pokazi obshchuyu summu zakrytykh sdelok za mesyac",
    # Turn 168
    "Klient BetaSoft gotov k itogoy vstreche -- zavtra v 14:00",
    # Turn 169
    "Sozdaj sdelku dlya novogo klienta: LLC OmegaSoft, ERP-sistema, 950000",
    # Turn 170
    "Perevedi FastDev v UC_WON posle polucheniya oplaty",
    # Turn 171
    "Klient DigitalPuls podtverdil skidku i podpisanie na sleduyushchuyu nedelyu",
    # Turn 172
    "Pokazi strategiyu dlya OmegaSoft -- kak luchshe vsego zakryt sdelku na 950k?",
    # Turn 173
    "Perevedi BetaSoft v UC_WON -- soglashenie podpisano!",
    # Turn 174
    "Kakie sdelki eshche aktuvnye v pipeline?",
    # Turn 175
    "Klient TechFront podtverdil byudzhet 800000 i podpisal predvaritelnyy dogovor",
    # Turn 176
    "Pokazi vse fakty sohranennye za ves mesyac",
    # Turn 177
    "Perevedi TechFront v UC_INVOICE",
    # Turn 178
    "Klient MediaGrup gotov k finalnoy vstreche. Naznach na zavtra.",
    # Turn 179
    "Pokazi progrеss za chetvertuyu nedelyu: zakrytye, otkrytye, novye sdelki",
    # Turn 180
    "Sdelaj sravnenie byudzhetov zakrytykh sdelok -- OmikronAvto, EtaFinans, TetaMedia",
    # Turn 181
    "Klient SigmaAuto podpisal dogovor na 480000! Perevedi v UC_WON.",
    # Turn 182
    "Pokazi ves pipeline dlya analiza",
    # Turn 183
    "OmegaSoft prosit otchyot o nашikh resheniyakh. Podgotov kratkoe opisanie.",
    # Turn 184
    "Perevedi DigitalPuls v UC_INVOICE posle podtverzhdeniya",
    # Turn 185
    "TechFront podtverdil oplatu. Perevedi v UC_WON.",
    # Turn 186
    "Klient MediaGrup podpisal! Perevedi v UC_WON.",
    # Turn 187
    "Pokazi finansovuyu svodku: obshchaya summa vsekh zakrytykh sdelok",
    # Turn 188
    "Sozdaj strategiyu prodazh na sleduyushchiy mesyac",
    # Turn 189
    "Kakie uroki my izvlekli iz etogo mesyaca -- pochemu nekotorye sdelki sornalis?",
    # Turn 190
    "Pokazi top-5 krupneyshikh zakrytykh sdelok etogo mesyaca",
    # Turn 191
    "DigitalPuls podpisal! Perevedi v UC_WON.",
    # Turn 192
    "Pokazi kompletnyy otchyot za ves mesyac: vse sdelki, vse fakty",
    # Turn 193
    "Kak mensya zovut i kakie klyuchevye fakty ty pomnish pro moikh klientov?",
    # Turn 194
    "Pokazi analiz effektivnosti: konversiya po stadiyam",
    # Turn 195
    "Naznachj vstrechuyu s OmegaSoft na sleduyushchuyu nedelyu dlya demonstracii",
    # Turn 196
    "Pokazi vse critical facts sohranennye za ves period",
    # Turn 197
    "Sdelaj prognoz plana prodazh na sleduyushchiy mesyac",
    # Turn 198
    "Pokazi metriiku kachestva: gallyucinacii, drift, dejstviya",
    # Turn 199
    "Podvedi itogu chetyrex nedel raboty -- samye vazhnye sobytiya i dos-izheniya",
    # Turn 200
    "Poslednyaya proverka pamyati: byudzhet OmikronAvto, dedlayn TetaMedia, byudzhet EtaFinans -- pomnish?",
]

assert len(TURNS) == TOTAL_TURNS, f"Expected {TOTAL_TURNS} turns, got {len(TURNS)}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_print(msg: str) -> None:
    """Print with ASCII-safe fallback."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", errors="replace").decode("ascii"))


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * pct / 100.0
    lo = int(k)
    hi = lo + 1
    if hi >= len(sorted_vals):
        return sorted_vals[-1]
    return sorted_vals[lo] + (k - lo) * (sorted_vals[hi] - sorted_vals[lo])


async def _flush_redis_state(redis_client: aioredis.Redis, chat_id: int) -> None:
    """Delete all Redis keys belonging to chat_id before the run starts."""
    keys_to_delete = [
        f"state:{chat_id}",
        f"critical_facts:{chat_id}",
        f"audit:{chat_id}",
        f"metrics:{chat_id}",
        f"reminders:{chat_id}",
        f"pii:{chat_id}",
        f"lock:chat:{chat_id}",
    ]
    deleted = await redis_client.delete(*keys_to_delete)
    _safe_print(f"[FLUSH] Deleted {deleted} Redis keys for chat_id={chat_id}")


# ---------------------------------------------------------------------------
# Component factory
# ---------------------------------------------------------------------------


def _build_engine(
    redis_client: aioredis.Redis,
    crm: BitrixAdapter,
) -> tuple[AgentEngine, StateStore]:
    """Wire all components together and return (engine, store)."""
    store = StateStore(redis_client, audit_ttl_days=settings.audit_ttl_days)
    llm = LLMClient()
    validator = ResponseValidator(crm)
    router = ActionRouter(crm=crm, bot=None, state_store=store)
    compressor = StateCompressor(llm)
    drift = DriftDetector(crm)
    anonymizer = PIIAnonymizer(redis_client)
    lock = DistributedLock(redis_client)
    metrics = MetricsService(state_store=store, bot=None, alert_chat_id=None)

    engine = AgentEngine(
        state_store=store,
        crm=crm,
        llm=llm,
        validator=validator,
        action_router=router,
        compressor=compressor,
        drift=drift,
        anonymizer=anonymizer,
        lock=lock,
        metrics=metrics,
    )
    return engine, store


# ---------------------------------------------------------------------------
# Per-turn metadata collection
# ---------------------------------------------------------------------------


async def _collect_turn_meta(
    store: StateStore,
    chat_id: int,
    turn_number: int,
    user_input: str,
    response: str,
    latency_ms: int,
    error: str | None,
) -> dict:
    """Read Redis state after a turn and assemble per-turn metrics dict."""
    tokens_in = 0
    tokens_out = 0
    state_size_bytes = 0
    critical_facts_count = 0
    iteration = 0
    working_memory_len = 0

    try:
        raw_state = await store.redis.get(f"state:{chat_id}")
        if raw_state:
            state_size_bytes = len(
                raw_state.encode("utf-8") if isinstance(raw_state, str) else raw_state
            )
            try:
                state_obj = json.loads(raw_state)
                iteration = int(state_obj.get("iteration", 0))
                working_memory_len = len(state_obj.get("working_memory", ""))
            except (json.JSONDecodeError, ValueError):
                pass

        cf_count = await store.redis.llen(f"critical_facts:{chat_id}")
        critical_facts_count = int(cf_count) if cf_count else 0

        audit_raw = await store.redis.xrevrange(f"audit:{chat_id}", count=1)
        if audit_raw:
            _, fields = audit_raw[0]
            tokens_in = int(fields.get("tokens_in", 0))
            tokens_out = int(fields.get("tokens_out", 0))

    except Exception as exc:
        logging.getLogger(__name__).warning("_collect_turn_meta error: %s", exc)

    return {
        "turn": turn_number,
        "user_input": user_input,
        "response": response,
        "latency_ms": latency_ms,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "state_size": state_size_bytes,
        "critical_facts_count": critical_facts_count,
        "working_memory_len": working_memory_len,
        "iteration": iteration,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Memory verification
# ---------------------------------------------------------------------------


async def _verify_memory(
    redis_client: aioredis.Redis,
    chat_id: int,
    turn_number: int,
    response: str,
) -> dict:
    """
    Check whether key planted facts are present in critical_facts or
    the most recent response text. Returns a dict of fact -> bool.
    """
    cf_raw = await redis_client.lrange(f"critical_facts:{chat_id}", 0, -1)
    all_facts_text = ""
    for item in cf_raw:
        try:
            obj = json.loads(item)
            all_facts_text += " " + obj.get("content", "")
        except (json.JSONDecodeError, TypeError):
            all_facts_text += " " + str(item)

    combined = (all_facts_text + " " + response).lower()

    omikron_ok = (
        "omikron" in combined
        and ("700" in combined or "700000" in combined or "700k" in combined)
    )
    teta_ok = (
        ("teta" in combined or "tetamedia" in combined)
        and ("aprel" in combined or "april" in combined or "1 apr" in combined)
    )
    eta_ok = (
        ("eta" in combined or "etafinans" in combined)
        and ("600" in combined or "600000" in combined or "600k" in combined)
    )

    result = {
        "turn": turn_number,
        "omikron_700k": omikron_ok,
        "teta_april1": teta_ok,
        "eta_600k": eta_ok,
    }

    _safe_print(f"\n  [MEMORY CHECK @ turn {turn_number}]")
    _safe_print(f"    OmikronAvto budget 700k -> {omikron_ok}")
    _safe_print(f"    TetaMedia deadline Apr 1 -> {teta_ok}")
    _safe_print(f"    EtaFinans budget 600k   -> {eta_ok}")

    return result


# ---------------------------------------------------------------------------
# State size progression report
# ---------------------------------------------------------------------------


def _print_state_report(metrics_per_turn: list[dict], up_to_turn: int) -> None:
    """Print state size progression for completed turns at report_interval."""
    _safe_print(f"\n  [STATE SIZE REPORT @ turn {up_to_turn}]")
    _safe_print(f"  {'Turn':>5}  {'state_size':>12}  {'iter':>6}  {'facts':>6}  {'wm_len':>7}")
    # Print every 5 turns within the last STATE_REPORT_INTERVAL block
    start = max(0, up_to_turn - STATE_REPORT_INTERVAL)
    for m in metrics_per_turn[start:up_to_turn]:
        _safe_print(
            f"  {m['turn']:>5}  {m['state_size']:>10}B  "
            f"{m['iteration']:>6}  {m['critical_facts_count']:>6}  "
            f"{m['working_memory_len']:>7}"
        )

    # Check for unbounded growth: compare first and last state sizes in this window
    window = metrics_per_turn[start:up_to_turn]
    if len(window) >= 2:
        sizes = [m["state_size"] for m in window if m["state_size"] > 0]
        if sizes:
            growth_ratio = sizes[-1] / sizes[0] if sizes[0] > 0 else 1.0
            bounded = growth_ratio < 3.0  # allow up to 3x growth before flagging
            _safe_print(
                f"  Growth in window: {sizes[0]}B -> {sizes[-1]}B "
                f"(ratio={growth_ratio:.2f}, bounded={bounded})"
            )


# ---------------------------------------------------------------------------
# Main endurance runner
# ---------------------------------------------------------------------------


async def run_endurance() -> None:
    _safe_print("=" * 72)
    _safe_print("  E2E ENDURANCE TEST -- 200 TURNS (4-WEEK SIMULATION)")
    _safe_print(f"  chat_id   : {CHAT_ID}")
    _safe_print(f"  redis_url : {os.environ.get('REDIS_URL', settings.redis_url)}")
    _safe_print(f"  llm_model : {settings.llm_model}")
    _safe_print(f"  bitrix    : {settings.bitrix_webhook[:40]}...")
    _safe_print("=" * 72)

    # ------------------------------------------------------------------
    # 1. Connect to Redis (DB 5)
    # ------------------------------------------------------------------
    redis_url = os.environ.get("REDIS_URL", settings.redis_url)
    redis_client: aioredis.Redis = aioredis.from_url(
        redis_url,
        decode_responses=True,
        socket_timeout=settings.redis_socket_timeout,
        socket_connect_timeout=settings.redis_connect_timeout,
    )

    try:
        await redis_client.ping()
        _safe_print(f"[OK] Redis connected: {redis_url}")
    except Exception as exc:
        _safe_print(f"[FATAL] Cannot connect to Redis: {exc}")
        await redis_client.aclose()
        return

    # ------------------------------------------------------------------
    # 2. Flush previous state for chat_id 80010
    # ------------------------------------------------------------------
    await _flush_redis_state(redis_client, CHAT_ID)

    # ------------------------------------------------------------------
    # 3. Build single engine instance (used for all 200 turns)
    # ------------------------------------------------------------------
    crm = BitrixAdapter(settings.bitrix_webhook)
    engine, store = _build_engine(redis_client, crm)

    # ------------------------------------------------------------------
    # 4. Run all 200 turns
    # ------------------------------------------------------------------
    metrics_per_turn: list[dict] = []
    memory_checks: list[dict] = []
    total_errors = 0

    _safe_print(f"\nStarting {TOTAL_TURNS} turns (4-week simulation) ...\n")

    week_labels = {1: "Week 1", 51: "Week 2", 101: "Week 3", 151: "Week 4"}

    for idx, user_input in enumerate(TURNS):
        turn_number = idx + 1

        # Print week boundary header
        if turn_number in week_labels:
            _safe_print(f"\n{'=' * 72}")
            _safe_print(f"  {week_labels[turn_number].upper()} (turns {turn_number}-{turn_number + 49})")
            _safe_print(f"{'=' * 72}")

        t_start = time.monotonic()
        error_msg: str | None = None
        response = ""

        # Each turn is wrapped in try/except so the test continues on failure
        try:
            response = await engine.process(user_input, CHAT_ID)
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            total_errors += 1
            response = f"[ERROR: {error_msg}]"
            logging.getLogger(__name__).error(
                "Turn %d raised exception: %s", turn_number, exc, exc_info=True
            )

        latency_ms = round((time.monotonic() - t_start) * 1000)

        turn_data = await _collect_turn_meta(
            store=store,
            chat_id=CHAT_ID,
            turn_number=turn_number,
            user_input=user_input,
            response=response,
            latency_ms=latency_ms,
            error=error_msg,
        )
        metrics_per_turn.append(turn_data)

        # Per-turn console line
        status = "ERR" if error_msg else " OK"
        _safe_print(
            f"  Turn {turn_number:03d}/{TOTAL_TURNS} [{status}] "
            f"{latency_ms}ms | "
            f"tokens={turn_data['tokens_in']}in/{turn_data['tokens_out']}out | "
            f"state={turn_data['state_size']}B | "
            f"facts={turn_data['critical_facts_count']} | "
            f"wm={turn_data['working_memory_len']}ch | "
            f"iter={turn_data['iteration']}"
        )
        preview = response[:100].replace("\n", " ")
        _safe_print(f"         -> {preview}")

        # State size progression report every 20 turns
        if turn_number % STATE_REPORT_INTERVAL == 0:
            _print_state_report(metrics_per_turn, turn_number)

        # Memory verification at milestone turns
        if turn_number in VERIFY_TURNS:
            check = await _verify_memory(redis_client, CHAT_ID, turn_number, response)
            memory_checks.append(check)

        # Delay between turns to avoid rate-limiting
        if idx < len(TURNS) - 1:
            await asyncio.sleep(INTER_TURN_DELAY_SEC)

    # ------------------------------------------------------------------
    # 5. Post-run summary
    # ------------------------------------------------------------------
    _safe_print("\n" + "=" * 72)
    _safe_print("  POST-RUN VERIFICATION")
    _safe_print("=" * 72)

    # Final state
    final_state_raw = await redis_client.get(f"state:{CHAT_ID}")
    final_state: dict = {}
    if final_state_raw:
        try:
            final_state = json.loads(final_state_raw)
        except json.JSONDecodeError:
            pass

    final_iteration = int(final_state.get("iteration", 0))
    final_wm = final_state.get("working_memory", "")
    final_assessment = final_state.get("agent_assessment", "")
    final_summary = final_state.get("conversation_summary", "")
    final_state_size = (
        len(final_state_raw.encode("utf-8") if isinstance(final_state_raw, str) else final_state_raw)
        if final_state_raw
        else 0
    )

    _safe_print(f"  Final iteration    : {final_iteration}")
    _safe_print(f"  Final state size   : {final_state_size} bytes")
    _safe_print(f"  Working memory     : {len(final_wm)} chars")
    _safe_print(f"  Assessment         : {len(final_assessment)} chars")
    _safe_print(f"  Conv. summary      : {len(final_summary)} chars")

    # Critical facts
    cf_raw = await redis_client.lrange(f"critical_facts:{CHAT_ID}", 0, -1)
    critical_facts_list: list[dict] = []
    for item in cf_raw:
        try:
            critical_facts_list.append(json.loads(item))
        except (json.JSONDecodeError, TypeError):
            critical_facts_list.append({"raw": str(item)})

    _safe_print(f"\n  Critical facts saved: {len(critical_facts_list)}")
    for i, cf in enumerate(critical_facts_list[:40]):
        content = cf.get("content", cf.get("raw", "?"))[:100]
        fact_type = cf.get("fact_type", "?")
        deal_id = cf.get("deal_id", "")
        _safe_print(f"    [{i+1:02d}] [{fact_type}] deal={deal_id or '-'}: {content}")
    if len(critical_facts_list) > 40:
        _safe_print(f"    ... and {len(critical_facts_list) - 40} more")

    # State growth verification
    _safe_print(f"\n  State size progression (every 20 turns):")
    _safe_print(f"  {'Turn':>5}  {'state_size':>12}  {'iter':>6}  {'facts':>6}")
    for m in metrics_per_turn[::20]:
        _safe_print(
            f"  {m['turn']:>5}  {m['state_size']:>10}B  "
            f"{m['iteration']:>6}  {m['critical_facts_count']:>6}"
        )
    if metrics_per_turn:
        last = metrics_per_turn[-1]
        _safe_print(
            f"  {last['turn']:>5}  {last['state_size']:>10}B  "
            f"{last['iteration']:>6}  {last['critical_facts_count']:>6}"
        )

    # Unbounded growth check: compare first non-zero size to final
    first_nonzero = next((m["state_size"] for m in metrics_per_turn if m["state_size"] > 0), 0)
    growth_ratio = final_state_size / first_nonzero if first_nonzero > 0 else 1.0
    state_bounded = growth_ratio < 10.0  # healthy if <10x initial size after 200 turns
    _safe_print(f"\n  State growth: {first_nonzero}B -> {final_state_size}B (ratio={growth_ratio:.2f})")
    _safe_print(f"  State bounded (<10x): {state_bounded}")

    # Compression detection
    state_sizes_series = [m["state_size"] for m in metrics_per_turn if m["state_size"] > 0]
    compression_events = 0
    for i in range(1, len(state_sizes_series)):
        if state_sizes_series[i] < state_sizes_series[i - 1] * 0.7:
            compression_events += 1
    _safe_print(f"  Compression events detected: {compression_events}")

    # Memory check summary
    _safe_print(f"\n  Memory survival summary:")
    for check in memory_checks:
        t = check["turn"]
        o = check["omikron_700k"]
        te = check["teta_april1"]
        e = check["eta_600k"]
        # eta_600k only planted at turn 75, so N/A before turn 100
        eta_str = str(e) if t >= 100 else "N/A (planted at t75)"
        _safe_print(
            f"    Turn {t:03d}: OmikronAvto_700k={o}  TetaMedia_Apr1={te}  EtaFinans_600k={eta_str}"
        )

    # Redis metrics
    metrics_raw = await redis_client.hgetall(f"metrics:{CHAT_ID}")
    _safe_print(f"\n  Redis metrics:")
    for k, v in sorted(metrics_raw.items()):
        _safe_print(f"    {k}: {v}")

    total_turns_redis = int(metrics_raw.get("total_turns", 0))
    hallucination_count = int(metrics_raw.get("hallucination_count", 0))
    action_total = int(metrics_raw.get("action_total", 0))
    action_success = int(metrics_raw.get("action_success", 0))

    hallucination_rate = hallucination_count / total_turns_redis if total_turns_redis else 0.0
    action_success_rate = action_success / action_total if action_total else 0.0

    audit_len = await redis_client.xlen(f"audit:{CHAT_ID}")
    _safe_print(f"\n  Audit stream entries: {audit_len}")

    # ------------------------------------------------------------------
    # 6. Summary statistics
    # ------------------------------------------------------------------
    _safe_print("\n" + "=" * 72)
    _safe_print("  SUMMARY STATISTICS")
    _safe_print("=" * 72)

    latencies = [m["latency_ms"] for m in metrics_per_turn]
    total_tokens_in = sum(m["tokens_in"] for m in metrics_per_turn)
    total_tokens_out = sum(m["tokens_out"] for m in metrics_per_turn)
    errors_in_turns = [m for m in metrics_per_turn if m["error"]]

    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
    min_latency = min(latencies) if latencies else 0.0
    max_latency = max(latencies) if latencies else 0.0
    p95_latency = _percentile(latencies, 95)

    cost_input = (total_tokens_in / 1_000_000) * _PRICE_INPUT_PER_1M
    cost_output = (total_tokens_out / 1_000_000) * _PRICE_OUTPUT_PER_1M
    total_cost = cost_input + cost_output

    _safe_print(f"  Total turns run      : {TOTAL_TURNS}")
    _safe_print(f"  Errors during turns  : {len(errors_in_turns)}")
    _safe_print(f"  Crash rate           : {len(errors_in_turns) / TOTAL_TURNS:.1%}")
    _safe_print(f"")
    _safe_print(f"  Latency (ms):")
    _safe_print(f"    avg   : {avg_latency:.0f}")
    _safe_print(f"    min   : {min_latency:.0f}")
    _safe_print(f"    max   : {max_latency:.0f}")
    _safe_print(f"    p95   : {p95_latency:.0f}")
    _safe_print(f"")
    _safe_print(f"  Tokens:")
    _safe_print(f"    total input    : {total_tokens_in:,}")
    _safe_print(f"    total output   : {total_tokens_out:,}")
    _safe_print(f"    avg per turn   : {(total_tokens_in + total_tokens_out) // TOTAL_TURNS if TOTAL_TURNS else 0:,}")
    _safe_print(f"")
    _safe_print(f"  Cost estimate (gpt-4o-mini pricing):")
    _safe_print(f"    input   : ${cost_input:.4f}")
    _safe_print(f"    output  : ${cost_output:.4f}")
    _safe_print(f"    TOTAL   : ${total_cost:.4f}")
    _safe_print(f"")
    _safe_print(f"  Quality:")
    _safe_print(f"    hallucination_rate   : {hallucination_rate:.1%} ({hallucination_count}/{total_turns_redis})")
    _safe_print(f"    action_success_rate  : {action_success_rate:.1%} ({action_success}/{action_total})")
    _safe_print(f"    state_bounded        : {state_bounded} (ratio={growth_ratio:.2f}x)")
    _safe_print(f"    compression_events   : {compression_events}")
    _safe_print(f"")
    _safe_print(f"  Memory survival across 200 turns:")
    for check in memory_checks:
        t = check["turn"]
        o = check["omikron_700k"]
        te = check["teta_april1"]
        e = check["eta_600k"]
        eta_str = str(e) if t >= 100 else "N/A"
        _safe_print(f"    @turn {t:03d}: omikron_700k={o} | teta_apr1={te} | eta_600k={eta_str}")

    if errors_in_turns:
        _safe_print(f"\n  ERROR DETAILS:")
        for m in errors_in_turns[:20]:
            _safe_print(f"    Turn {m['turn']:03d}: {m['error']}")
        if len(errors_in_turns) > 20:
            _safe_print(f"    ... and {len(errors_in_turns) - 20} more errors")

    # ------------------------------------------------------------------
    # 7. Save results to JSON
    # ------------------------------------------------------------------
    # Compute per-turn state sizes as a compact list for the results
    state_size_progression = [
        {"turn": m["turn"], "state_size": m["state_size"], "iteration": m["iteration"]}
        for m in metrics_per_turn[::20]
    ]
    if metrics_per_turn:
        last = metrics_per_turn[-1]
        if last not in metrics_per_turn[::20]:
            state_size_progression.append(
                {"turn": last["turn"], "state_size": last["state_size"], "iteration": last["iteration"]}
            )

    results = {
        "run_meta": {
            "test_name": "e2e_endurance_200turns",
            "chat_id": CHAT_ID,
            "redis_url": redis_url,
            "llm_model": settings.llm_model,
            "total_turns": TOTAL_TURNS,
            "inter_turn_delay_sec": INTER_TURN_DELAY_SEC,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
        "summary": {
            "errors_total": len(errors_in_turns),
            "crash_rate": round(len(errors_in_turns) / TOTAL_TURNS, 4),
            "latency_avg_ms": round(avg_latency),
            "latency_min_ms": round(min_latency),
            "latency_max_ms": round(max_latency),
            "latency_p95_ms": round(p95_latency),
            "total_tokens_in": total_tokens_in,
            "total_tokens_out": total_tokens_out,
            "cost_usd_estimate": round(total_cost, 6),
            "hallucination_rate": round(hallucination_rate, 4),
            "action_success_rate": round(action_success_rate, 4),
            "final_state_size_bytes": final_state_size,
            "state_growth_ratio": round(growth_ratio, 2),
            "state_bounded": state_bounded,
            "compression_events": compression_events,
            "critical_facts_count": len(critical_facts_list),
        },
        "memory_survival_checks": memory_checks,
        "state_size_progression": state_size_progression,
        "metrics_per_turn": metrics_per_turn,
        "critical_facts": critical_facts_list,
        "final_state": {
            "iteration": final_iteration,
            "state_size_bytes": final_state_size,
            "working_memory_len": len(final_wm),
            "working_memory_preview": final_wm[:500],
            "assessment_preview": final_assessment[:300],
            "summary_preview": final_summary[:300],
        },
        "redis_metrics_raw": dict(metrics_raw),
        "errors": [
            {"turn": m["turn"], "input": m["user_input"], "error": m["error"]}
            for m in errors_in_turns
        ],
    }

    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    _safe_print(f"\n[DONE] Results saved to: {RESULTS_FILE}")
    _safe_print("=" * 72)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    await crm.close()
    await redis_client.aclose()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(run_endurance())
