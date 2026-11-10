# Pro Draft Rules and Data Research (as of 2026-11-10)

## 1. First Selection (2026)

**Mechanics (verified in rulebook text).** The team holding the *Right of First Selection* (RoFS) picks exactly ONE of:
- map side (Blue/Red), or
- pick position (First Pick/Second Pick).

The opponent then chooses the remaining dimension. Submissions are simultaneous: the RoFS team submits one choice, and the non-RoFS team submits a preference for both dimensions. Example: A has RoFS and takes "First Pick", so B picks a side, and B may take Blue. That means **Red side can now have first pick.**

**Who gets it:**
- **Game 1:** the higher seed. If a match has no seeding, a coin flip or equivalent decides (EWC 2026 rulebook §5.3.1). LEC regular season (Bo1) uses league-randomised RoFS assignment, with each team getting 5-6 RoFS games (LEC 2026 rulebook §6.2.3). For specific playoff matches the LEC declares which team "is considered the higher seed", e.g. the loser of the upper-bracket match. In the LEC Split Final, the upper-bracket team gets **both** choices in Game 1 (§6.8.4). LCP 2026 decides game 1 side selection with a 2v2 match (Leaguepedia LCP 2026 Split 1). In 2025 the LCP used a 1v1.
- **Games 2+:** "the losing Team of the previous Game will have the RoFS" (LEC §6.8.5; EWC §5.3.2, nearly verbatim). The choice is due within 5 minutes of the previous game ending.
- **Default if a team fails to submit:** the RoFS team gets First Pick and the other team gets Blue Side (LEC §6.8.6).

**Scope and start.** Riot announced the rule on 2026-01-08 ("First Selection will roll out in all regions at the start of the season"). It was first used at the LCK Cup opener on 2026-01-14, KT vs DNS. KT had RoFS and took Blue, then DNS took first pick, which was the first red-side first pick of the Riot era. Season start dates: LCK and LPL Jan 14, LCP Jan 16, LEC and CBLOL Jan 17, LCS Jan 24.

It applies to First Stand 2026. At First Stand, GEN had RoFS and took first pick, then G2 took blue. It also applies to MSI, Worlds and EWC 2026, whose rulebook has a RoFS section.

LPL: Riot says "all regions" and press lists the LPL. I found no LPL-specific rulebook text, so treat the LPL as following the same rule, unverified in detail.

**Implication for modelling.** From 2026, `side` and `firstPick` are independent. Before 2026, Blue always had first pick.

## 2. Fearless Draft history

| When | Event | Variant |
|---|---|---|
| 2022-07-14 | LDL Summer | **Soft**: a team can't re-pick its own previously played champions. The opponent is unaffected. |
| 2024 Spring | NACL | Hard. **Bo5 G4 skips ban phase 2. G5 has no bans.** |
| 2024 Summer | LCK CL | Hard. **Bo5 G5 reverts to normal Tournament Draft.** |
| 2024 Summer Placements | LPL (first top league) | Soft ("a team can only pick any champion once") |
| 2024 | Demacia Cup | Hard. Game 1 side decided by a 1v1. |
| 2025 Split 1 | LCK Cup (from 2025-01-15), LPL, LEC Winter, LTA, LCP, First Stand 2025 | Hard |
| 2025 (announced 2025-03-16) | All remaining 2025 competitions, including MSI and Worlds 2025 | Hard |
| 2026 | All leagues: LCK Cup, LPL, LEC, LCS, CBLOL, LCP, First Stand, MSI, Worlds (Bo3/Bo5; Worlds Swiss is Bo1), EWC | Hard |

**Hard Fearless rule (LEC §6.9, EWC §5.6.8):** any champion *picked* by either team in a game is unavailable to both teams for the rest of the match.
- **Bans do not carry over.** A champion banned in game 1 can be picked later if nobody picked it.
- **No game-5 exception in the tier-1 2025/2026 rulebooks.** A Bo5 game 5 has 40 locked champions plus 10 bans.

The game-5 exceptions you remembered were real, but only in 2024 tier-2 variants (NACL, LCK CL).

**2026 tweaks:** none to Fearless itself. First Selection is the 2026 change.

## 3. Draft order (confirmed)

LEC 2026 §7.8.6 and EWC 2026 §5.6.5 both give this order, with "1st pick Team = A; 2nd pick Team = B":
- Bans: A B A B A B
- Picks: A B B A A B
- Bans: B A B A
- Picks: B A A B

This matches your F,S,F,S,F,S / F,S,S,F,F,S / S,F,S,F / S,F,F,S.

**Important:** "F" is the *first-pick team*, not Blue. In 2026 it can be Red.

## 4. Oracle's Elixir data

**Downloads.** Download folder (from the `oracleselixir.com/tools/downloads` JS bundle): `https://drive.google.com/drive/u/1/folders/1gLSw0RLjBbtaNy0dgnGQDAZOHIgCe-HH`, folder ID **`1gLSw0RLjBbtaNy0dgnGQDAZOHIgCe-HH`**. Files are named `{YEAR}_LoL_esports_match_data_from_OraclesElixir.csv` and updated once per day. File IDs:
- 2025: `1v6LRphp2kYciU4SXp0PCjEMuev1bDejc`
- 2026: `1hnpbrUpBMS1TZI7IovfpKeZfWJH1Aptm`
- 2024: `1IjIEhLc9n8eLKeY-yh_YigKVWbhgGBsN`

