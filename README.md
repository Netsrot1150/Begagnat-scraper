# Begagnat-scraper

**Finds cheap high-VRAM GPUs for running AI locally** by searching Swedish and international second-hand marketplaces (Blocket, Tradera, eBay, Facebook Marketplace) and ranking listings by value, not just price.

- Understands what a listing actually sells: "2 st Tesla P100 16gb" = 32 GB VRAM; coolers, wanted-ads and SXM cards are filtered out
- Converts foreign prices to SEK, including import VAT and customs fees
- Scores cards by VRAM, memory bandwidth and software support per krona
- Watch mode with push notifications on new listings and price drops, plus a local web UI
- Optional AI screening with Claude that catches scams, whole PCs and wanted-ads
- 241 tests, none requiring network access

**Status:** working, v0.1.0. The documentation below is in Swedish.

---

Söker igenom svenska och internationella begagnatsajter efter grafikkort med
mycket VRAM — kort som är användbara för att köra LLM:er lokalt — och rankar
träffarna efter vad du faktiskt får för pengarna.

```
$ begagnat search "tesla p100" --min-vram 16 --max-price 1400

     SCORE      PRICE   kr/GB   VRAM  GPU                TITLE                                          WHERE
------------------------------------------------------------------------------------------------------------
**   123.9      1 250      78  16 GB  Tesla P100         Nvidia Tesla P100 16GB HBM2 servergrafikkort   blocket
    https://www.blocket.se/annons/stockholm/nvidia-tesla-p100/1401234567
**    74.1      2 089     131  16 GB  Tesla P100         NVIDIA Tesla P100 16GB HBM2 PCIe GPU           ebay
    https://www.ebay.com/itm/335123456789
```

Verktyget gör tre saker som en vanlig sparad sökning inte gör:

1. **Förstår vad annonsen säljer.** "2 st Tesla P100 16gb" blir 32 GB VRAM,
   "Kylare till Tesla P40" och "Tesla V100 SXM2" sorteras bort automatiskt.
2. **Räknar hem priset.** eBay-priser i USD/EUR konverteras till kronor, och
   för säljare utanför EU läggs 25 % moms plus tullavgift på — ett kort för
   130 USD från Kina landar på ~2 100 kr, inte 1 260 kr.
3. **Rankar efter värde, inte pris.** Poängen väger VRAM mot minnesbandbredd
   och hur väl kortet stöds av dagens inferensstackar, delat med vad det kostar.

## Installation

```bash
git clone https://github.com/Netsrot1150/Begagnat-scraper.git
cd Begagnat-scraper
pip install -e .
```

Python 3.10 eller senare. Tre tillval:

- `pip install -e '.[web]'` för webbgränssnittet (`begagnat web`)
- `pip install -e '.[ai]'` för AI-validering av annonser (se nedan)
- `pip install -e '.[browser]'` + `playwright install chromium` för Facebook Marketplace

## Kom igång

```bash
# En sökning, rankad tabell
begagnat search "tesla p100" --max-price 1400

# Allt med minst 16 GB som kostar under 120 kr/GB
begagnat search grafikkort --min-vram 16 --max-kr-per-gb 120

# Se varför annonser sorterades bort
begagnat search "tesla p40" --show-rejected

# Bevaka: kolla var 15:e minut och pusha nya träffar till mobilen
cp begagnat.example.yaml begagnat.yaml   # fyll i ntfy-topic
begagnat watch --interval 900
```

## Webbgränssnitt

```bash
pip install -e '.[web]'
begagnat web --open
```

Öppnar `http://127.0.0.1:8377/` med allt på ett ställe:

- **Fynd** — allt du har sparat, rankat, med filter på pris, VRAM, kr/GB, källa
  och kort. Klicka på en rad för prishistorik, hur annonsen ligger mot modellens
  median de senaste 180 dagarna, kortets fallgropar och en egen anteckning.
  Stjärnmärk det du är intresserad av, dölj skräpet.
- **Bevakningar** — samma sak som `watch`, men redigerbart i webbläsaren och
  sparat i databasen istället för i YAML. Varje bevakning har eget intervall,
  egna filter och en "Kör nu"-knapp. En bakgrundstråd kör dem när de blir due.
