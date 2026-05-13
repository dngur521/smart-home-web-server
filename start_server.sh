#!/bin/bash

# --- 설정 ---
# 1. 로그를 저장할 폴더 경로 (스크립트가 있는 현재 디렉토리의 하위 'log' 폴더)
LOG_DIR="./log" 
# 2. 원하는 로그 파일 이름 (예: 실행 시각 포함)
LOG_FILE_NAME="app_$(date +\%Y\%m\%d_\%H\%M\%S).log"
# 3. 로그 파일의 최종 경로
FULL_LOG_PATH="${LOG_DIR}/${LOG_FILE_NAME}"

# 4. 로그 폴더가 존재하지 않으면 생성 (핵심)
if [ ! -d "$LOG_DIR" ]; then
    mkdir -p "$LOG_DIR"
    echo "Created log directory: $LOG_DIR"
fi

# --- 앱 실행 ---
# nohup을 사용하여 백그라운드에서 실행 (전체 로그 경로 지정)
nohup python3 app.py > "$FULL_LOG_PATH" 2>&1 &

echo "App started in background. PID: $!"
echo "Log file saved to: $FULL_LOG_PATH"