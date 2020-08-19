#!/bin/bash

NAME=spotweb
DIR=~/env
EXEC_VENV=/usr/local/env/exec_venv
DEPS="clize pyspotify waitress"

VENV="${1:-$DIR/$NAME}"

if [[ ! -d "$VENV" ]]; then
    echo "Making venv in $VENV"
    python3 -m venv "$VENV"
fi

echo "Adding dependencies to $VENV"
source <($EXEC_VENV -s "$VENV")

pip install $DEPS

source <($EXEC_VENV -d)
