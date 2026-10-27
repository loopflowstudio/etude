# v0.2.8

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.2.8 fixes how v0.2.7 read some older evaluation tables. Some of those tables had a game-specific `seat` column next to Etude's `player` column, and v0.2.7 swapped the two players' results. If you read eval results written before v0.2.7, upgrade. manabot's Elemental tables are one affected case. This patch changes nothing else.

## Older eval tables credit the right player again

v0.2.7 renamed `player` to `seat` everywhere in evaluation. To keep older stored tables readable, it renamed a stored `player` column to `seat`, but only when the table had no `seat` column already. Some tables written by earlier releases have both columns. In those tables, `player` is Etude's seat and `seat` is the game's own seat number. v0.2.7 read the game's seat as Etude's seat. So whenever the players sat the other way round, each player got the other's results. ([#61](https://github.com/loopflowstudio/etude/pull/61))

- A stored table's `player` column is always read as Etude's `seat`. A `seat` column next to it is read as `game_seat`.
- This rule is exact because rows written now can no longer contain a `player` column. A table with a `player` column must be an older one.
- manabot reproduced the bug across versions. It wrote a table with the published 0.2.5 and read it with this release. The scores were challenger 1.0 and heuristic 0.0, the same as 0.2.5 reports. v0.2.7 had reversed them.

## Operational notes

- The release lists no compatibility break. Stored eval records from every earlier etude-sdk version still load, and delivered matches are not played again.
- **A `play` that returns a column named `player` is now refused** with `column_name_refused`. In v0.2.7, such a row was accepted if it also named `seat` or `group`. Return Etude's seat as `seat`. If your game has its own seat number, return it as `game_seat`.
- Results that v0.2.7 computed from affected tables were wrong. Look again at any comparison you made with v0.2.7, such as a report page or a statsdb query. Read with v0.2.8, it shows the correct per-player rows. The stored data was never wrong, so nothing needs to be played again.