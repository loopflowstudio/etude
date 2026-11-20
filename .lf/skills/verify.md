---
extends: builtin
---
When the Task includes an approved SDK release, use the repository’s release
workflow after merge. Install the exact `etude-sdk==<version>` from PyPI in a
fresh Python 3.12 environment outside the checkout, then run `etude --help`
from that environment. Report the verified pin before completing the Task.
