@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo ========================================
echo   INSTALADOR AUTOMATICO DEL PROYECTO
echo ========================================
echo.

where py >nul 2>nul
if errorlevel 1 (
    echo [ERROR] No se encontro el lanzador de Python ^(py^).
    echo Instala Python 3.11 desde python.org y vuelve a intentar.
    pause
    exit /b 1
)

py -3.11 -V >nul 2>nul
if errorlevel 1 (
    echo [ERROR] No se encontro Python 3.11 instalado.
    echo Este proyecto necesita Python 3.11 para evitar errores con TensorFlow y NumPy.
    pause
    exit /b 1
)

echo [1/6] Creando entorno virtual...
py -3.11 -m venv .venv
if errorlevel 1 (
    echo [ERROR] No se pudo crear el entorno virtual.
    pause
    exit /b 1
)

echo [2/6] Activando entorno virtual...
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] No se pudo activar el entorno virtual.
    pause
    exit /b 1
)

echo [3/6] Actualizando pip, setuptools y wheel...
python -m pip install --upgrade pip setuptools wheel
if errorlevel 1 (
    echo [ERROR] Fallo al actualizar pip/setuptools/wheel.
    pause
    exit /b 1
)

echo [4/6] Instalando NumPy primero con binarios precompilados...
pip install --only-binary=:all: numpy==1.26.4
if errorlevel 1 (
    echo [ERROR] Fallo al instalar numpy.
    echo Revisa que tu Python 3.11 sea de 64 bits.
    pause
    exit /b 1
)

echo [5/6] Instalando dependencias del proyecto...
if exist requirements.txt (
    pip install --only-binary=:all: -r requirements.txt
    if errorlevel 1 (
        echo [ADVERTENCIA] Fallo con modo binario estricto. Intentando instalacion normal...
        pip install -r requirements.txt
        if errorlevel 1 (
            echo [ERROR] No se pudieron instalar las dependencias.
            pause
            exit /b 1
        )
    )
) else (
    echo [ERROR] No se encontro requirements.txt en esta carpeta.
    pause
    exit /b 1
)

echo [6/6] Verificando librerias principales...
python -c "import fastapi, uvicorn, cv2, numpy, pandas, PIL, requests; import tensorflow as tf; from deepface import DeepFace; print('OK: entorno listo')"
if errorlevel 1 (
    echo [ERROR] La verificacion final fallo.
    pause
    exit /b 1
)

echo.
echo ========================================
echo   INSTALACION COMPLETADA CORRECTAMENTE
echo ========================================
echo.
echo Para activar el entorno luego usa:
echo   .venv\Scripts\activate
echo.
echo Para ejecutar tu proyecto:
echo   uvicorn main:app --host 0.0.0.0 --port 8000 --reload
echo.
pause