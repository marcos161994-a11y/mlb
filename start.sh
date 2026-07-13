#!/bin/bash
uvicorn servidor_mlb:app --host 0.0.0.0 --port $PORT
