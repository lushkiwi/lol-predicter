# M1: cross-check of the OE draft order against Leaguepedia

Date: 2026-11-10. Code: `draft_analyzer/ingest/leaguepedia.py` (client and parsers) and `draft_analyzer/ingest/join.py` (join and comparison). Output: `data/processed/leaguepedia_crosscheck.parquet`. Rerun with `python -m draft_analyzer.ingest.join`; add `--offline` to use the cache only. Tests: `tests/test_leaguepedia.py`.

## Bottom line

- In the stratified sample of **245 games**, **245 matched (100%)**. Leaguepedia and OE agree on **first-pick team in 245/245** and **map side in 245/245**, including all **74 games where OE says red had first pick**. Ban order agrees in 245/245. Pick order and the full 20-action sequence agree in 244/245. The one exception is an OE error (picks stored in role order; see D1).
- Every game on the fetched pages was also compared, at no extra request cost: 1,444 matched games, 418 of them red-first. First pick and side agree in **1444/1444**. The full sequence agrees in 1437/1444, and the 7 differences fall into 3 small classes (below). **None of them involves first pick or side.**
- **OE's `first_pick_side` / `first_pick_team` (and so the red-first-pick drafts of 2026) are confirmed.** The draft order in `draft.py` matches Leaguepedia's layout.
- Total live requests to lol.fandom.com: **23**, with no rate-limit errors. Everything is cached under `data/raw/leaguepedia/` (8.6 MB), so a rerun makes 0 requests.

## Coordinator question: LCK Cup 2026 opening day (2026-01-14)

**OE is right on both side and first pick for all five games.** A news report said KT were on blue with DNS on first pick in game 1. That is the configuration of **game 3**, not game 1.

| OE game_id | Game | OE: blue / red / first pick | Leaguepedia MatchSchedule (`Data:LCK/2026 Season/Cup`) | Leaguepedia Scoreboard (blue players) | Leaguepedia PicksAndBans | Verdict |
|---|---|---|---|---|---|---|
| LOLTMNT03_337058 | KT-DNS g1 | DNS / KT / **KT (red)** | blue=DN SOOPers, red=KT, first_sel=KT, ssel=DN SOOPers, pick_sel=KT, first_pick=KT | team1 (blue) = DN SOOPers: DuDu, Pyosik, Clozer, deokdam, Life | blueteam=KT Rolster, switched_sides=yes | OE correct |
| LOLTMNT03_337069 | KT-DNS g2 | DNS / KT / KT (red) | blue=DNS, red=KT, first_pick=KT | blue = DNS players | blueteam=KT, switched_sides=yes | OE correct |
| LOLTMNT03_337081 | KT-DNS g3 | **KT / DNS / DNS (red)** | blue=KT, red=DNS, first_sel=DNS (lost g2), first_pick=DNS | blue = KT players | blueteam=DN SOOPers, switched_sides=yes | OE correct |
| LOLTMNT03_337109 | DK-BRO g1 | DK / BRO / **BRO (red)** | blue=Dplus, red=BRION, first_sel=Dplus, ssel=Dplus, pick_sel=BRION, first_pick=BRION | blue = Siwoo, Lucid, ShowMaker, Smash, Career | blueteam=BRION, switched_sides=yes | OE correct |
| LOLTMNT03_336084 | DK-BRO g2 | DK / BRO / BRO (red) | blue=Dplus, red=BRION, first_sel=BRION (lost g1), first_pick=BRION | blue = DK players | blueteam=BRION, switched_sides=yes | OE correct |

The full 20-action sequences of games 337058, 337081 and 337109 agree with OE action by action. The tests check them by hand from the page source (see "Tests"). The side evidence is strong: the Scoreboard is keyed to the Riot game id and lists DNS's five players on blue. The first-pick evidence comes from two separately edited wiki templates (MatchSchedule `first_pick` and the PicksAndBans slot order), and both agree with OE. They also tell a consistent First Selection story. In g1, KT won first selection and took first pick, and DNS then took blue. In g3, DNS (the loser of g2) took first pick, and KT then took blue.

## How Leaguepedia was read (what worked, what did not)

- **Anonymous page source (`action=query&prop=revisions`) works.** It returned HTTP 200 every time. The 23 requests break down as follows:
  - 17 `list=allpages` calls to discover page names; most of these were exploratory.
  - 6 `prop=revisions` calls, which fetched 56 pages in batches of up to 20 titles. These pages include the 2 Lua name modules.
