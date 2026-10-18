#!/bin/bash
# Move a downloaded ZIM into production, then refresh the kiwix library and
# menus.  The caller runs this with a checked return code, so abort on the
# first failure instead of reporting success for a partial install.
set -e

WORKINGDIR=/library/working/zims/
DESTDIR=/library/zims/content
SRC="$WORKINGDIR/$1"

# ZIM File

echo "Moving downloaded $1 to production"
mv "$SRC" "$DESTDIR"

echo "Re-indexing Kiwix Library"
/usr/bin/iiab-make-kiwix-lib

echo "Updating home page menues"
/usr/bin/iiab-update-menus
