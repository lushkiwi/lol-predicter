# Leaguepedia Cargo API — research findings (2026-11-10)

All tests were run from this machine with `requests`, using the User-Agent `lol-draft-analyzer-research/0.1`.
I waited at least 3 s between calls. In total I sent 13 requests to `lol.fandom.com`: 4 were `cargoquery`, 1 was an `index.php` page and the rest were metadata or wikitext reads.

## TL;DR

| Question | Answer |
|---|---|
| Do anonymous `action=cargoquery` calls work? | **No, not in practice.** All 4 anonymous cargoquery calls returned `{"error":{"code":"ratelimited"}}`, including the very first one of the session and one sent after a 90 s cool-down. |
| What does work anonymously? | `action=cargofields` (the schema) and `action=query&prop=revisions` (page wikitext). Both returned HTTP 200 every time. |
| What do you need for Cargo queries? | Log in with a **Bot Password** (grant: "Create, query and delete data through the Cargo extension") on an account with a **confirmed email** (official docs, quoted below). |
| Row limit per query | **500** for normal users, including bot-password users. **5,000** only for wiki admins. `limit=max` is allowed. The default is 50. |
| `index.php` / Special pages (Special:CargoTables, Special:CargoExport, `?action=raw`) | Behind a **Cloudflare managed challenge** (HTTP 403 "Just a moment...") for non-browser clients. Use `api.php` only. |
| Is `Team1` blue side? | In **ScoreboardGames**: yes, always (map side). In **PicksAndBansS7**, before 2026 it is effectively yes. **From 2026, Team1 is the team that drafts first.** The `SwitchedSides=true` flag means that team actually played on red. See section 3. |
| First-pick field | Not in PicksAndBansS7. It is in **MatchScheduleGame**: `FirstPick`, `FirstSelection`, `Selection` (side selection) and `PickSelection`, each holding a team name. |
| How to join PB and SG | `PicksAndBansS7.GameId = ScoreboardGames.GameId`. Both use the format `OverviewPage_Tab_MatchInTab_GameInMatch`. |
| Joining to Oracle's Elixir | `ScoreboardGames.RiotPlatformGameId` (also in MatchScheduleGame), e.g. `LOLTMNT02_242547`. This matches the format of OE's `gameid` for Riot-hosted leagues (LCK/LEC/LCS). Check this against an actual OE CSV. LPL rows will not match this way (see `GridSeriesId`). |

## 1. Access rules and what happened

### Official docs (Help:Leaguepedia API, revision 2026-01-30, fetched via the API)

> If you want to make cargo queries through the API, it is really important **to log in with a Bot Password and have your email address confirmed first**, as Fandom added ratelimits that make it practically impossible to execute queries while logged out.

> Limits: 5,000 results per query if you are an admin; 500 result per query otherwise

The guide `User:Arbolitoloco/Avoiding cargo ratelimits` (2026-01-30) gives these steps:
1. Go to Special:BotPasswords and create a bot with the **"Create, query and delete data through the Cargo extension"** grant.
2. Confirm your email at Special:Preferences.
3. Optionally install a patched mwcleric that retries rate-limited queries: `pip install git+https://github.com/arbolitoloco1/mwcleric@empty_string_fix`, or use `mwrogue`'s `EsportsClient('lol', credentials=...)`.

So your premise of 500 anonymous / 5,000 bot is slightly off. The split is 500 for non-admins (including bots) and 5,000 for admins. The real problem with anonymous access is the rate limit, not the row cap.

### Empirical log

| # | Request | Result |
|---|---|---|
| 1 | `action=cargofields&table=PicksAndBansS7` | 200 OK, full schema |
| 2 | `action=cargofields&table=ScoreboardGames` | 200 OK, full schema |
| 3 | `cargoquery` PB⋈SG join, LCK 2025, limit 1 (first cargoquery of the session) | 200, `ratelimited` |
| 4 | `cargoquery` PB⋈SG, 2026 LCK/LEC, limit 40 (3 s later) | 200, `ratelimited` |
| 5 | (90 s pause) minimal `cargoquery` on ScoreboardGames, 4 fields, limit 2, no join | 200, `ratelimited` |
| 6 | `index.php?title=Help:Leaguepedia_API&action=raw` | **403**, Cloudflare challenge page |
| 7–10, 12 | `action=query&prop=revisions` / `list=allpages|search` (wikitext) | 200 OK each time |
| 11 | (several minutes later) `cargoquery` PB⋈SG, LCK 2025 Rounds 1-2, limit 1 | 200, `ratelimited` |