- **Sök** — en engångssökning direkt mot marknadsplatserna, med bortsorterade
  annonser och skälet till varje bortsortering.
- **Flöde** — nya träffar och prissänkningar, per bevakning.
- **Prisstatistik** — min/median/max per modell ur din egen historik.
- **Kortkatalog** — hela GPU-katalogen med kommentarer.

`--host 0.0.0.0` gör den nåbar från mobilen på samma nät. Det finns ingen
inloggning, så gör det bara på ett nät du litar på.

`watch` läser sökprofilerna från `begagnat.yaml`, kommer ihåg vad den redan
har sett i en SQLite-databas (`~/.local/share/begagnat/listings.db`) och larmar
bara om **nya** annonser och **prissänkningar** — inte om samma annons om och
om igen. En säljare som raderar och lägger upp annonsen på nytt för att bumpa
den ger inte heller ett nytt larm.

Kör den som en systemd-timer eller cronjobb om du vill ha den igång dygnet runt:

```
*/15 * * * * cd /sökväg/till/Begagnat-scraper && begagnat watch --once >> watch.log 2>&1
```

## AI-validering

Regelmotorn ser bara nyckelord. Den kan inte avgöra att "Säljer min gamingdator,
RTX 3090 24GB" är en hel dator och inte ett löst kort, att "Tesla P40 sökes" är
en **köpes**-annons, eller att ett P100 för 400 kr med dimmig stockbild och
"hör av dig på WhatsApp" är ett bedrägeriförsök. Det är vad `--ai` är till för.

```bash
pip install -e '.[ai]'
export ANTHROPIC_API_KEY=sk-ant-...

begagnat search "tesla p100" --max-price 1400 --ai
```

Claude läser varje annons som tagit sig igenom regelfiltren och returnerar ett
strukturerat omdöme: säljs kortet faktiskt, är det ett löst kort eller en hel
dator, vilken modell och hur mycket VRAM, vilket skick, och vilka varningar som
är värda att nämna (minat, passiv kylning, ingen garanti, minnesmoddat kort,
SXM-formfaktor). Omdömet kan korrigera VRAM och antal, vilket räknas om till
kr/GB och poäng.

```
**   123.9      1 250      78  16 GB  Tesla P100   Nvidia Tesla P100 16GB HBM2   blocket
    ! möjligen minat, passiv kylning - kräver shroud och fläkt
    https://www.blocket.se/annons/...

Filtrerade bort:
  - Tesla P40 24GB sökes                    AI: köpes/bytes-annons, inte till salu
  - Gamingdator i7 + RTX 3090               AI: hel dator, inte ett löst grafikkort
```

`--ai-rescue` låter dessutom AI:n titta på annonser som sorterats bort *enbart*
för att modellen inte finns i katalogen — så ett nytt eller ovanligt kort inte
missas för att verktyget aldrig hört talas om det. Ett räddat kort som visar sig
vara en känd modell får katalogens siffror; ett okänt märks ut i utdatan.

AI:n får aldrig återuppliva en annons som föll på pris, VRAM eller kr/GB — dina
filter är dina filter.

**Kostnad.** Annonser skickas i buntar om åtta, systemprompten cachas, och varje
omdöme sparas i databasen så samma annons aldrig betalas för två gånger (en
ändrad annons bedöms om). Varje körning skriver ut vad den kostade:

```
AI: 3 calls, 41 cached, 12 480 in / 2 130 out tokens, ~$0.116
```

Standardmodellen är `claude-opus-5`. För stora volymer är `--ai-model
claude-sonnet-5` eller `claude-haiku-4-5` billigare — screening av annonstext är
en enkel uppgift. `--ai-effort` styr hur mycket modellen tänker (`low` räcker).

Utan `--ai` görs inga API-anrop alls och `anthropic` behöver inte vara
installerat.

## Källor

