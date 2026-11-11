@echo off
rem ======================================================================
rem  iniciar.bat - Painel de controle do Dual Load Balancer Lab
rem
rem  Duplo clique: verifica o WSL, abre o painel interativo (Python, dentro
rem  do WSL), que inicializa os componentes desligados e mostra o menu.
rem  Funciona de qualquer diretorio: usa a pasta onde este arquivo esta.
rem
rem  Opcional: para usar outra distribuicao WSL, defina A2_WSL_DISTRO
rem  antes de executar (padrao: Ubuntu-24.04). Argumentos extras sao
rem  repassados ao painel (ex.: iniciar.bat --sem-inicializar).
rem ======================================================================
setlocal EnableExtensions
chcp 65001 >nul
title Painel de Controle - Dual Load Balancer Lab

rem Pasta do projeto = pasta deste .bat (sem a barra final, que confunde o wsl.exe)
set "PROJETO=%~dp0"
if "%PROJETO:~-1%"=="\" set "PROJETO=%PROJETO:~0,-1%"
if not defined A2_WSL_DISTRO set "A2_WSL_DISTRO=Ubuntu-24.04"

echo.
echo  ==============================================================
echo    DUAL LOAD BALANCER LAB - abrindo o painel de controle
echo  ==============================================================
echo.
echo  [1/4] Pasta do projeto: %PROJETO%
if not exist "%PROJETO%\painel\painel.py" (
    echo.
    echo  [ERRO] Arquivo painel\painel.py nao encontrado nesta pasta.
    echo         O iniciar.bat precisa ficar na raiz do projeto.
    goto :falha
)

echo  [2/4] Verificando o WSL...
where wsl.exe >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  [ERRO] wsl.exe nao encontrado. Instale o WSL com:  wsl --install
    goto :falha
)

rem Observacoes: o wsl.exe retorna -1 em alguns erros (por isso NEQ 0, e nao
rem "if errorlevel 1"), e as verificacoes recebem <nul para nao consumirem
rem o teclado destinado ao painel.
echo  [3/4] Iniciando a distribuicao %A2_WSL_DISTRO% ^(pode levar alguns segundos^)...
wsl.exe -d %A2_WSL_DISTRO% --exec true <nul >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  [ERRO] Nao foi possivel iniciar a distribuicao "%A2_WSL_DISTRO%".
    echo         Distribuicoes instaladas:
    wsl.exe -l -v
    echo         Ajuste a variavel A2_WSL_DISTRO ou instale a distribuicao.
    goto :falha
)
wsl.exe -d %A2_WSL_DISTRO% --exec python3 --version <nul >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  [ERRO] python3 nao encontrado na distribuicao %A2_WSL_DISTRO%.
    echo         No Ubuntu:  sudo apt install python3
    goto :falha
)

echo  [4/4] Abrindo o painel...
wsl.exe -d %A2_WSL_DISTRO% --cd "%PROJETO%" --exec python3 painel/painel.py %*
set "CODIGO=%ERRORLEVEL%"
if not "%CODIGO%"=="0" (
    echo.
    echo  [ERRO] O painel terminou com codigo %CODIGO%.
    goto :falha
)
endlocal
exit /b 0

:falha
echo.
echo  A janela continua aberta para que voce possa ler a mensagem acima.
pause
endlocal
exit /b 1