The error body is:
```json
{"error":{"code":"ratelimited","info":"You've exceeded your rate limit. Please wait some time and try again.","*":"See https://lol.fandom.com/api.php for API usage. ..."}}
```
- There was no `Retry-After` or rate-limit header. The response has HTTP status **200**, so check `error.code` in the JSON, not the status code.
- Response headers were `Cache-Control: private, must-revalidate, max-age=0, no-store`.
- The very first cargoquery was refused. Anonymous cargo is either budgeted at roughly zero, or the limit is per IP and shared. Either way, **plan on authenticated access from day one.**

### Implications for the scraper
- Use `mwclient.Site('lol.fandom.com', path='/')`, then `site.login('User@BotName', '<botpassword>')`, then `site.api('cargoquery', tables=..., fields=..., where=..., limit=500, offset=N)`. You can also use `mwrogue`.
- Page through with `offset`, 500 rows per page. Retry with backoff whenever `error.code == 'ratelimited'`. Keep a delay of 1–2 s or more between calls.
- Without login you can still read the schema (`cargofields`) and raw wikitext (`prop=revisions`). As a last resort you could parse the PicksAndBansS7 and Scoreboard templates out of page wikitext, which is what section 4 does.

## 2. Schemas (exact field names, from `action=cargofields`)

### PicksAndBansS7
```
Team1Role1..Team1Role5, Team2Role1..Team2Role5          String
Team1Ban1..Team1Ban5,   Team2Ban1..Team2Ban5            String
Team1Pick1..Team1Pick5, Team2Pick1..Team2Pick5          String
Team1, Team2                                            String
Winner                                                  Integer (1 or 2, relative to PB Team1/Team2)
Team1Score, Team2Score                                  Integer (series score after this game)
SwitchedSides                                           Boolean
Team1PicksByRoleOrder, Team2PicksByRoleOrder            String ("string of picks by role order for comparing with ScoreboardGames")
OverviewPage, Phase, UniqueLine, Tab                    String
IsComplete, IsFilled                                    Boolean
N_Page, N_TabInPage, N_MatchInPage, N_GameInPage,
N_GameInMatch, N_MatchInTab, N_GameInTab                Integer
GameId    String  "OverviewPage_Tab_MatchInTab_GameInMatch"
MatchId   String  "OverviewPage_Tab_MatchInTab"
GameID_Wiki String  DEPRECATED
```
The descriptions come from `Module:CargoDeclare/PicksAndBansS7`. For example, Team1Role1 is "Role of the first-picked blue-side champion".

**How draft order is encoded.** Template:PicksAndBansS7 stores picks and bans per team in champion-select order, before any swaps. The global draft order is implied by the slot numbers, not stored:

```
Ban phase 1:  T1Ban1, T2Ban1, T1Ban2, T2Ban2, T1Ban3, T2Ban3
Pick phase 1: T1Pick1, T2Pick1, T2Pick2, T1Pick2, T1Pick3, T2Pick3
Ban phase 2:  T2Ban4, T1Ban4, T2Ban5, T1Ban5
Pick phase 2: T2Pick4, T1Pick4, T1Pick5, T2Pick5
```
- `TeamNRoleK` is the role of `TeamNPickK`. Values are normalized to Top, Jungle, Mid, Bot or Support. Editors may leave them as TBD if `noroles=yes`.
- Special values include `Loss of Ban`, `None` and `Missing Data`.
- `Team1PicksByRoleOrder` lets you match against ScoreboardGames.TeamNPicks, which is ordered by role.

