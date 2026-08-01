@echo off
REM ============================================================================
REM SIGA — Job de retention audit log (Windows Task Scheduler)
REM ============================================================================
REM A planifier dans Task Scheduler :
REM   - Frequence : Quotidien 03:00 (heure de faible charge)
REM   - Action    : Demarrer un programme = ce fichier .bat
REM   - User      : compte ayant acces a la BD (par defaut, le compte Django prod)
REM ============================================================================

REM Repertoire racine du projet
set PROJECT_DIR=C:\react_projects\GES\siga
cd /D %PROJECT_DIR%

REM Active venv si present
if exist "%PROJECT_DIR%\venv\Scripts\activate.bat" call "%PROJECT_DIR%\venv\Scripts\activate.bat"

REM Variables d'environnement (override si .env n'est pas charge)
set DJANGO_SETTINGS_MODULE=siga.settings.production

REM Logs (rotation manuelle)
set LOG_DIR=%PROJECT_DIR%\logs\audit_retention
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set TS=%date:~6,4%%date:~3,2%%date:~0,2%

echo === [%date% %time%] DEBUT retention audit ===  >> "%LOG_DIR%\retention_%TS%.log"

REM 1. Archive : HOT > 90 jours -> AuditLogArchive
python manage.py archive_audit_logs >> "%LOG_DIR%\retention_%TS%.log" 2>&1

REM 2. Purge : ARCHIVE > 365 jours -> JSONL.gz + DELETE
python manage.py purge_audit_logs   >> "%LOG_DIR%\retention_%TS%.log" 2>&1

echo === [%date% %time%] FIN retention audit ===   >> "%LOG_DIR%\retention_%TS%.log"
echo.                                              >> "%LOG_DIR%\retention_%TS%.log"
