#!/bin/bash
cd /home/sfr/work/personal/daiv/daiv
PYTHONFAULTHANDLER=1 exec /home/sfr/work/personal/daiv/.venv/bin/python -u -m automation.agent.acp 2>/tmp/daiv-acp.log