### ScoreboardGames
```
OverviewPage, Tournament, Team1, Team2, WinTeam, LossTeam         String
DateTime_UTC                                                      Datetime
DST                                                               String
Team1Score, Team2Score, Winner                                    Integer
Gamelength (String "mm:ss"), Gamelength_Number                    Float (minutes)
Team1Bans, Team2Bans, Team1Picks, Team2Picks,
Team1Players, Team2Players                                        List (,) of String
Team1Dragons, Team2Dragons, Team{1,2}{Clouds,Infernals,Mountains,Oceans,Hextechs,Chemtechs,Elders}
Team{1,2}Barons, Team{1,2}Towers, Team{1,2}Kills, Team{1,2}RiftHeralds,
Team{1,2}VoidGrubs, Team{1,2}Atakhans, Team{1,2}Inhibitors          Integer
Team1Gold, Team2Gold                                              Float
Patch, LegacyPatch, PatchSort, MatchHistory                       String
VOD                                                               Wikitext
N_Page, N_MatchInTab, N_MatchInPage, N_GameInMatch                Integer
Gamename, UniqueLine, GameId, MatchId                             String
RiotPlatformGameId, RiotPlatformId, RiotGameId, RiotHash          String
RiotVersion                                                       Integer
```
- In ScoreboardGames, Team1 is blue side and Team2 is red side, because the Scoreboard template uses `blue1..5` and `red1..5` under team1/team2.
- `TeamNPicks` and `TeamNPlayers` are in **role order** (Top, Jungle, Mid, Bot, Support), not draft order.
- `TeamNBans` is in ban order.
- In a cargoquery, a list field returns a comma-joined string. Use `HOLDS` in `where`.

### MatchScheduleGame (relevant to 2026), from `Module:CargoDeclare/MatchScheduleGame`
`Blue, Red, Winner, Selection` (the team with side selection), `PickSelection, FirstPick, FirstSelection, HasSelection, GameId, MatchId, OverviewPage, N_GameInMatch, RiotPlatformGameId, RiotPlatformId, RiotGameId, GridSeriesId` (GRID series id for LPL), `FF, IsRemake, IsChronobreak, MVP, Vod...`

The `FirstPick`, `FirstSelection`, `Selection` and `PickSelection` fields hold **team names**. They come from the 2026 template parameters `first_sel`, `ssel`, `pick_sel` and `first_pick` (Template:MatchSchedule/Game/doc, updated 2026-01-10).

## 3. 2026 "Right of First Selection" and how PicksAndBansS7 encodes it

The wiki article "Right of First Selection" (2026-01-09) says that from 2026, the team with first selection picks either a map side or draft order (first or second pick), and the opponent picks the other. So first pick is no longer tied to blue side.

PicksAndBansS7 and Module:PicksAndBans have **no first-pick field**. The module never refers to "first". The editing convention I observed in 2026 LCK data is that **the template's "blue" slot holds the team that drafts first, and `switched_sides=yes` marks a first-picker that actually played on red.**

Here is the evidence from LCK 2026 Rounds 1-2, Week 1. MatchSchedule data is from `Data:LCK/2026 Season/Rounds 1-2`, and picks and bans are from `LCK/2026 Season/Rounds 1-2/Picks and Bans/1-3`:

| Game | MatchScheduleGame (blue / red / first_pick / winner) | PicksAndBansS7 (Team1 / Team2 / SwitchedSides / Winner) |
|---|---|---|
| HLE vs BRO g1 (`LOLTMNT02_375568`) | blue=HANJIN BRION, red=HLE, first_pick=HLE, winner=2 (HLE) | blueteam=Hanwha Life Esports, redteam=HANJIN BRION, switched_sides=yes, winner=1 (HLE) |
| HLE vs BRO g2 (`LOLTMNT02_375577`) | blue=HLE, red=HANJIN BRION, first_pick=HANJIN BRION, winner=1 (HLE) | blueteam=HANJIN BRION, redteam=HLE, switched_sides=yes, winner=2 (HLE) |
| T1 vs KT g1 (`LOLTMNT02_374657`) | blue=KT, red=T1, first_pick=T1, winner=1 (KT) | blueteam=T1, redteam=KT Rolster, switched_sides=yes, winner=2 (KT) |
| T1 vs KT g2 | blue=KT, red=T1, first_pick=T1 | blueteam=T1, redteam=KT, switched_sides=yes, winner=2 |
| DK vs NS g1 | blue=NS, red=DPLUS, first_pick=DPLUS | blueteam=Dplus Kia, redteam=Nongshim RedForce, switched_sides=yes |