| Källa | Status | Kommentar |
|---|---|---|
| `blocket` | standard | Blockets eget JSON-API, ingen inloggning. Tre vägar i tur och ordning: nuvarande `recommerce`-endpoint, äldre `search_bff` med token, och HTML. |
| `tradera` | standard | Registrera en gratis app på [api.tradera.com](https://api.tradera.com/) och sätt `TRADERA_APP_ID` och `TRADERA_APP_KEY` — då används det officiella REST-API:t automatiskt istället för HTML-skrapning. Skillnaden är stor: skrapningen ser en sida, API:t bläddrar genom hela träfflistan (2995 annonser på 60 sidor för "grafikkort") och får med annonstexten och budstatusen. |
| `ebay` | standard | **eBay svarar 403 på vanliga HTTP-klienter från de flesta nät.** Registrera en gratis app på [developer.ebay.com](https://developer.ebay.com/) och sätt `EBAY_CLIENT_ID` och `EBAY_CLIENT_SECRET` — då används det officiella Browse-API:t automatiskt istället för HTML-skrapning. Välj marknadsplats med `--ebay-domain ebay.de` för EU-säljare utan momspåslag. |
| `marketplace` | opt-in | Facebook Marketplace. Kräver Playwright och en engångsinloggning: `begagnat login-marketplace`. |

Marketplace är den bräckligaste källan — Facebook obfuskerar sin markup och
byter den ofta. Den ingår inte i standarduppsättningen; lägg till den med
`--source marketplace`.

**Auktioner.** Ett pågående bud är inte ett pris — ingen har gått med på att
sälja för det. En auktion som just öppnat på 1 kr skulle annars vara landets
bästa affär tills någon lägger ett bud. Auktioner visas därför, men markerade
med `~` och rankade efter alla fasta priser, och de räknas inte in i
prisstatistiken. Ett avslutat klubbat pris, och ett "köp nu"-pris, räknas som
vanligt.

## Kommandon

| Kommando | Vad det gör |
|---|---|
| `search` | En sökning. `--format table\|json\|csv\|detail` |
| `watch` | Pollar profilerna och notifierar om nya träffar |
| `best` | Bästa fynden i din databas hittills |
| `stats` | Prisstatistik per modell ur din egen historik — vet du vad ett P40 brukar kosta vet du när 1 400 kr är ett fynd |
| `gpus` | GPU-katalogen som används för matchning |
| `identify` | Testa titelparsern: `begagnat identify "2 st Tesla P100 16gb"` |
| `reindex` | Kör om titelparsern över allt du redan sparat. Varje parserfix lämnar historik efter sig — en rad behåller det parsern trodde den dagen den först sågs. `--dry-run` visar vad som skulle ändras |
| `sources` | Lista marknadsplatser (och vilken implementation som är aktiv per källa) |
| `web` | Startar webbgränssnittet med bevakningar, flöde och prishistorik |

## Filter

```
--max-price 1400          max landad kostnad i kr (inkl. moms/frakt för import)
--min-vram 16             minsta totala VRAM i GB
--max-kr-per-gb 120       pristak per GB
--min-tier good           hoppa över arkitekturer som är jobbiga för inferens
--model "Tesla P100"      bara en viss modell (upprepas)
--keyword pcie            kräv ett ord i annonsen (upprepas)
--exclude laptop          förkasta annonser med ett ord (upprepas)
--allow-broken            behåll trasiga kort och tillbehör
--any-listing             behåll annonser vars modell inte finns i katalogen
--no-import-cost          jämför råa priser utan moms/tullavgift
--region uppsala          bara ett län på Blocket (namn eller "Göteborg" m.fl.)
--ai                      låt Claude validera annonserna
--ai-rescue               låt AI:n även titta på okända GPU-modeller
--ai-model claude-sonnet-5  billigare modell för stora volymer
```

## Om poängen

`score` är ungefär "hur mycket användbar inferenskapacitet får jag per krona".
Täljaren är VRAM × √(bandbredd/500) × en faktor för mjukvarustöd; nämnaren är
den landade kostnaden. Ett P100 för 1 250 kr hamnar runt 120; ett RTX 3090 för
6 100 kr runt 54. Poängen är ett *sorteringsverktyg*, inte ett facit —
bandbredd avgör hur snabbt tokens genereras, men ett kort som inte får plats i
ditt chassi eller din nätdel är värt noll oavsett poäng.

`--min-tier` bygger på samma bedömning i grövre form:

- **great** — modernt, snabbt, stöds av allt (V100, A10, L4, 3090, A4000/A5000/A6000)
- **good** — bra värde med förbehåll (P100, P40, T4, 2080 Ti, 3060 12GB, 7900 XTX)
- **ok** — fungerar, men räkna med långsam promptbearbetning eller saknade kernels (MI50, Radeon VII, Arc A770, 1080 Ti)
- **poor** — bara värt det nästan gratis (K80, M40, MI25)

`begagnat gpus` visar hela katalogen med kommentarer om kylning, drivrutiner
och compute capability.

## Praktiska fallgropar när du köper

Verktyget flaggar de flesta av dem automatiskt, men det är värt att veta varför:

- **Passiv kylning.** P100, P40, M40, V100 och MI50 har ingen fläkt. De är
  byggda för serverchassin med 12 000 rpm-fläktar i rad. I ett vanligt chassi
  behöver du en 3D-printad shroud plus fläkt, annars stryper de sig direkt.
- **Strömkontakt.** Teslakorten använder EPS/CPU-kontakt (8-pin), inte PCIe
  8-pin. Adapter behövs — de ser likadana ut men har olika pinout.
- **SXM2/SXM4** är servermoduler, inte PCIe-kort. De går inte i en vanlig
  dator. Verktyget sorterar bort dem.
- **Above 4G Decoding / Resizable BAR** måste ofta slås på i BIOS för att
  datacenterkort ska bli synliga.
- **Compute capability.** P100 är 6.0 och P40 är 6.1 — de saknar bf16 och
  FlashAttention-2. llama.cpp (GGUF) fungerar bra, exllamav2 och nyare vLLM
  gör det inte. Kolla att din stack faktiskt stöder kortet innan du köper.
- **Import utanför EU.** 25 % moms på vara + frakt, plus ~125 kr
  förtullningsavgift hos transportören. `--no-import-cost` stänger av
  påslaget om du vill jämföra råa priser.

## Utveckling

```bash
pip install -e '.[dev]'
python -m pytest              # 145 tester, inget nätverk
```

AI-testerna stubbar Anthropic-klienten, så inga API-anrop görs och inget
API-nyckel behövs för att köra testsviten.

Parsers testas mot fixtures i `tests/fixtures/`. När en sajt ändrar sin markup:

```bash
begagnat search "tesla p100" --source tradera --save-html html-dump/
```

Spara den relevanta delen av svaret som en ny fixture och uppdatera parsern.
Varje källa isolerar sina fel — om Tradera slutar fungera fortsätter de andra
källorna att leverera, och felet skrivs ut på stderr.

Nya källor läggs till genom att ärva `begagnat.sources.base.Source`, ge klassen
ett `name` och en `search(criteria)` som returnerar `Listing`-objekt, och
registrera den i `begagnat/sources/__init__.py`.

## Inspiration

Blockets nuvarande sök-endpoint (`recommerce/forsale/search`, som inte kräver
någon token) är hämtad från [dunderrrrrr/blocket_api](https://github.com/dunderrrrrr/blocket_api),
ett välunderhållet inofficiellt Blocket-API. Andra projekt i samma genre värda
att titta på: [banjo/begagnat-bevakning](https://github.com/banjo/begagnat-bevakning)
(Blocket + Tradera till Telegram) och [WilhelmvonArndt/scraper-blocket](https://github.com/WilhelmvonArndt/scraper-blocket).

Både Tradera och eBay går den officiella vägen när nycklarna finns — se
tabellen under [Källor](#källor). HTML-skrapningen ligger kvar som reserv.

## Ansvar

Skrapning är emot användarvillkoren på flera av sajterna — Blocket förbjuder
uttryckligen automatiserad åtkomst utan tillstånd. Verktyget begränsar
sig själv till ett anrop per host och 1,5 sekund (`--min-interval`), använder
inga fejkade inloggningar och kringgår inte några skydd. Håll dig till rimliga
intervall — `watch` med 15 minuter räcker gott och väl för begagnatannonser.
