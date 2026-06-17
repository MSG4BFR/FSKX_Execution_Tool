@echo off
REM FSKX Runner launcher (Windows).
REM
REM   run.bat [MODELS_FOLDER] [PORT]
REM
REM By default it serves (and downloads into) the "fskx_models" folder inside this directory.
REM Override per-launch with the arguments above, or persistently in .env (MODELS_DIR / PORT).
REM Requires Docker Desktop to be installed and running.

setlocal enabledelayedexpansion
cd /d "%~dp0"

set IMAGE=fskx-runner

REM Load local config + secrets from .env first (KEY=value, one per line, no spaces around
REM '='). This is the single config file: ANTHROPIC_API_KEY (or API_KEY), FSKX_CLAUDE_MODEL,
REM MODELS_DIR and PORT.
if exist "%~dp0.env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%~dp0.env") do set "%%A=%%B"
)
if not defined ANTHROPIC_API_KEY if defined API_KEY set "ANTHROPIC_API_KEY=%API_KEY%"

REM Resolve models folder + port: CLI argument > .env > default (fskx_models in this folder).
if not "%~1"=="" set "MODELS_DIR=%~f1"
if not defined MODELS_DIR set "MODELS_DIR=%~dp0fskx_models"
if not "%~2"=="" set "PORT=%~2"
if not defined PORT set "PORT=8000"
if not exist "%MODELS_DIR%" mkdir "%MODELS_DIR%"

where docker >nul 2>nul
if errorlevel 1 (
  echo ERROR: Docker is not installed or not on PATH.
  echo Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and try again.
  pause
  exit /b 1
)

echo Models folder : %MODELS_DIR%
echo Building image ^(first run only - this can take a minute^)...
docker build -t %IMAGE% .
if errorlevel 1 ( echo Build failed. & pause & exit /b 1 )

set "URL=http://localhost:%PORT%"
echo.
echo Starting FSKX Runner at %URL%
echo (The FIRST run of each model also builds its environment, which may take a few minutes.
echo  Later runs of the same model are fast. Close this window to stop.)
echo.

start "" %URL%

REM Models folder mounted read-write (for repository downloads); Docker socket mounted
REM so the AI-assisted feature can build and run per-model images.
docker run --rm -p %PORT%:8000 -e ANTHROPIC_API_KEY -e FSKX_CLAUDE_MODEL -v "%MODELS_DIR%:/models" -v fskx_envs:/opt/conda/envs -v fskx_work:/work -v //var/run/docker.sock:/var/run/docker.sock %IMAGE%

endlocal
