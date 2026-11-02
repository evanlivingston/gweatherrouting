#!/usr/bin/env bash
set -euo pipefail

sudo add-apt-repository -y ppa:ubuntugis/ubuntugis-unstable
sudo apt-get -o Acquire::Retries=3 update
# libgdal-dev selects the matching runtime ABI on each Ubuntu release.
sudo apt-get -o Acquire::Retries=3 install -y libgdal-dev gdal-data
# gdal-plugins is not published in every PPA/Ubuntu combination; install it
# only when available so dependency setup does not fail on newer runners.
if apt-cache policy gdal-plugins 2>/dev/null | grep -q 'Candidate: .*[^)]'; then
    candidate="$(apt-cache policy gdal-plugins 2>/dev/null | sed -n 's/.*Candidate: //p' | head -1 | tr -d ' ')"
    if [ -n "$candidate" ] && [ "$candidate" != "(none)" ]; then
        sudo apt-get -o Acquire::Retries=3 install -y gdal-plugins
    fi
fi
sudo apt-get install -y libgtk-3-dev libgirepository1.0-dev gir1.2-gtk-3.0
