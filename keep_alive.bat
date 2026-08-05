@echo off
title kenlet-v1 - Keep Alive
cd /d %~dp0

echo ========================================
echo  kenlet-v1 - 保持运行
echo  防止笔记本休眠 + 持续监控
echo ========================================

echo [1/3] 设置电源...
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /hibernate off
echo     休眠已禁用

echo [2/3] 检查环境...
echo     模式: 纸交易 (BINANCE_TESTNET 可开启)

echo [3/3] 启动循环...
echo.
echo  按 Ctrl+C 停止
echo ========================================

:loop
echo [%date% %time%] 检查行情...
python -m kenlet status BTC

echo [%date% %time%] 组合概览...
python -m kenlet dashboard

echo.
echo --------------------------------
echo  休息 5 分钟...
echo --------------------------------
timeout /t 300 /nobreak
goto loop