Rules for the analyzer:
- **PB.Team1 is the first-drafting team.** PB.Team1Ban1 is the first ban and PB.Team1Pick1 is the first pick of the draft.
- **Map side = PB.Team1 is blue unless PB.SwitchedSides is true, in which case PB.Team1 is red.** Before 2026, SwitchedSides was a rare edge case.
- **PB.Winner is relative to PB.Team1/Team2.** It can therefore be the opposite number from SG.Winner and MSG.Winner, which are relative to blue/red.
- When SwitchedSides is true, PB.Team1 equals SG.Team2. Join on GameId, never on the team columns.
- Cross-check against `MatchScheduleGame.FirstPick` (team name) when it is filled. These are editor conventions, so some games may be wrong; validate with an assertion such as `MSG.FirstPick == PB.Team1` (after normalizing team names).
- In these 5 LCK games, the team with first selection always took **first pick** and the opponent took **blue side**. Many 2026 games will therefore have the first picker on red.
- I could not check LEC 2026 empirically because cargoquery is rate-limited. The same template and module apply, but verify once you have authenticated access.

## 4. Joins

```
PicksAndBansS7=PB, ScoreboardGames=SG, MatchScheduleGame=MSG
join_on = PB.GameId=SG.GameId, PB.GameId=MSG.GameId
```
- The GameId format is `OverviewPage_Tab_MatchInTab_GameInMatch`, e.g. `LCK/2025 Season/Rounds 1-2_Week 1_1_1`.
- Alias every field when joining. Several names (GameId, Team1, Winner, OverviewPage and others) exist in more than one table.
- To reach Oracle's Elixir, use `SG.RiotPlatformGameId` (or `MSG.RiotPlatformGameId`), e.g. `LOLTMNT02_242547` or `ESPORTSTMNT04_2674408`. Split versions are `RiotPlatformId` (`LOLTMNT02`) and `RiotGameId` (`242547`). OE's `gameid` for Riot-hosted leagues uses this same `PLATFORM_GAMEID` form. Confirm this on a sample OE CSV before relying on it.
- Leaguepedia's `GameId` string never appears in OE. For LPL (Tencent-hosted) the Riot id is usually missing; `MSG.GridSeriesId` exists for those games.

## 5. Example: LCK 2025 Rounds 1-2, Week 1, HLE vs Gen.G, Game 1

Cargoquery was rate-limited, so **these rows are reconstructed from the source wikitext** that populates the tables. They are not literal API output. The sources were fetched via `prop=revisions`: `LCK/2025 Season/Rounds 1-2/Picks and Bans/1-3` and `LCK/2025 Season/Rounds 1-2/Scoreboards`.

Derived values (GameId, normalized names, UTC time) follow the module logic. Re-run the query below once you are authenticated to get the literal row.

Source template (PicksAndBansS7):
```
{{PicksAndBansS7|blueteam=Hanwha Life Esports|redteam=Gen.G
|team1score=0 |team2score=1 |winner=2
|blueban1= Varus    |red_ban1= Kalista
|blueban2= Skarner  |red_ban2= Yone
|blueban3= Rumble   |red_ban3= Gwen
|bluepick1= Naafiri   |bluerole1= j
                      |red_pick1= Jayce        |red_role1= t
                      |red_pick2= Miss Fortune |red_role2= a
|bluepick2= Ambessa   |bluerole2= t
|bluepick3= Ezreal    |bluerole3= a
                      |red_pick3= Vi           |red_role3= j
|blueban4= Rell     |red_ban4= Ryze
|blueban5= Poppy    |red_ban5= Sylas
                      |red_pick4= Ahri         |red_role4= m
|bluepick4= Aurora    |bluerole4= m
|bluepick5= Alistar   |bluerole5= s
                      |red_pick5= Rakan        |red_role5= s
|game1=yes }}
```

Expected **PicksAndBansS7** row:
```
OverviewPage   = LCK/2025 Season/Rounds 1-2
Tab            = Week 1
GameId         = LCK/2025 Season/Rounds 1-2_Week 1_1_1
MatchId        = LCK/2025 Season/Rounds 1-2_Week 1_1
N_GameInMatch  = 1
Team1 = Hanwha Life Esports   Team2 = Gen.G   Winner = 2   Team1Score = 0   Team2Score = 1
SwitchedSides  = (unset/false)
Team1Ban1..5   = Varus, Skarner, Rumble, Rell, Poppy
Team2Ban1..5   = Kalista, Yone, Gwen, Ryze, Sylas
Team1Pick1..5  = Naafiri, Ambessa, Ezreal, Aurora, Alistar
Team1Role1..5  = Jungle, Top, Bot, Mid, Support
Team2Pick1..5  = Jayce, Miss Fortune, Vi, Ahri, Rakan
Team2Role1..5  = Top, Bot, Jungle, Mid, Support
Team1PicksByRoleOrder ~ Ambessa,Naafiri,Aurora,Ezreal,Alistar   (exact delimiter unverified)
Team2PicksByRoleOrder ~ Jayce,Vi,Ahri,Miss Fortune,Rakan
IsComplete = true, IsFilled = true
```

