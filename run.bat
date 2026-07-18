@echo off
cd /d H:\归爻
echo ========== GuiYao V5 ==========
echo 1 - Verify engine
echo 2 - Git status
echo 3 - Activate venv
echo 4 - Run tests
echo ================================
set /p c=Choice:
if %%c%%==1 .venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'.'); from engine.numpy_metrics import calc_dsr; from engine.regime import RegimeDetector; from engine.execution_engine import ExecutionEngine; print('Engine OK')"
if %%c%%==2 git status
if %%c%%==3 cmd /k .venv\Scripts\activate.bat
if %%c%%==4 .venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'.'); exec(open('scripts/test_regime.py').read())" 2>nul || echo No test script
pause