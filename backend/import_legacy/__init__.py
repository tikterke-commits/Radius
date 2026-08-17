"""One-time SAS4 legacy import (Part F). Not imported by app.main or any
running-platform code path — invoked only as a standalone script
(`python -m import_legacy.run_import`), and only when settings.import_enabled
is explicitly true. After cutover this package should be treated as
historical: don't wire it back into the live system.
"""