- **Cargo route**: implemented in `Client.cargo()`, with mwclient login when `FANDOM_USER` and `FANDOM_BOT_PASSWORD` are set. It is **UNTESTED**: no credentials exist. I did not re-test anonymous `cargoquery`, because docs/research_leaguepedia.md had already shown it rate-limited.
- Client behaviour: at most 1 request per 2 s; exponential backoff (4, 8, 16 s ...) on `ratelimited`, HTTP 429/5xx and network errors; User-Agent `lol-draft-analyzer/0.1`. Every raw response is stored in `data/raw/leaguepedia/api/<action>_<name>_<sha1>.json` and every page in `data/raw/leaguepedia/pages/`. Anything cached is never requested again.
- **Name resolution uses the wiki's own lookup modules.** These are `Module:Championnames` (1,005 aliases, including editor shorthands like `RG`, `XZ`, `ksa`, `wu` and Korean names) and `Module:Teamnames` (22,537 aliases). After resolution, every champion on the fetched pages has an OE spelling. Without this step about 1/3 of champion tokens would not have matched.

### Research claims checked against the real page source

| Claim (docs/research_leaguepedia.md) | Result |
|---|---|
| Picks/bans are in PicksAndBansS7 templates on `<Overview>/Picks and Bans[/<sub>]` pages | **Confirmed.** Tabs are the level-2 headings. The game number is `team1score+team2score`, cross-checked with `game1=yes`. |
| 2026: template Team1 (`blueteam=`) is the team that drafts **first**; `switched_sides=yes` means Team1 was on red | **Confirmed.** Side derived this way equals OE's side in 1443/1444 games (1 editor slip, I4 below). `first_pick` equals Team1 in 706/709 2026 games (3 slips, I1-I3). In 2026, `switched_sides` was blank in 16 games, and Team1 was blue in all of them. |
| Before 2026, Team1 = blue = first pick | **Confirmed.** `switched_sides` is never set on 2024/2025 pages, and PB-derived side = OE side in all 735. |
| MatchSchedule data pages carry `first_pick` and the Riot platform game id | **Confirmed** for LCK, LEC, LCS, LTA N, LCP, MSI and Worlds. `first_pick`/`first_sel`/`ssel`/`pick_sel` exist only on 2026 pages. **LPL data pages have no Riot id and no GRID id**, so LPL is matched by the fallback. |
| Riot platform game id == OE game_id for Riot-hosted leagues | **Confirmed.** 1,080 direct matches and no id collisions. |

## Sample

Whole tabs (usually week 1 of a split) were taken from the pages below. For LCK 2025/2024 and LPL, the first N matches of the tab were used to keep the sample near 200. LCK Cup 2026 Week 1 was included at the coordinator's request.

| Season | League | Page (tab) | Games | OE red first pick | Matched by |
|---|---|---|---|---|---|
| 2026 | LCK | Cup / Group Stage (Week 1) + Rounds 1-2 (Week 1) | 45 | 30 | Riot id |
| 2026 | LEC | Versus Season (Week 1) | 18 | 8 | Riot id |
| 2026 | LPL | Split 1 (Week 1, first 8 matches) | 19 | 15 | date+teams+game |
| 2026 | LCS | Lock-In (Swiss Round 1) | 9 | 7 | Riot id |
| 2026 | LCP | Split 1 (Week 1) | 14 | 5 | Riot id |
| 2026 | MSI | 2026 MSI Play-In (all 4 days) | 18 | 9 | Riot id |
| 2025 | LCK | Rounds 1-2 (Week 1, 6 matches) | 14 | 0 | Riot id |
| 2025 | LEC | Winter Season (Week 1) | 15 | 0 | Riot id |
| 2025 | LPL | Split 2 (Week 1, 5 matches) | 13 | 0 | date+teams+game |
| 2025 | LTA N | Split 2 (Week 1) | 8 | 0 | Riot id |
| 2025 | Worlds | Main Event Swiss (Round 1) | 8 | 0 | Riot id |
| 2024 | LCK | Spring (Week 1, 5 matches) | 11 | 0 | Riot id |
| 2024 | LEC | Summer (Week 1) | 15 | 0 | Riot id |
| 2024 | LCS | Summer (Week 1) | 10 | 0 | Riot id |
| 2024 | LPL | Spring (Week 1, 5 matches) | 12 | 0 | date+teams+game |
| 2024 | Worlds | Main Event (Quarterfinals) | 16 | 0 | Riot id |
| **Total** | | | **245** | **74** (of 123 in 2026) | 201 Riot id, 44 fallback |

