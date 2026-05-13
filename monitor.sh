#!/bin/bash

# 필요한 패키지: sysstat (iostat, sar), bc, smartmontools 설치 확인 필요

# ===================================================
# 0. SSD 장치 이름 정의
# ===================================================
SSD_DEVICE="sda"

# ===================================================
# 1. CPU 온도 및 사용률 확인
# ===================================================
CPU_TEMP=$(sudo vcgencmd measure_temp | cut -f 2 -d "=" | cut -f 1 -d "'")

# CPU Usage
CPU_IDLE=$(vmstat 1 2 | tail -n 1 | awk '{print $15}')
CPU_USAGE=$((100 - CPU_IDLE))

# ===================================================
# 2. SSD 온도 확인 (smartctl을 사용하여 NVMe 온도 추출)
# ===================================================
SSD_TEMP="N/A (smartctl need)"
if command -v smartctl &> /dev/null; then
    # 'Temperature:' 라인을 찾고, 두 번째 필드 (40)만 명확하게 추출합니다.
    TEMP_RAW=$(sudo smartctl -A /dev/${SSD_DEVICE} | grep 'Temperature:' | head -n 1 | awk '{print $2}')
    if [ -n "$TEMP_RAW" ]; then
        SSD_TEMP="${TEMP_RAW}C"
    else
        SSD_TEMP="N/A (Extract Fail)"
    fi
fi

# ===================================================
# 3. 메모리 사용량 확인 및 계산
# ===================================================
MEM_TOTAL=$(free -m | awk 'NR==2{print $2}')
MEM_AVAILABLE=$(free -m | awk 'NR==2{print $7}')
MEM_REAL_USED=$((MEM_TOTAL - MEM_AVAILABLE))
MEM_PERCENT=$(echo "scale=1; (${MEM_TOTAL} - ${MEM_AVAILABLE}) * 100 / ${MEM_TOTAL}" | bc)

# ===================================================
# 4. 네트워크 트래픽 (MB/s) 및 디스크 I/O (MB/s) 확인
# ===================================================

# A. 디스크 I/O (iostat 사용) - SSD 장치(sda) 기준
IOSTAT_DATA=$(iostat -d 1 2 | grep "${SSD_DEVICE}" | tail -n 1)
DISK_READ_KB=$(echo "$IOSTAT_DATA" | awk '{print $4}')
DISK_WRITE_KB=$(echo "$IOSTAT_DATA" | awk '{print $5}')

# B. 네트워크 트래픽 (sar -n DEV 사용: eth0 인터페이스)
NET_DATA=$(sar -n DEV 1 2 | grep 'eth0' | tail -n 1)
NET_RX_KB=$(echo "$NET_DATA" | awk '{print $5}')
NET_TX_KB=$(echo "$NET_DATA" | awk '{print $6}')

# C. MB/s 계산 후 소수점 2자리 포맷 강제 적용 (bc 필요)
DISK_READ_MB=$(printf "%.2f" $(echo "scale=2; ${DISK_READ_KB} / 1024" | bc))
DISK_WRITE_MB=$(printf "%.2f" $(echo "scale=2; ${DISK_WRITE_KB} / 1024" | bc))

NET_DOWNLOAD_MB=$(printf "%.2f" $(echo "scale=2; ${NET_RX_KB} / 1024" | bc))
NET_UPLOAD_MB=$(printf "%.2f" $(echo "scale=2; ${NET_TX_KB} / 1024" | bc))

# ===================================================
# 5. 결과 출력
# ===================================================
echo "=========================================="
echo "         Raspberry Pi 2 Monitoring"
echo "=========================================="
echo "CPU Temp: ${CPU_TEMP}C | Usage: ${CPU_USAGE}%"
echo "------------------------------------------"
echo "SSD Temperature: (/dev/sda1): ${SSD_TEMP}"
echo "------------------------------------------"
echo "Memory Usage: ${MEM_REAL_USED}MB / ${MEM_TOTAL}MB (${MEM_PERCENT}%)"
echo "------------------------------------------"
echo "Network  (MB/s): Down: ${NET_DOWNLOAD_MB} | Upload: ${NET_UPLOAD_MB}"
echo "Disk I/O (MB/s): Read: ${DISK_READ_MB} | Write : ${DISK_WRITE_MB}"
echo "=========================================="