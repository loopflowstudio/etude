# v0.2.6

<!-- loopflow:release-notes=narrative;gate=safe -->

Etude v0.2.6 lets a rental run as long as your run and your storage allow, instead of cutting it off at about 12 hours. Long training no longer has to be split into segments that each pay for startup again, restart their streams and stage a continuation. The limit now comes from things you set (`max_hours`, dollar limits, the on-demand's price ceiling) and from how long your storage can give a machine access. Upgrading from 0.2.5 keeps existing stores working.

## Long runs stay on one machine

`OnDemand.max_rental_seconds` (default 43,000 s) is gone. It capped every rental at about 12 hours whatever the run asked for. Now a rental lasts until the first of these runs out: the run's `max_hours`, its dollar limits, the on-demand's price ceiling, or the longest access grant its storage can issue.

| Storage | Longest grant | Mechanism |
| --- | --- | --- |
| R2 | 7 days | Cloudflare temporary credentials |
| S3 | 36 hours | federation keys |

- A session's storage grants, inbox link and bootstrap links last until the session's deadline. Before, they lasted a fixed 13 hours. Work added to a kept rental gets grants that last until that rental's deadline.
- If a run's `max_hours` plus `setup_seconds` is longer than the storage's grant, the launch is refused, and so is a `rent` that asks for longer. For example, a 37-hour run or a 37-hour `rent` on S3 is refused, while a 48-hour rental on R2 is accepted.
- A cohort's shared rental still stops at the storage's grant. The cohort's remaining runs move to a new rental.
- `etude.launch.storage.s3.longest_grant` is now the one place that decides how long each kind of storage can grant access.

([#46](https://github.com/loopflowstudio/etude/pull/46))

## Operational notes

- Older versions keep working; this release proves no compatibility break.
- On-demands saved with `max_rental_seconds` still load. The extra key is ignored, so the database schema stays the same. Any duration you stored there no longer has an effect. Set `max_hours` on the run, or rely on dollar limits, to cap how long a rental lasts.
- Launch and cohort asks each make one extra database query, to read the on-demand's storage. The result is cached per storage, because a storage never changes after it is added. The tests that pin query counts now expect the new numbers.
- S3-backed runs longer than 36 hours, counting setup, are now refused at launch. Before, they ran in 12-hour segments. Move such runs to R2, or shorten `max_hours`.