**Extended set ("all fetched pages", parquet `stratum == "extended"`).** This covers every other game on the same pages plus 7 extra pages fetched in one request to look at OE role-order suspects: 1,238 more games, 1,199 matched. With the sample that makes 1,444 matched games: 305 from 2024, 430 from 2025 and 709 from 2026 (418 red-first).

## Matching

1. PB game to schedule game, within the same overview page and tab: (both teams, game number). If a pair meets twice in one tab (this happens in LPL weeks), the k-th meeting in PB links to the k-th in the schedule. All 245 sample games linked.
2. Leaguepedia game to OE: **Riot platform game id == OE `game_id`** (201/245). Otherwise **fallback**: same OE league, same two teams, same game number in series, and OE start within ±36 h of the schedule's match start (44/245; all LPL). There were no ambiguous fallbacks and no OE game was matched twice.
3. **Match rate: 245/245 in the sample.** In the extended set, 39 games are unmatched, all LPL 2025 Split 2 Week 5. Their schedule is on a `Data:` continuation page I did not fetch, so they have no date ("no date (PB game not linked to a schedule game)"). They are listed in the parquet with `matched=False` and the reason.

**Team-name normalization.** Steps:
- The wiki name is resolved through `Module:Teamnames` (for example `KT`→KT Rolster, `HRTS`→Team Heretics, `AL CN`→Anyone's Legend, `LYON24`→LYON (2024 American Team)).
- It is then mapped onto the OE team names of that league-season. Rules are tried in order: an exact key match after accent folding, dropping parentheticals and keeping only lowercase alphanumerics; then the explicit `TEAM_ALIASES`; then **unique** containment.

What needed more than the wiki module:

- **Explicit aliases.** OE back-fills the *current* franchise name onto older seasons, and two 2026 short names are separate wiki entries:
  - `Kwangdong Freecs`/`DN Freecs`/`KDF`/`DNF` → DN SOOPers
  - `OKSavingsBank BRION`/`OK BRO`/`OK BRION` → HANJIN BRION
  - `KRX` → Kiwoom DRX
  - `SHFT` → Shifters
- **Containment:**
  - `BRION`→HANJIN BRION
  - `DRX`→Kiwoom DRX
  - `FearX`→BNK FEARX
  - `NIP.CN`/`Ninjas in Pyjamas.CN`→Ninjas in Pyjamas
  - `MVK`→MVK Esports
  - `SEN`/`Sentinels ESC`→Sentinels
- **Normalized:** `Dplus KIA`→Dplus Kia, `GIANTX`→GiantX, `Rogue (European Team)`→Rogue, `LYON (2024 American Team)`→LYON.

No team was left unresolved.

## Agreement rates

Columns in the parquet are `first_pick_agree`, `side_agree`, `ban_order_agree` (both teams, all 5 positions), `pick_order_agree`, `sequence_agree` (the 20 actions, built with `draft.interleave` on both sides) and `winner_agree`. First pick here means PB Team1 vs OE `first_pick_team`. The MatchSchedule `first_pick` field is compared separately (`ms_first_pick_agree`).

### Stratified sample (245 matched)

| | n | first pick | side | ban order | pick order | full sequence |
|---|---|---|---|---|---|---|
| all | 245 | 245 (100%) | 245 (100%) | 245 (100%) | 244 (99.6%) | 244 (99.6%) |
| 2024 | 64 | 64 | 64 | 64 | 64 | 64 |
| 2025 | 58 | 58 | 58 | 58 | 58 | 58 |
| 2026 | 123 | 123 | 123 | 123 | 122 (99.2%) | 122 (99.2%) |
| OE first pick = Blue | 171 | 171 | 171 | 171 | 171 | 171 |
| OE first pick = Red | 74 | 74 | 74 | 74 | 73 (98.6%) | 73 (98.6%) |
| 2026, Blue first | 49 | 49 | 49 | 49 | 49 | 49 |
| 2026, Red first | 74 | 74 | 74 | 74 | 73 | 73 |

In 2026 the MatchSchedule `first_pick` was filled for 123/123 games and agrees with OE in 123/123. Winner agrees in 245/245.

### All fetched pages (1,444 matched)

| | n | first pick | side | ban order | pick order | full sequence |
|---|---|---|---|---|---|---|
| all | 1444 | 1444 (100%) | 1444 (100%) | 1442 (99.9%) | 1439 (99.7%) | 1437 (99.5%) |
| 2024 | 305 | 305 | 305 | 304 | 305 | 304 |
| 2025 | 430 | 430 | 430 | 430 | 430 | 430 |
| 2026 | 709 | 709 | 709 | 708 | 704 | 703 (99.2%) |
| OE first pick = Blue | 1026 | 1026 | 1026 | 1025 | 1024 | 1023 (99.7%) |
| OE first pick = Red | 418 | 418 | 418 | 417 | 415 | 414 (99.0%) |

The MatchSchedule `first_pick` agrees with OE in 706/709 games from 2026 (see I1-I3). Winner agrees in 1444/1444.

## Every disagreement

Notation: "first"/"second" is the draft slot. Pick numbers are the team's own order.

| # | OE game_id | Game | Leaguepedia | OE | Class | Judgement |
|---|---|---|---|---|---|---|
| D1 (sample) | LOLTMNT06_164458 | LEC 2026 Versus W1, Movistar KOI (first, red) vs Shifters | MKOI picks Azir, Ambessa, Sivir, Rumble, Nautilus. SHFT picks Vi, Varus, Neeko, Cassiopeia, Aurora | MKOI Rumble, Ambessa, Azir, Sivir, Nautilus. SHFT Aurora, Vi, Cassiopeia, Varus, Neeko | **OE picks stored in role order** (both teams' pick1-5 are exactly top/jng/mid/bot/sup) | **Leaguepedia right.** OE's order is the role order, not a draft order. LP's is a normal draft (the editor typed it with roles m, j, b, t, s). First pick and side are unaffected. |
| D2 | LOLTMNT04_150288 | LEC 2026 Versus W2, SK Gaming (first, blue) vs Karmine Corp Blue | SK Varus, Rumble, Yone, Pantheon, Alistar. KCB Yunara, Jarvan IV, Aatrox, Azir, Rakan | SK Rumble, Pantheon, Yone, Varus, Alistar. KCB Aatrox, Jarvan IV, Azir, Yunara, Rakan | OE role order (same as D1) | **Leaguepedia right**, same reason. |
| D3 | LOLTMNT03_338517 | LCK Cup 2026 W2, Kiwoom DRX (first, red) vs DN SOOPers | DRX pick2/pick3 = Corki, Nocturne | Nocturne, Corki | **Swap of a same-team double pick** (actions 9 and 10 are both DRX's) | **Undecidable from text.** The two champions are locked back to back by the same team in the same window. Neither availability nor information differs. Low impact. |
| D4 | LOLTMNT05_214276 | LEC 2026 Summer W1, Team Vitality (first, red) vs G2 (second) | G2 pick1/pick2 = Jarvan IV, Viktor | Viktor, Jarvan IV | Double-pick swap (actions 7 and 8) | Undecidable, low impact. |
| D5 | LOLTMNT04_182527 | LEC 2026 Summer W3, GIANTX (first, blue) vs Movistar KOI (second) | MKOI pick1/pick2 = Viktor, Pantheon | Pantheon, Viktor | Double-pick swap (actions 7 and 8) | Undecidable, low impact. |
| D6 | LOLTMNT02_343221 | LCP 2026 S1 W2, MVK Esports (first, red) vs CTBC Flying Oyster | CFO ban3 = `NONE` | CFO ban3 = Azir, which MVK had **already banned** at its ban3 (OE flags `duplicate_champion_in_draft`) | **Impossible duplicate ban vs missed ban** | **Leaguepedia right in effect.** A champion cannot be banned twice, so the slot was a lost or no-op ban. OE probably records the hovered champion. For modelling, treat OE's duplicate as a missed ban. |
| D7 | 10699-10699_game_2 | LPL 2024 Spring W3, LGD (first, blue) vs Team WE | WE ban4 = `none`, ban5 = Rell | WE ban4 = Rell, ban5 = Rell (duplicate, flagged `duplicate_champion_in_draft`) | Duplicate ban vs missed ban | **Leaguepedia right in effect**, same as D6. These 2 are the only `duplicate_champion_in_draft` games in all of OE. |

No first-pick-flag flips and no side flips occurred between OE and Leaguepedia.

### Leaguepedia-internal inconsistencies (OE not involved; all in the extended set)

| # | Game | What | Judgement |
|---|---|---|---|
| I1 | LPL 2026 S1 W2, JD Gaming vs Bilibili g2 (13083-13083_game_2) | MatchSchedule `first_pick=BLG`, but the PB template puts JDG in the first-drafter slot (switched_sides=no, so JDG on blue). OE says JDG first, on blue. | Probably a MatchSchedule slip: `first_pick` was copied from `pick_sel`, but the team holding the pick choice can choose to pick second. PB and OE agree. Not verifiable without a VOD. |
| I2-I3 | LCP 2026 S1 W2, Fukuoka SoftBank HAWKS vs Ground Zero g1 and g2 (LOLTMNT02_343177/178) | MatchSchedule `first_pick=GZ` (= `pick_sel`); PB and OE: FSHG drafted first on blue | Same as I1. |
| I4 | LCP 2026 S2 W5, GAM vs Deep Cross g1 (LOLTMNT01_369848) | PB has GAM in the first slot with `switched_sides=no`. The schedule says blue = Deep Cross, `first_pick=GAM`. OE: Deep Cross blue, GAM first. | PB editor forgot `switched_sides=yes`. OE and the schedule agree. My side comparison uses the schedule's blue/red when linked, so the agreement figures are unaffected. |

### OE role-order suspects

In all of OE, 49 games have **both** teams' `pick1..5` equal to their top/jng/mid/bot/sup champions (`oe_role_order_suspect` column). I compared 12 of them against Leaguepedia:

- **2 are confirmed OE errors** (D1, D2; both LEC 2026 Versus, January).
- **In the other 10, Leaguepedia has the same order as OE.**

So the heuristic over-flags. A rough independence estimate predicts about 20 such games by chance: one team's picks happen to be in role order in about 10% of games. I cannot tell whether the remaining 37 unchecked suspects are real. If both teams' picks equal role order, that is grounds for suspicion, not proof.

## Caveats and limits

- **Independence.** Leaguepedia's side comes from Scoreboards, which are imported from the Riot game record (the same upstream as OE). Leaguepedia's draft order and first-pick fields are typed by editors (lowercase shorthands, typos like `Thundertalk Gaming`). The D1-D5 differences show PB is not a copy of OE. But if an editor copied the order from an OE-like source, matching errors could go unnoticed. VODs were not checked.
- **Editor conventions are unwritten.** "Team1 = first drafter" in 2026 is an observed convention (99.7-100% consistent here), not a template field. The template still calls the parameter `blueteam`. The MatchSchedule `first_pick` field slipped in 3/709 games and PB `switched_sides` in 1/709.
- **Double-pick order (D3-D5)** is the noisiest part of any source. Models should not lean on the order within a same-team pick pair.
- **Sample bias.** The stratified sample is mostly week 1 of each split, which are early patches. The extended set (1,444 games, more weeks) reduces but does not remove this. Some strata are small: LTA N 8, Worlds 2025 8, LCS 2026 9. LTA S, FST and EWC were not sampled.
- **Dates.** Schedule times are match starts, converted with fixed offsets (PST −8, CET +1, +1 h when `dst=yes`; KST/CST have no DST). They are only used for the ±36 h fallback window.
- **Not done.** The Cargo route is untested; anonymous `cargoquery` was not retried. LPL 2025 Week 5 is unmatched because I skipped its continuation `Data:` page. Scoreboard ban order (a third source) was read for the coordinator's games only, not parsed in code.

## Tests (`tests/test_leaguepedia.py`, no network)

Fixtures in `tests/fixtures/` are real page-source excerpts:
- LCK Cup 2026 Group Stage Week 1 PicksAndBans (5 games)
- the matching `Data:` MatchSchedule (2 matches)
- the first LCK 2025 Rounds 1-2 game
- small excerpts of `Module:Championnames` and `Module:Teamnames`

The tests cover:
- the template parser, including nested braces
- the PB and schedule parsers (tabs, game numbers, `switched_sides`, UTC conversion)
- Lua alias resolution (`RG`, `XZ`, pointer chains, `none`/`loss of ban` mapping to None)
- team normalization and resolver rules, including a refused ambiguous containment
- PB-to-schedule linking and the OE join fallback (wrong game number, out-of-window date, missing date)
- client caching, throttle and backoff with a mocked session

Four **hand-checked games** assert the full expected 20-action sequence from the fixture and from OE's `draft_actions.parquet`; the OE asserts skip if the parquet is absent:
- LOLTMNT03_337058 (KT, red first pick)
- LOLTMNT03_337081 (DNS, red first pick)
- LOLTMNT03_337109 (BRION, red first pick)
- LOLTMNT02_242547 (2025, blue first pick)

A further test shows that the template's line layout (picks and phase-1 bans) equals `draft.interleave`. This is an independent check of the order encoded in `draft.py`.