Expected **ScoreboardGames** row:
```
OverviewPage = LCK/2025 Season/Rounds 1-2      Tournament = LCK 2025 Rounds 1-2
Team1 = Hanwha Life Esports (blue)            Team2 = Gen.G (red)
WinTeam = Gen.G   LossTeam = Hanwha Life Esports   Winner = 2
DateTime_UTC = 2025-04-02 08:11:00   (source: 10:11 CET, dst=yes)
Patch = 15.6   Gamelength = 28:31   Gamelength_Number ~ 28.52
Team1Bans  = Varus,Skarner,Rumble,Rell,Poppy
Team2Bans  = Kalista,Yone,Gwen,Ryze,Sylas
Team1Picks = Ambessa,Naafiri,Aurora,Ezreal,Alistar          (role order)
Team2Picks = Jayce,Vi,Ahri,Miss Fortune,Rakan
Team1Players = Zeus,Peanut,Zeka (Kim Geon-woo),Viper (Park Do-hyeon),Delight
Team2Players = Kiin,Canyon,Chovy,Ruler,Duro
Team1Gold = 42488  Team2Gold = 62311   Team1Kills = 2  Team2Kills = 24
Team1Dragons = 0   Team2Dragons = 4 (Mountain 1, Ocean 1, Hextech 2)
Team1Barons = 0    Team2Barons = 1     Team1Towers = 0  Team2Towers = 10
Team1RiftHeralds = 1  Team2RiftHeralds = 0   Team1VoidGrubs = 3  Team2VoidGrubs = 3
Team1Atakhans = 0  Team2Atakhans = 1   Team1Inhibitors = 0  Team2Inhibitors = 1
RiotPlatformGameId = LOLTMNT02_242547   RiotPlatformId = LOLTMNT02   RiotGameId = 242547   RiotVersion = 5
GameId = LCK/2025 Season/Rounds 1-2_Week 1_1_1
```

The query to run once you are logged in (it is the one that got rate-limited):
```python
site.api('cargoquery',
  tables='PicksAndBansS7=PB,ScoreboardGames=SG,MatchScheduleGame=MSG',
  join_on='PB.GameId=SG.GameId,PB.GameId=MSG.GameId',
  fields='PB.GameId=GameId,PB.Team1=PB_Team1,PB.Team2=PB_Team2,PB.SwitchedSides=SwitchedSides,PB.Winner=PB_Winner,'
         'PB.Team1Ban1,...,PB.Team2Pick5,PB.Team1Role1,...,'
         'SG.Team1=Blue,SG.Team2=Red,SG.Winner=SG_Winner,SG.Patch,SG.DateTime_UTC,SG.RiotPlatformGameId,'
         'MSG.FirstPick,MSG.FirstSelection',
  where="SG.OverviewPage LIKE 'LCK/2025%'",
  order_by='SG.DateTime_UTC', limit=500, offset=0)
```

## Sources (fetched via api.php `prop=revisions`)
- Help:Leaguepedia API (rev 2026-01-30). This is where the rate-limit warning and the 500/5,000 limits come from.
- User:Arbolitoloco/Avoiding cargo ratelimits (rev 2026-01-30)
- Template:PicksAndBansS7, Template:PicksAndBansS7/doc (rev 2026-01-06), Module:PicksAndBans (rev 2026-01-14)
- Module:CargoDeclare/PicksAndBansS7 (rev 2026-01-06), Module:CargoDeclare/MatchScheduleGame (rev 2026-07-27)
- Template:MatchSchedule/Game/doc (rev 2026-01-10), "Right of First Selection" (rev 2026-01-09)
- Data:LCK/2026 Season/Rounds 1-2, LCK/2026 Season/Rounds 1-2/Picks and Bans/1-3
- LCK/2025 Season/Rounds 1-2/Picks and Bans/1-3, LCK/2025 Season/Rounds 1-2/Scoreboards
