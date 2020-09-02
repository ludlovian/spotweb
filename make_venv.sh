#!/bin/bash

DIR="${0%/*}/env"
DEPS="clize pyspotify waitress"

echo "Making venv in $DIR"
python3 -m venv "$DIR"

echo "Adding depdencies: $DEPS"
$DIR/bin/pip install $DEPS