The folder listing at `https://drive.google.com/embeddedfolderview?id=<folder>` has links to each file. Direct download URL: `https://drive.usercontent.google.com/download?id=<fileid>&export=download&confirm=t`.

**Warning:** Drive frequently returns a "Quota exceeded" HTML page. The 2026 file returned it on every attempt. Check for `<!DOCTYPE html` before parsing, and retry or cache.

**Columns.** Verified from the 2025 file header; 165 columns in total. Leading columns:
`gameid, datacompleteness, url, league, year, split, playoffs, date, game, patch, participantid, side, position, playername, playerid, teamname, teamid, firstPick, champion, ban1..ban5, pick1..pick5, gamelength, result, kills, deaths, assists, ...`

Later columns cover objectives (`firstdragon`, `void_grubs`, `atakhans`, `firstbaron`, `firsttower`, ...) and at-10/15/20/25 stats (`goldat10`, `golddiffat15`, ...).

Row layout and values:
- **12 rows per game:** participantid 1-10 are players, 100 is the Blue team row and 200 is the Red team row. Team rows have `position=='team'`.
- `side` is `Blue` or `Red`. `firstPick` is 1 or 0. `game` is the game number within the series. `datacompleteness` is `complete` or `partial`.
- `pick1..pick5` are filled on team rows in that team's **draft order**, not role order. Sample: the Blue team row lists Maokai, Jinx, Leona, Hwei, Gnar, while its top laner played Gnar.
- `ban1..ban5` are the team's bans in order: ban1-3 are phase 1 and ban4-5 are phase 2. The bans are also copied onto player rows.
- There is **no series-ID column**. You have to derive series from league, date, teams and `game`.

**Not verified:** whether the 2026 file populates `firstPick` independently of side (the 2026 download hit the quota). Check in 2026 data that `firstPick==1` appears on Red rows.

## 5. League names and OE codes

**LTA and LCS.** The LTA lasted only 2025, with conferences LTA North and LTA South. On 2025-09-28 Riot announced the LTA would be dissolved and the **LCS and CBLOL would return** as separate leagues in 2026. The LCS covers North and Central America and the CBLOL covers South America. The LCS 2026 started on Jan 24 with Lock-In. The Americas Cup was retained. The LCP (Pacific) started in 2025, replacing PCS/VCS at tier 1.

**OE `league` codes** (seen in OE-consuming public repos; not verified against the 2026 CSV):

| Code | Competition |
|---|---|
| `LCK` | LCK |
| `LPL` | LPL |
| `LEC` | LEC |
| `LCS` | LCS, pre-2025 and 2026 |
| `LTA N`, `LTA S` | LTA 2025 conferences |
| `LTA` | LTA 2025 cross-conference stages |
| `CBLOL` | CBLOL |
| `LCP` | LCP, from 2025 |
| `PCS`, `VCS`, `LJL`, `LLA` | Pre-2025 leagues |
| `MSI` | Mid-Season Invitational |
| `WLDs` | Worlds |
| `FST` | First Stand |
| `LCKC` | LCK CL |
| `LDL` | LDL |

Many projects alias `LTA N` to LCS and `LTA S` to CBLOL so the LCS and CBLOL series stay continuous. I could not confirm a code for EWC or the Americas Cup. Run `df.league.unique()` on the 2026 file to check.

## Sources
- LEC 2026 Rulebook v3.1: https://static.wikia.nocookie.net/lolesports_gamepedia_en/images/c/cf/LEC_2026_Rulebook.pdf/revision/latest?cb=20260202160035
- EWC 2026 LoL Rulebook: https://cdn.esportsworldcup.com/resources/uploads/EWC_26_League_of_Legends_Rulebook_a31ae07a83.pdf
- LoL Esports, Season start 2026: https://lolesports.com/en-US/news/season-start-2026-lol-esports
- Esports News UK, LCK Cup opener: https://esports-news.co.uk/?p=175170
- esports.gg, First Selection explained: https://esports.gg/news/league-of-legends/first-selection-explained/
- GosuGamers, First Selection: https://www.gosugamers.net/lol/news/77812-lol-esports-2026-introduces-new-draft-rule-first-selection-and-a-bo5-heavy-first-stand
- Leaguepedia, 2025 LoL Esports Restructure (Fearless history): https://lol.fandom.com/wiki/2025_LOL_Esports_Restructure
- Leaguepedia event pages (format sections): LCK 2025 Cup, LPL 2024 Summer Placements, Demacia Cup 2024, LCP 2025 Kickoff / 2026 Split 1, LTA North 2025, First Stand 2026, LPL / CBLOL 2026 Split 1
- GosuGamers, Fearless for all 2025: https://www.gosugamers.net/lol/news/74517-fearless-draft-confirmed-to-stay-throughout-season-2025
- Strafe, Fearless returns 2026: https://www.strafe.com/news/read/fearless-draft-returns-first-stand-msi-and-worlds-2026-locations-and-format-changes-revealed/
- Riot, LCS and CBLOL return: https://www.leagueoflegends.com/en-us/news/lcs-and-cblol-return/
- esports.gg, LCS/CBLOL 2026 formats: https://esports.gg/news/league-of-legends/lcs-cblol-formats-2026-season/
- Oracle's Elixir downloads page and JS bundle: https://oracleselixir.com/tools/downloads
- 2025 OE CSV header (downloaded directly)
- OE league codes in public repos, e.g. github.com/LasseRogers/git-rich and kushagra8855/lol-pickem-predictor
