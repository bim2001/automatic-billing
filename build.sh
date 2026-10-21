#!/usr/bin/env bash
set -o errexit

export PYTHONPATH="$PWD/apartsystem${PYTHONPATH:+:$PYTHONPATH}"

pip install -r requirements.txt
python apartsystem/manage.py collectstatic --no-input
python apartsystem/manage.py migrate
