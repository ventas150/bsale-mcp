@echo off
rem Tests de integracion contra el Postgres portable del PC (test_integracion.py).
rem Postgres 18 sin instalador: C:\Users\rolguin\pg18\pgsql (binarios EDB),
rem datos en C:\Users\rolguin\pgdata, puerto 5433, usuario postgres sin clave
rem (auth trust, solo localhost). Base: bsale_test. Los tests BORRAN el esquema.
rem   scripts\pg-test.cmd        -> solo test_integracion.py
rem   scripts\pg-test.cmd todo   -> la suite completa (unitaria + integracion)
setlocal
set PGBIN=C:\Users\rolguin\pg18\pgsql\bin
set PGDATA=C:\Users\rolguin\pgdata
set DATABASE_URL_TEST=postgresql://postgres@localhost:5433/bsale_test

"%PGBIN%\pg_ctl.exe" -D "%PGDATA%" status >nul 2>&1
if errorlevel 1 (
  echo Arrancando Postgres local en el puerto 5433...
  "%PGBIN%\pg_ctl.exe" -D "%PGDATA%" -o "-p 5433" -l "%PGDATA%.log" -w start || exit /b 1
)

cd /d %~dp0..
if "%~1"=="todo" (
  .venv\Scripts\python.exe -m pytest -q
) else (
  .venv\Scripts\python.exe -m pytest -q test_integracion.py %*
)
endlocal
