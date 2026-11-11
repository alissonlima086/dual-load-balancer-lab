#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Painel de controle interativo do Dual Load Balancer Lab (Projeto A2)
====================================================================

Aberto pelo iniciar.bat (Windows), roda DENTRO do WSL e administra os
componentes reais do laboratório:

    navegador / curl
          |
    a2-front  (Caddy, container Docker, :8080)                entrada única
       |-- lb1 (Caddy, container Docker, 127.0.0.1:31081)     load balancers
       '-- lb2 (Caddy, container Docker, 127.0.0.1:31082)
              '-- app1..app4 (Python, serviços systemd, 10.28.0.1:31001..31004)

Regra central: toda operação é um objeto `Comando`. A MESMA string é
exibida na tela e executada com `bash -c "<string>"` no diretório do
laboratório; não existe uma versão do comando para exibição e outra para
execução. Todas as strings de comando são montadas na seção
"CATÁLOGO DE COMANDOS" deste arquivo.

Somente biblioteca padrão do Python 3 (>= 3.8).

Opções:
    --sem-inicializar   abre o menu sem a inicialização automática
    --lab-dir CAMINHO   clone de execução (padrão: ~/dual-load-balancer-lab)
"""

import argparse
import datetime
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:  # só existe em Linux; o painel roda dentro do WSL
    fcntl = None


# ======================================================================
# PARÂMETROS
# ======================================================================

NOME_CLONE = "dual-load-balancer-lab"
IMAGEM_CADDY = "caddy:2.8.4-alpine"   # inventory/group_vars/all.yml -> imagens.caddy
JANELA_PARTIDA = 10        # s: processo recém-iniciado ainda sem resposta HTTP = INICIALIZANDO
TIMEOUT_SUBIDA = 30        # s: espera máxima até um componente ficar ONLINE
TIMEOUT_DESCIDA = 20       # s: espera máxima até um componente ficar OFFLINE
TIMEOUT_REINTEGRACAO = 25  # s: espera máxima pela volta ao rodízio do balanceador
ARQ_LOG = os.path.expanduser("~/.local/state/a2-painel/comandos.log")
ARQ_LOG_EXIBICAO = "~/.local/state/a2-painel/comandos.log"

# Servidores e balanceadores (mesmos dados de inventory/group_vars/all.yml)
APPS = [
    # chave,  cor,      hex,       variável no .env, porta padrão
    ("app1", "Azul",    "#2563EB", "APP1_PORT", "31001"),
    ("app2", "Verde",   "#059669", "APP2_PORT", "31002"),
    ("app3", "Laranja", "#EA580C", "APP3_PORT", "31003"),
    ("app4", "Roxo",    "#7C3AED", "APP4_PORT", "31004"),
]
LBS = [("lb1", "LB1_PORT", "31081"), ("lb2", "LB2_PORT", "31082")]

ESTADOS_RODANDO_OK = ("ONLINE",)
ESTADOS_PARADO = ("OFFLINE", "AUSENTE")


# ======================================================================
# CONTEXTO GLOBAL
# ======================================================================

class Contexto:
    lab_dir = ""          # clone de execução (onde o make roda)
    lab_curto = ""        # mesmo caminho com ~
    repo_dir = ""         # pasta onde está o iniciar.bat / painel
    env = {}              # conteúdo do .env do clone
    comps = []            # componentes do laboratório
    rede = "a2_net"
    dir_dados = "/opt/a2"
    front_caddyfile = os.path.expanduser("~/a2-front/Caddyfile")
    ambiente = ""
    distro = ""
    sudo_sem_senha = True
    rede_ok = True
    mensagem = None       # aviso mostrado no topo do próximo menu
    trava = None


CTX = Contexto()


# ======================================================================
# TERMINAL: CORES, LAYOUT E ENTRADA
# ======================================================================

USAR_CORES = False


class C:
    RESET = NEGRITO = VERMELHO = VERDE = AMARELO = AZUL = MAGENTA = ""
    CIANO = BRANCO = CINZA = ERRO_BG = ""


def configurar_terminal():
    global USAR_CORES
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError):
            pass
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    USAR_CORES = (sys.stdout.isatty() and not os.environ.get("NO_COLOR")
                  and os.environ.get("TERM") != "dumb")
    if USAR_CORES:
        C.RESET, C.NEGRITO = "\033[0m", "\033[1m"
        C.VERMELHO, C.VERDE, C.AMARELO = "\033[91m", "\033[92m", "\033[93m"
        C.AZUL, C.MAGENTA, C.CIANO = "\033[94m", "\033[95m", "\033[96m"
        C.BRANCO, C.CINZA = "\033[97m", "\033[90m"
        C.ERRO_BG = "\033[97;41m"


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def vis(s):
    """Comprimento visível (sem códigos de cor)."""
    return len(_ANSI.sub("", s))


def ajustar(s, n):
    return s + " " * max(0, n - vis(s))


def largura():
    colunas = shutil.get_terminal_size((80, 24)).columns
    return max(64, min(80, colunas - 1))


def cor_rgb(hexa):
    if not USAR_CORES or not hexa:
        return ""
    h = hexa.lstrip("#")
    return "\033[38;2;%d;%d;%dm" % (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def limpar():
    if USAR_CORES:
        sys.stdout.write("\033[H\033[2J\033[3J")
    else:
        sys.stdout.write("\n")
    sys.stdout.flush()


def cabecalho(titulo, sub=None):
    limpar()
    w = largura()
    print(C.AZUL + "=" * w + C.RESET)
    print(C.NEGRITO + C.BRANCO + titulo.center(w).rstrip() + C.RESET)
    if sub:
        print(C.CINZA + sub.center(w).rstrip() + C.RESET)
    print(C.AZUL + "=" * w + C.RESET)


def traco():
    print(C.CINZA + "-" * largura() + C.RESET)


def secao(titulo):
    print()
    print(" " + C.NEGRITO + titulo + C.RESET)
    print(" " + C.CINZA + "-" * (largura() - 1) + C.RESET)


def texto(msg, recuo=1, cor=""):
    w = largura()
    for par in msg.split("\n"):
        linhas = textwrap.wrap(par, max(20, w - recuo - 1)) or [""]
        for l in linhas:
            print(" " * recuo + cor + l + (C.RESET if cor else ""))


def info(msg):
    print(" " + C.AZUL + C.NEGRITO + "[INFO]" + C.RESET + " " + msg)


def sucesso(msg):
    print(" " + C.VERDE + C.NEGRITO + "[SUCESSO]" + C.RESET + " " + msg)


def aviso(msg):
    print(" " + C.AMARELO + C.NEGRITO + "[AVISO]" + C.RESET + " " + msg)


def erro(msg):
    print(" " + C.VERMELHO + C.NEGRITO + "[ERRO]" + C.RESET + " " + msg)


ESTILO_ESTADO = {
    "ONLINE": "VERDE", "OFFLINE": "VERMELHO", "INICIALIZANDO": "AMARELO",
    "ENCERRANDO": "AMARELO", "ERRO": "ERRO_BG", "AUSENTE": "CINZA",
}


def selo(estado):
    cor = getattr(C, ESTILO_ESTADO.get(estado, "BRANCO"))
    return cor + C.NEGRITO + "[" + estado + "]" + C.RESET


def dec(x):
    return ("%.1f" % x).replace(".", ",")


class FimDaEntrada(Exception):
    """A entrada padrão foi fechada (janela fechada ou fim do arquivo)."""


def ler(prompt):
    sys.stdout.flush()
    try:
        return input(" " + C.NEGRITO + prompt + C.RESET + " ").strip()
    except EOFError:
        print()
        raise FimDaEntrada()


def pausar(msg="Pressione ENTER para retornar ao menu principal."):
    print()
    ler(C.CINZA + msg)


def escolher(validas, prompt="Selecione uma opção:"):
    while True:
        r = ler(prompt)
        if r in validas:
            return r
        if r:
            erro("Opção inválida: '%s'. Digite um dos números listados." % r)
        else:
            erro("Nenhuma opção digitada. Digite um dos números listados.")


def confirmar(pergunta):
    print()
    print(" " + C.NEGRITO + pergunta + C.RESET)
    print()
    print(" [1] Sim, executar")
    print(" [2] Não, cancelar")
    print()
    try:
        r = escolher({"1", "2"})
    except KeyboardInterrupt:
        print()
        r = "2"
    if r == "1":
        return True
    print()
    aviso("Operação cancelada. Nenhum comando foi executado e nada foi alterado.")
    return False


class Progresso:
    """Linha de progresso que se sobrescreve no terminal."""

    def __init__(self):
        self.ultimo = None

    def mostrar(self, msg):
        if USAR_CORES:
            limite = shutil.get_terminal_size((80, 24)).columns - 1
            if len(msg) > limite:      # linha quebrada não seria sobrescrita
                msg = msg[:limite - 3] + "..."
            sys.stdout.write("\r\033[K" + msg)
            sys.stdout.flush()
        elif msg != self.ultimo:
            print(msg)
        self.ultimo = msg

    def fim(self):
        if USAR_CORES and self.ultimo is not None:
            sys.stdout.write("\n")
            sys.stdout.flush()


class LinhaFluxo:
    """Imprime itens curtos em sequência, quebrando a linha quando enche."""

    def __init__(self, prefixo):
        self.prefixo = prefixo
        self.col = None

    def add(self, item):
        w = largura()
        if self.col is None:
            sys.stdout.write(self.prefixo)
            self.col = vis(self.prefixo)
        elif self.col + 3 + vis(item) > w:
            sys.stdout.write("\n" + " " * vis(self.prefixo))
            self.col = vis(self.prefixo)
        else:
            sys.stdout.write(C.CINZA + " · " + C.RESET)
            self.col += 3
        sys.stdout.write(item)
        self.col += vis(item)
        sys.stdout.flush()

    def fim(self):
        if self.col is not None:
            sys.stdout.write("\n")
            sys.stdout.flush()


# ======================================================================
# PROTEÇÃO CONTRA INTERRUPÇÕES
# ======================================================================

class OperacaoCritica:
    """Durante operações que alteram o estado, Ctrl+C é ignorado e o
    fechamento da janela (SIGHUP) só encerra o painel depois que o
    procedimento termina. Os comandos de ação também rodam em sessão
    própria, então não recebem o Ctrl+C nem o SIGHUP do terminal."""

    nivel = 0
    sinal_pendente = None
    _anterior = None

    def __enter__(self):
        if OperacaoCritica.nivel == 0:
            OperacaoCritica._anterior = signal.signal(signal.SIGINT, OperacaoCritica._ctrl_c)
        OperacaoCritica.nivel += 1
        return self

    def __exit__(self, *exc):
        OperacaoCritica.nivel -= 1
        if OperacaoCritica.nivel == 0:
            signal.signal(signal.SIGINT, OperacaoCritica._anterior)
            if OperacaoCritica.sinal_pendente:
                raise SystemExit(128 + OperacaoCritica.sinal_pendente)
        return False

    @staticmethod
    def _ctrl_c(signum, frame):
        try:
            print("\n " + C.AMARELO + "[AVISO]" + C.RESET +
                  " Operação em andamento: Ctrl+C ignorado para não interromper o procedimento pela metade.")
        except OSError:
            pass


def _desviar_saida_para_null():
    fd = os.open(os.devnull, os.O_RDWR)
    for alvo in (0, 1, 2):
        try:
            os.dup2(fd, alvo)
        except OSError:
            pass


def _sinal_terminacao(signum, frame):
    if OperacaoCritica.nivel > 0:
        # Janela fechada no meio de uma operação: termina o procedimento
        # sem terminal (saída descartada) e só então encerra o painel.
        OperacaoCritica.sinal_pendente = signum
        if signum == signal.SIGHUP:
            _desviar_saida_para_null()
        return
    raise SystemExit(128 + signum)


# ======================================================================
# COMANDO: FONTE ÚNICA PARA EXIBIR E EXECUTAR
# ======================================================================

class Saida:
    __slots__ = ("rc", "stdout", "stderr", "duracao")

    def __init__(self, rc, stdout="", stderr="", duracao=0.0):
        self.rc, self.stdout, self.stderr, self.duracao = rc, stdout, stderr, duracao

    @property
    def ok(self):
        return self.rc == 0


def _txt(b):
    if not b:
        return ""
    return b.decode("utf-8", "replace") if isinstance(b, bytes) else b


def registrar(tipo, linha, rc, duracao):
    """Histórico de tudo o que o painel executou de forma visível."""
    try:
        os.makedirs(os.path.dirname(ARQ_LOG), exist_ok=True)
        agora = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(ARQ_LOG, "a", encoding="utf-8") as f:
            f.write("%s  %-8s rc=%-3s %6ss  %s\n" % (agora, tipo, rc, dec(duracao), linha))
    except OSError:
        pass


class Comando:
    """Um comando de terminal. `linha` é exatamente o que aparece na tela
    e exatamente o que é executado (bash -c "<linha>", no diretório do
    laboratório)."""

    def __init__(self, linha, descricao="", consulta=False, longo=False, interativo=None):
        self.linha = linha
        self.descricao = descricao
        self.consulta = consulta    # não altera estado: executa sem confirmação
        self.longo = longo          # saída transmitida ao vivo (ex.: make up)
        if interativo is None:
            interativo = linha.startswith("sudo ") and not CTX.sudo_sem_senha
        self.interativo = interativo  # precisa do teclado (ex.: senha do sudo)
        self.alvos = []             # componentes afetados (para verificação)
        self.efeito = None          # "iniciar" | "parar" | None

    def exibir(self, recuo=3, numero=None, com_descricao=False):
        pre = " " * recuo + ("%d. " % numero if numero is not None else "")
        print(pre + C.CINZA + "$ " + C.RESET + C.NEGRITO + C.CIANO + self.linha + C.RESET)
        if com_descricao and self.descricao:
            texto("# " + self.descricao, recuo=len(pre) + 2, cor=C.CINZA)

    def _argv(self):
        return ["bash", "-c", self.linha]

    def capturar(self, timeout=15):
        """Execução silenciosa (usada só pelo quadro de status do menu,
        que informa quais consultas usa)."""
        t0 = time.time()
        try:
            p = subprocess.run(self._argv(), cwd=CTX.lab_dir, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            return Saida(p.returncode, _txt(p.stdout), _txt(p.stderr), time.time() - t0)
        except subprocess.TimeoutExpired as e:
            return Saida(124, _txt(e.stdout), "tempo esgotado após %d s" % timeout, time.time() - t0)

    def executar(self, timeout=None, mostrar_saida=True):
        """Executa mostrando o resultado e registra no histórico."""
        if self.consulta:
            return self._executar(timeout, mostrar_saida)
        with OperacaoCritica():
            return self._executar(timeout, mostrar_saida)

    def _executar(self, timeout, mostrar_saida):
        sys.stdout.flush()
        t0 = time.time()
        # Ações rodam em sessão própria: Ctrl+C e o fechamento da janela
        # não as interrompem no meio. Consultas ficam na sessão do
        # terminal (Ctrl+C as cancela normalmente).
        nova_sessao = not self.consulta and not self.interativo
        if self.longo or self.interativo:
            p = subprocess.run(self._argv(), cwd=CTX.lab_dir,
                               stdin=None if self.interativo else subprocess.DEVNULL,
                               start_new_session=nova_sessao)
            s = Saida(p.returncode, duracao=time.time() - t0)
        else:
            try:
                p = subprocess.run(self._argv(), cwd=CTX.lab_dir, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=nova_sessao, timeout=timeout)
                s = Saida(p.returncode, _txt(p.stdout), _txt(p.stderr), time.time() - t0)
            except subprocess.TimeoutExpired as e:
                s = Saida(124, _txt(e.stdout), "tempo esgotado após %d s" % timeout, time.time() - t0)
            if mostrar_saida:
                imprimir_saida(s)
        registrar("CONSULTA" if self.consulta else "ACAO", self.linha, s.rc, s.duracao)
        return s


def imprimir_saida(s, recuo=5):
    out = s.stdout.rstrip("\n").splitlines() if s.stdout.strip() else []
    err = s.stderr.rstrip("\n").splitlines() if s.stderr.strip() else []
    if not out and not err:
        print(" " * recuo + C.CINZA + "(sem saída)" + C.RESET)
        return
    for l in out:
        print(" " * recuo + C.CINZA + "│ " + C.RESET + l)
    for l in err:
        print(" " * recuo + C.VERMELHO + "│ " + l + C.RESET)


MOTIVOS_CURL = {6: "nome não resolvido", 7: "conexão recusada", 28: "tempo esgotado",
                52: "resposta vazia", 56: "conexão interrompida"}


def motivo_curl(s):
    if s.rc in MOTIVOS_CURL:
        return MOTIVOS_CURL[s.rc]
    if s.rc == 0:
        return "resposta inesperada: %s" % (s.stdout.strip()[:30] or "vazia")
    return "curl terminou com código %d" % s.rc


def rodape(s, cmd):
    if s.ok:
        print("     " + C.CINZA + "código de saída 0 · %s s" % dec(s.duracao) + C.RESET)
        return
    extra = ""
    if cmd.linha.startswith("curl") and s.rc in MOTIVOS_CURL:
        extra = " (%s)" % MOTIVOS_CURL[s.rc]
    if cmd.consulta:
        print("     " + C.AMARELO + "código de saída %d%s · %s s" % (s.rc, extra, dec(s.duracao)) + C.RESET)
    else:
        erro("O comando terminou com código de saída %d%s (%s s)." % (s.rc, extra, dec(s.duracao)))


# ======================================================================
# COMPONENTES
# ======================================================================

class Componente:
    def __init__(self, numero, chave, tipo, unidade, host, porta, url_saude, cor_nome="", cor_hex=""):
        self.numero, self.chave, self.tipo, self.unidade = numero, chave, tipo, unidade
        self.host, self.porta, self.url_saude = host, porta, url_saude
        self.cor_nome, self.cor_hex = cor_nome, cor_hex

    @property
    def endereco(self):
        return "%s:%s" % (self.host, self.porta)

    @property
    def rotulo(self):
        if self.tipo == "app":
            return "Servidor %s" % self.chave
        if self.tipo == "lb":
            return "Load Balancer %s" % self.chave
        return "Caddy frontal (a2-front)"

    @property
    def gerenciador(self):
        return ("systemd " if self.tipo == "app" else "docker ") + self.unidade

    def nome_fmt(self):
        if self.tipo == "app":
            return cor_rgb(self.cor_hex) + "■" + C.RESET + " Servidor %s (%s)" % (self.chave, self.cor_nome)
        if self.tipo == "lb":
            return C.CIANO + "■" + C.RESET + " Load Balancer %s" % self.chave
        return C.BRANCO + "■" + C.RESET + " Caddy frontal (entrada)"


def comps_tipo(tipo):
    return [c for c in CTX.comps if c.tipo == tipo]


def comp_por_chave(chave):
    return next((c for c in CTX.comps if c.chave == chave), None)


def montar_componentes(env):
    gw = env.get("A2_DOCKER_GATEWAY") or "172.28.0.1"
    health = env.get("A2_HEALTH_URI") or "/health"
    lb_ip = env.get("LB_BIND_IP") or "127.0.0.1"
    if lb_ip == "0.0.0.0":
        lb_ip = "127.0.0.1"
    comps = []
    for i, (chave, cor, hexa, var, padrao) in enumerate(APPS, 1):
        porta = env.get(var) or padrao
        comps.append(Componente(i, chave, "app", "a2-" + chave, gw, porta,
                                "http://%s:%s%s" % (gw, porta, health), cor, hexa))
    for chave, var, padrao in LBS:
        porta = env.get(var) or padrao
        comps.append(Componente(len(comps) + 1, chave, "lb", chave, lb_ip, porta,
                                "http://%s:%s%s" % (lb_ip, porta, health)))
    front = (env.get("A2_FRONT_URL") or "").rstrip("/")
    if front:
        u = urlsplit(front)
        porta = str(u.port or (443 if u.scheme == "https" else 80))
        comps.append(Componente(len(comps) + 1, "a2-front", "front", "a2-front",
                                u.hostname or "localhost", porta, front + "/"))
    return comps


# ======================================================================
# CATÁLOGO DE COMANDOS (todas as strings executadas pelo painel)
# ======================================================================

FMT_INSPECT = "'{{.State.Status}} {{.State.StartedAt}} {{.State.ExitCode}}'"
FMT_REQ = "'%{http_code}  %header{x-load-balancer} -> %header{x-servidor}\\n'"


def _plural(grupo, um, varios):
    return um if len(grupo) == 1 else varios


class Cmd:

    # ---------- consultas de status ----------
    @staticmethod
    def status(c):
        """[processo, funcionamento HTTP real] de um componente."""
        if c.tipo == "app":
            return [
                Comando("systemctl show " + c.unidade +
                        " --property=LoadState,ActiveState,SubState,MainPID,ActiveEnterTimestampMonotonic",
                        "Processo: o serviço está instalado e ativo no systemd? Qual o PID?", consulta=True),
                Comando("curl -s --max-time 2 " + c.url_saude,
                        "Funcionamento real: requisição HTTP direta ao servidor, sem passar "
                        "pelo LB (esperado: 'ok %s')." % c.chave, consulta=True),
            ]
        inspect = Comando("docker inspect --format " + FMT_INSPECT + " " + c.unidade,
                          "Processo: estado do container no Docker (status, horário de início, código de saída).",
                          consulta=True)
        if c.tipo == "lb":
            http = Comando("curl -s -o /dev/null --max-time 2 -w '%{http_code}' " + c.url_saude,
                           "Funcionamento real: o Caddy responde em /health? (esperado: 200)", consulta=True)
        else:
            http = Comando("curl -s -o /dev/null --max-time 5 -w "
                           "'%{http_code} %header{x-load-balancer} %header{x-servidor}' " + c.url_saude,
                           "Funcionamento real: requisição completa pela entrada única "
                           "(código HTTP, LB e servidor que atenderam).", consulta=True)
        return [inspect, http]

    @staticmethod
    def rede():
        return Comando("docker network inspect --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}}{{end}}' "
                       + CTX.rede, "A rede Docker do laboratório existe? (nome e sub-rede)", consulta=True)

    @staticmethod
    def portas(cabecalho=True):
        portas = []
        for c in CTX.comps:
            if c.porta not in portas:
                portas.append(c.porta)
        filtro = " or ".join("sport = :" + p for p in portas)
        return Comando(("ss -ltn " if cabecalho else "ss -ltnH ") + "'( " + filtro + " )'",
                       "Portas TCP do laboratório em escuta (LISTEN).", consulta=True)

    # ---------- ações ----------
    @staticmethod
    def parar(comps):
        """Encerramento na ordem inversa da dependência: entrada -> LBs -> servidores."""
        cmds = []
        for tipo in ("front", "lb", "app"):
            grupo = [c for c in comps if c.tipo == tipo]
            if not grupo:
                continue
            nomes = " ".join(c.unidade for c in grupo)
            if tipo == "app":
                cmd = Comando("sudo systemctl stop " + nomes,
                              _plural(grupo, "Para o serviço systemd do servidor Python.",
                                      "Para os serviços systemd dos servidores Python."))
            elif tipo == "lb":
                cmd = Comando("docker stop " + nomes,
                              _plural(grupo, "Para o container Caddy do load balancer.",
                                      "Para os containers Caddy dos load balancers."))
            else:
                cmd = Comando("docker stop " + nomes, "Para o container do Caddy frontal (entrada única).")
            cmd.alvos, cmd.efeito = grupo, "parar"
            cmds.append(cmd)
        return cmds

    @staticmethod
    def iniciar_simples(comps):
        """Inicialização na ordem da dependência: servidores -> LBs -> entrada."""
        cmds = []
        for tipo in ("app", "lb", "front"):
            grupo = [c for c in comps if c.tipo == tipo]
            if not grupo:
                continue
            nomes = " ".join(c.unidade for c in grupo)
            if tipo == "app":
                cmd = Comando("sudo systemctl start " + nomes,
                              "Inicia " + _plural(grupo, "o serviço systemd", "os serviços systemd") +
                              " (python3 %s/bin/app_server.py com nome, porta e cor de cada servidor)."
                              % CTX.dir_dados)
            elif tipo == "lb":
                cmd = Comando("docker start " + nomes,
                              "Inicia " + _plural(grupo, "o container Caddy existente",
                                                  "os containers Caddy existentes") +
                              " (configuração em %s/caddy/<lb>/Caddyfile)." % CTX.dir_dados)
            else:
                cmd = Comando("docker start " + nomes,
                              "Inicia o container do Caddy frontal (configuração em ~/a2-front/Caddyfile).")
            cmd.alvos, cmd.efeito = grupo, "iniciar"
            cmds.append(cmd)
        return cmds

    @staticmethod
    def iniciar(comps, est):
        """Plano completo de inicialização. Retorna (comandos, problemas)."""
        cmds, problemas = [], []
        nucleo = [c for c in comps if c.tipo in ("app", "lb")]
        precisa_make = bool(nucleo) and (not CTX.rede_ok or
                                         any(est[c.chave].estado == "AUSENTE" for c in nucleo))
        if precisa_make:
            cmd = Cmd.make("up", "Reimplanta a infraestrutura com o Ansible (playbooks/site.yml): rede Docker, "
                                 "serviços systemd e containers Caddy. É idempotente: cria e inicia apenas o "
                                 "que estiver faltando ou parado.")
            cmd.alvos = [c for c in CTX.comps if c.tipo in ("app", "lb")]
            cmd.efeito = "iniciar"
            cmds.append(cmd)
        else:
            falhos = [c for c in nucleo if c.tipo == "app" and est[c.chave].bruto == "failed"]
            if falhos:
                cmds.append(Comando("sudo systemctl reset-failed " + " ".join(c.unidade for c in falhos),
                                    "Limpa o estado de falha do systemd (e o limite de reinícios) para "
                                    "permitir um novo start."))
            cmds += Cmd.iniciar_simples(nucleo)
        for f in [c for c in comps if c.tipo == "front"]:
            if est[f.chave].estado != "AUSENTE":
                cmds += Cmd.iniciar_simples([f])
            elif os.path.isfile(CTX.front_caddyfile):
                cmd = Comando('docker run -d --name a2-front --network host --restart unless-stopped '
                              '-v "$HOME/a2-front:/etc/caddy:ro" ' + IMAGEM_CADDY,
                              "Cria e inicia o container do Caddy frontal com ~/a2-front/Caddyfile "
                              "(mesmo comando da seção 9, passo 6, do guia).")
                cmd.alvos, cmd.efeito = [f], "iniciar"
                cmds.append(cmd)
            else:
                problemas.append("O container a2-front não existe e o arquivo ~/a2-front/Caddyfile também "
                                 "não foi encontrado. Crie-o conforme a seção 9, passo 6, do "
                                 "GUIA_EXECUCAO_E_TESTES.txt.")
        return cmds, problemas

    @staticmethod
    def make(alvo, descricao, consulta=False):
        # O Makefile pede senha (-k / -K) conforme o .env; nesse caso o
        # comando precisa do teclado.
        interativo = (CTX.env.get("A2_SEM_SENHA", "1") != "1" or
                      CTX.env.get("A2_SENHA_SSH", "1") != "0")
        return Comando("make " + alvo, descricao, consulta=consulta, longo=True, interativo=interativo)

    # ---------- requisições e logs ----------
    @staticmethod
    def requisicoes(url, n, descricao):
        return Comando("for i in $(seq 1 %d); do curl -s -o /dev/null --max-time 10 -w %s %s; done"
                       % (n, FMT_REQ, url), descricao, consulta=True)

    @staticmethod
    def contagem(url, n):
        return Comando("for i in $(seq 1 %d); do curl -s -o /dev/null --max-time 10 "
                       "-w '%%{http_code} %%header{x-servidor}\\n' %s; done | sort | uniq -c" % (n, url),
                       "Envia %d requisições e conta quantas cada servidor atendeu (código HTTP + servidor)." % n,
                       consulta=True)

    @staticmethod
    def trafego(url):
        return Comando("while true; do printf '%s  ' \"$(date +%T)\"; curl -s -o /dev/null --max-time 10 -w "
                       + FMT_REQ + " " + url + "; sleep 0.5; done",
                       "Tráfego contínuo (2 requisições/s), igual à função 'trafego' do guia.", consulta=True)

    @staticmethod
    def logs(c, seguir):
        if c.tipo == "app":
            if seguir:
                return Comando("journalctl -u %s -f -n 20" % c.unidade,
                               "Acompanha o journal do serviço em tempo real (últimas 20 linhas e as novas).",
                               consulta=True)
            return Comando("journalctl -u %s -n 50 --no-pager" % c.unidade,
                           "Mostra as últimas 50 linhas do journal do serviço systemd.", consulta=True)
        if seguir:
            return Comando("docker logs -f --tail 20 %s" % c.unidade,
                           "Acompanha os logs do container em tempo real (últimas 20 linhas e as novas).",
                           consulta=True)
        return Comando("docker logs --tail 50 %s" % c.unidade,
                       "Mostra as últimas 50 linhas de log do container (Caddy registra em JSON).", consulta=True)

    @staticmethod
    def residuos():
        units = " ".join(c.unidade for c in comps_tipo("app"))
        filtros = " ".join("--filter 'name=^%s$'" % c.unidade for c in CTX.comps if c.tipo != "app")
        return [
            Comando("systemctl is-active " + units,
                    "Estado dos serviços dos servidores (esperado: nenhum 'active').", consulta=True),
            Comando("docker ps " + filtros + " --format '{{.Names}} {{.Status}}'",
                    "Containers do laboratório em execução (esperado: nenhum).", consulta=True),
            Comando("pgrep -af '[a]pp_server.py'",
                    "Processos Python dos servidores, inclusive órfãos (esperado: nenhum). "
                    "O [a] evita que o próprio comando apareça no resultado.", consulta=True),
            Comando(Cmd.portas(cabecalho=False).linha,
                    "Portas do laboratório ainda em escuta (esperado: nenhuma).", consulta=True),
        ]


# ======================================================================
# STATUS DOS COMPONENTES
# ======================================================================

class Estado:
    __slots__ = ("estado", "rodando", "detalhe", "bruto")

    def __init__(self, estado, rodando, detalhe="", bruto=""):
        self.estado, self.rodando, self.detalhe, self.bruto = estado, rodando, detalhe, bruto


def _props(txt):
    return dict(l.split("=", 1) for l in txt.splitlines() if "=" in l)


def _idade_monotonic(us):
    try:
        v = int(us)
    except (TypeError, ValueError):
        return None
    return time.monotonic() - v / 1e6 if v > 0 else None


def _idade_docker(ts):
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", ts or "")
    if not m or ts.startswith("0001"):
        return None
    inicio = datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - inicio).total_seconds()


def _primeira_linha(s):
    s = (s or "").strip()
    return s.splitlines()[0][:70] if s else "sem detalhes"


def avaliar(c, s_proc, s_http):
    if c.tipo == "app":
        return _avaliar_app(c, s_proc, s_http)
    return _avaliar_container(c, s_proc, s_http)


def _avaliar_app(c, s_proc, s_http):
    p = _props(s_proc.stdout)
    if s_proc.rc != 0 or "LoadState" not in p:
        return Estado("ERRO", False, "falha ao consultar o systemd: " + _primeira_linha(s_proc.stderr), "?")
    load, ativo = p.get("LoadState", ""), p.get("ActiveState", "")
    sub, pid = p.get("SubState", ""), p.get("MainPID", "0")
    http_ok = s_http.ok and s_http.stdout.strip() == "ok " + c.chave
    if load == "not-found":
        return Estado("AUSENTE", False, "serviço %s não instalado (requer make up)" % c.unidade, "not-found")
    if ativo == "active":
        if http_ok:
            return Estado("ONLINE", True, "PID %s · /health respondeu 'ok %s'" % (pid, c.chave), ativo)
        idade = _idade_monotonic(p.get("ActiveEnterTimestampMonotonic"))
        if idade is not None and idade < JANELA_PARTIDA:
            return Estado("INICIALIZANDO", True,
                          "processo iniciado (PID %s), aguardando a porta %s" % (pid, c.porta), ativo)
        return Estado("ERRO", True, "processo ativo (PID %s), mas /health não responde (%s)"
                      % (pid, motivo_curl(s_http)), ativo)
    if ativo in ("activating", "reloading"):
        det = "reinício automático após falha (Restart=always)" if sub == "auto-restart" else \
            "systemd iniciando o serviço"
        return Estado("INICIALIZANDO", True, det, ativo)
    if ativo == "deactivating":
        return Estado("ENCERRANDO", True, "systemd encerrando o serviço", ativo)
    if ativo == "failed":
        return Estado("ERRO", False, "serviço em estado de falha (failed)", ativo)
    return Estado("OFFLINE", False, "serviço parado (inactive)", ativo or "inactive")


def _avaliar_container(c, s_proc, s_http):
    if s_proc.rc != 0:
        msg = (s_proc.stderr or s_proc.stdout).strip()
        if "no such object" in msg.lower() or "no such container" in msg.lower():
            extra = " (requer make up)" if c.tipo == "lb" else ""
            return Estado("AUSENTE", False, "container %s não existe%s" % (c.unidade, extra), "ausente")
        return Estado("ERRO", False, "Docker indisponível: " + _primeira_linha(msg), "?")
    partes = s_proc.stdout.split()
    status = partes[0] if partes else "?"
    iniciado = partes[1] if len(partes) > 1 else ""
    codigo = partes[2] if len(partes) > 2 else "?"
    if status == "running":
        resp = s_http.stdout.split()
        http = resp[0] if resp else "000"
        if c.tipo == "lb":
            ok = s_http.ok and http == "200"
            det = "/health respondeu %s" % http
        else:
            ok = http not in ("000", "")
            if http == "200" and len(resp) >= 3:
                det = "requisição de teste: HTTP 200 via %s -> %s" % (resp[1], resp[2])
            elif http in ("502", "503"):
                det = "responde HTTP %s: nenhum load balancer disponível" % http
            else:
                det = "responde HTTP %s" % http
        if ok:
            return Estado("ONLINE", True, det, status)
        idade = _idade_docker(iniciado)
        if idade is not None and idade < JANELA_PARTIDA:
            return Estado("INICIALIZANDO", True, "container iniciado, aguardando o Caddy abrir a porta", status)
        return Estado("ERRO", True, "container em execução, mas sem resposta HTTP (%s)" % motivo_curl(s_http),
                      status)
    if status == "restarting":
        return Estado("INICIALIZANDO", True, "Docker reiniciando o container", status)
    if status == "paused":
        return Estado("ERRO", True, "container pausado (docker unpause %s)" % c.unidade, status)
    if status == "dead":
        return Estado("ERRO", False, "container em estado 'dead'", status)
    if status == "created":
        return Estado("OFFLINE", False, "container criado, nunca iniciado", status)
    return Estado("OFFLINE", False, "container parado (exited, código %s)" % codigo, status)


def coletar():
    """Consulta todos os componentes em paralelo (mesmos comandos da opção 1)."""
    comps = CTX.comps
    with ThreadPoolExecutor(max_workers=2 * len(comps) + 1) as ex:
        futuros = {c.chave: [ex.submit(cmd.capturar) for cmd in Cmd.status(c)] for c in comps}
        f_rede = ex.submit(Cmd.rede().capturar)
    est = {c.chave: avaliar(c, *[f.result() for f in futuros[c.chave]]) for c in comps}
    CTX.rede_ok = f_rede.result().ok
    return est


def tabela_status(est, comps=None, numerar=False, detalhes="auto"):
    for c in comps or CTX.comps:
        e = est[c.chave]
        num = "[%d] " % c.numero if numerar else ""
        linha = " " + num + ajustar(c.nome_fmt(), 27)
        if not numerar:
            linha += ajustar(C.CINZA + c.gerenciador + C.RESET, 17)
        linha += ajustar(c.endereco, 17) + selo(e.estado)
        print(linha)
        mostrar = detalhes == "todos" or (detalhes == "auto" and
                                           (e.estado not in ("ONLINE", "OFFLINE") or c.tipo == "front"))
        if mostrar and e.detalhe:
            cor = C.CINZA if e.estado in ("ONLINE", "OFFLINE") else C.AMARELO
            print(" " * (len(num) + 3) + cor + "└ " + e.detalhe + C.RESET)


def nomes(comps):
    return ", ".join(c.chave for c in comps)


def aguardar(comps, alvos, timeout, rotulo, mostrar_consultas=True):
    """Consulta o status a cada segundo até todos atingirem um dos estados alvo."""
    print()
    info("%s: consultando o status a cada 1 s (até %d s)." % (rotulo, timeout))
    if mostrar_consultas:
        if len(comps) == 1:
            texto("Consultas usadas (somente leitura):", recuo=3, cor=C.CINZA)
            for cmd in Cmd.status(comps[0]):
                cmd.exibir(recuo=5)
        else:
            texto("Consultas usadas: as mesmas da opção 1 (systemctl show / docker inspect + curl de "
                  "cada componente).", recuo=3, cor=C.CINZA)
    prog = Progresso()
    t0 = time.time()
    while True:
        est = coletar()
        pend = [c for c in comps if est[c.chave].estado not in alvos]
        decorrido = time.time() - t0
        if pend:
            msg = "aguardando " + ", ".join("%s (%s)" % (c.chave, est[c.chave].estado) for c in pend)
        else:
            msg = "pronto: " + ", ".join("%s %s" % (c.chave, est[c.chave].estado) for c in comps)
        prog.mostrar("   [%2ds] %s" % (int(decorrido), msg))
        if not pend or decorrido >= timeout:
            break
        time.sleep(1)
    prog.fim()
    return est


# ======================================================================
# EXECUÇÃO DE PLANOS (sequências de comandos)
# ======================================================================

def apresentar_e_confirmar(titulo_op, descricao, cmds, nota=None, pergunta=None):
    print()
    traco()
    print(" " + C.NEGRITO + "OPERAÇÃO SELECIONADA: " + titulo_op.upper() + C.RESET)
    traco()
    print()
    print(" " + C.NEGRITO + "Descrição:" + C.RESET)
    texto(descricao, recuo=1)
    print()
    print(" " + C.NEGRITO + ("Comando que será executado:" if len(cmds) == 1 else
                             "Comandos que serão executados, nesta ordem:") + C.RESET)
    print(" " + C.CINZA + "(no WSL %s, diretório %s)" % (CTX.distro, CTX.lab_curto) + C.RESET)
    print()
    for i, cmd in enumerate(cmds, 1):
        cmd.exibir(numero=i if len(cmds) > 1 else None, com_descricao=len(cmds) > 1)
    if nota:
        print()
        texto(nota, recuo=1, cor=C.CINZA)
    print()
    traco()
    if pergunta is None:
        pergunta = "Deseja executar este comando?" if len(cmds) == 1 else "Deseja executar estes comandos?"
    return confirmar(pergunta)


def executar_plano(cmds):
    """Executa os comandos na ordem; após cada um que inicia/para componentes,
    verifica de verdade se eles chegaram ao estado esperado antes de seguir."""
    est = None
    total = len(cmds)
    with OperacaoCritica():
        for i, cmd in enumerate(cmds, 1):
            print()
            info(("Executando comando %d/%d:" % (i, total)) if total > 1 else "Executando comando:")
            print()
            cmd.exibir()
            if cmd.longo:
                print()
            s = cmd.executar()
            rodape(s, cmd)
            if not (cmd.alvos and cmd.efeito):
                continue
            if not s.ok:
                est = coletar()
                continue
            if cmd.efeito == "iniciar":
                est = aguardar(cmd.alvos, ESTADOS_RODANDO_OK, TIMEOUT_SUBIDA,
                               "Verificando se %s %s" % (nomes(cmd.alvos), _plural(cmd.alvos, "responde",
                                                                                     "respondem")),
                               mostrar_consultas=len(cmd.alvos) == 1 or i == 1)
            else:
                est = aguardar(cmd.alvos, ESTADOS_PARADO, TIMEOUT_DESCIDA,
                               "Confirmando o encerramento de %s" % nomes(cmd.alvos),
                               mostrar_consultas=len(cmd.alvos) == 1 or i == 1)
    return est or coletar()


def verificar_reintegracao(comp, est):
    """Após iniciar um app/LB, confirma com requisições reais que a camada de
    cima voltou a usá-lo (health check ativo do Caddy)."""
    if comp.tipo == "app":
        via = next((c for c in comps_tipo("lb") if est[c.chave].estado == "ONLINE"), None)
        if not via:
            aviso("Nenhum load balancer online: não há rodízio para verificar.")
            return
        cmd = Comando("curl -s -o /dev/null --max-time 5 -w '%{http_code} %header{x-servidor}' "
                      "http://" + via.endereco + "/",
                      "Requisição real ao %s; repetida até o %s aparecer como servidor que atendeu."
                      % (via.chave, comp.chave), consulta=True)
    elif comp.tipo == "lb":
        via = next((c for c in comps_tipo("front") if est[c.chave].estado == "ONLINE"), None)
        if not via:
            aviso("Caddy frontal fora do ar: não há como verificar a reintegração do %s." % comp.chave)
            return
        cmd = Comando("curl -s -o /dev/null --max-time 5 -w '%{http_code} %header{x-load-balancer}' "
                      + via.url_saude,
                      "Requisição real pela entrada única; repetida até o %s aparecer como LB que atendeu."
                      % comp.chave, consulta=True)
    else:
        return
    print()
    info("Verificando a volta do %s ao rodízio do %s (health check ativo a cada 3 s; "
         "costuma levar de 3 a 10 s):" % (comp.chave, via.chave if via.tipo == "lb" else "a2-front"))
    print()
    cmd.exibir()
    fluxo = LinhaFluxo("     respostas: ")
    t0, n, achou = time.time(), 0, False
    while True:
        s = cmd.capturar(10)
        n += 1
        partes = s.stdout.split()
        cod = partes[0] if partes else "000"
        quem = partes[1] if len(partes) > 1 else "-"
        alvo = quem == comp.chave
        cor = (C.VERDE + C.NEGRITO) if alvo else (C.BRANCO if cod == "200" else C.VERMELHO)
        fluxo.add(cor + "%s %s" % (cod, quem) + C.RESET)
        if alvo:
            achou = True
            break
        if time.time() - t0 >= TIMEOUT_REINTEGRACAO:
            break
        time.sleep(0.5)
    fluxo.fim()
    registrar("CONSULTA", cmd.linha + "   [repetido %dx]" % n, 0 if achou else 1, time.time() - t0)
    if achou:
        sucesso("O %s voltou ao rodízio após %s s (%d requisições)." % (comp.chave, dec(time.time() - t0), n))
    else:
        aviso("O %s ainda não apareceu nas respostas após %d s." % (comp.chave, TIMEOUT_REINTEGRACAO))


def demonstrar_sem(comp, est):
    """Após derrubar um app/LB, mostra com requisições reais que o tráfego
    continua sendo atendido pelos demais (tolerância a falhas)."""
    if comp.tipo == "app":
        via = next((c for c in comps_tipo("lb") if est[c.chave].estado == "ONLINE"), None)
        outros = [a for a in comps_tipo("app") if a is not comp and est[a.chave].estado == "ONLINE"]
        if not via:
            return
        if not outros:
            aviso("Nenhum servidor restante: os load balancers passam a responder 502 (falha total).")
            return
        url, campo = "http://%s/" % via.endereco, 3     # "200  lb1 -> app3": servidor
    elif comp.tipo == "lb":
        via = next((c for c in comps_tipo("front") if est[c.chave].estado == "ONLINE"), None)
        outros = [l for l in comps_tipo("lb") if l is not comp and est[l.chave].estado == "ONLINE"]
        if not via:
            return
        if not outros:
            aviso("Nenhum load balancer restante: a entrada única passa a responder 502.")
            return
        url, campo = via.url_saude, 1                   # "200  lb1 -> app3": LB
    else:
        return
    cmd = Cmd.requisicoes(url, 8, "8 requisições reais: código HTTP, LB e servidor que atenderam cada uma.")
    print()
    info("Consequência na prática: 8 requisições pelo %s (consulta, executada automaticamente):"
         % (via.chave if via.tipo == "lb" else "a2-front"))
    print()
    cmd.exibir()
    s = cmd.executar()
    respostas = [l.split() for l in s.stdout.splitlines() if l.strip()]
    ok200 = sum(1 for r in respostas if r and r[0] == "200")
    atendentes = [r[campo] if len(r) > campo else "" for r in respostas if r and r[0] == "200"]
    if ok200 == len(respostas) == 8 and comp.chave not in atendentes:
        sucesso("8/8 requisições atendidas (HTTP 200) sem o %s: o tráfego seguiu pelos demais (%s)."
                % (comp.chave, ", ".join(sorted(set(atendentes)))))
    else:
        aviso("%d/8 requisições com HTTP 200; atendidas por: %s." % (ok200, ", ".join(atendentes) or "-"))


# ======================================================================
# TELAS DO MENU
# ======================================================================

def escolher_componente(titulo, pode, motivo, vazio, nota_excluidos):
    """Lista os componentes elegíveis (numeração fixa de 1 a 7)."""
    while True:
        est = coletar()
        cabecalho(titulo)
        eleg = [c for c in CTX.comps if pode(est[c.chave])]
        if not eleg:
            print()
            aviso(vazio)
            print()
            tabela_status(est)
            pausar()
            return None, est
        print()
        print(" Selecione o componente:")
        print()
        tabela_status(est, comps=eleg, numerar=True, detalhes="nenhum")
        excl = [c for c in CTX.comps if c not in eleg]
        if excl:
            print()
            texto("%s: %s" % (nota_excluidos, ", ".join("%s (%s)" % (c.chave, est[c.chave].estado)
                                                         for c in excl)), recuo=1, cor=C.CINZA)
        print()
        print(" [0] Voltar ao menu principal")
        print()
        r = ler("Selecione uma opção:")
        if r == "0":
            return None, est
        comp = next((c for c in CTX.comps if str(c.numero) == r), None)
        if comp is None:
            erro("Opção inválida: '%s'. Digite um dos números listados." % r if r else
                 "Nenhuma opção digitada. Digite um dos números listados.")
        elif comp not in eleg:
            erro(motivo(comp, est[comp.chave]))
        else:
            return comp, est
        pausar("Pressione ENTER para escolher novamente.")


def descricao_parar(comp, est):
    if comp.tipo == "app":
        outros = [a for a in comps_tipo("app") if a is not comp and est[a.chave].estado == "ONLINE"]
        t = ("Para o serviço systemd %s, que executa o servidor Python %s (cor %s, porta %s). "
             "O systemd envia SIGTERM e o servidor encerra de forma limpa. Por ser uma parada "
             "intencional, o systemd NÃO o religa sozinho (Restart=always vale só para falhas). "
             "Os load balancers percebem a queda (tentativa com falha + health check em /health a "
             "cada 3 s) e retiram o %s do rodízio sem que o cliente receba erro."
             % (comp.unidade, comp.chave, comp.cor_nome, comp.porta, comp.chave))
        if not outros:
            t += ("\n\nATENÇÃO: %s é o último servidor online. Sem servidores, os load balancers "
                  "passarão a responder 502." % comp.chave)
        return t
    if comp.tipo == "lb":
        outro = [l for l in comps_tipo("lb") if l is not comp]
        t = ("Para o container Docker %s (Caddy, porta %s). O Docker envia SIGTERM ao Caddy e, se ele "
             "não terminar em 10 s, SIGKILL. O Caddy frontal (a2-front) detecta a queda e passa a "
             "enviar todo o tráfego para o %s. Como a política de reinício é unless-stopped, o "
             "container fica parado até um docker start."
             % (comp.unidade, comp.porta, nomes(outro) or "outro LB"))
        if outro and est[outro[0].chave].estado != "ONLINE":
            t += ("\n\nATENÇÃO: o %s também está fora do ar; a entrada única deixará de atender "
                  "(HTTP 502)." % outro[0].chave)
        return t
    return ("Para o container a2-front, o Caddy frontal que é a entrada única em %s. Os load "
            "balancers e os servidores continuam funcionando e podem ser acessados diretamente "
            "(%s), mas a entrada única fica indisponível."
            % (comp.url_saude, ", ".join(l.endereco for l in comps_tipo("lb"))))


def descricao_iniciar(comp, est, cmds):
    if any(c.linha == "make up" for c in cmds):
        return ("O %s não está implantado (%s). A forma correta de recriá-lo é reaplicar a "
                "infraestrutura com o Ansible: make up executa playbooks/site.yml (rede Docker, "
                "serviços systemd e containers Caddy). É idempotente, mas inicia TODOS os servidores "
                "e load balancers que estiverem parados. Pode levar de 30 s a alguns minutos."
                % (comp.rotulo, est[comp.chave].detalhe if CTX.rede_ok else "rede Docker %s ausente" % CTX.rede))
    if comp.tipo == "app":
        t = ("Inicia o serviço systemd %s: o systemd executa %s/bin/app_server.py com --name %s "
             "--port %s. Em seguida o painel confirma que o servidor responde em /health e que os "
             "load balancers o devolvem ao rodízio (health check ativo a cada 3 s)."
             % (comp.unidade, CTX.dir_dados, comp.chave, comp.porta))
        if est[comp.chave].bruto == "failed":
            t += " Como o serviço está em estado de falha, antes é preciso limpar esse estado (reset-failed)."
        return t
    if comp.tipo == "lb":
        t = ("Inicia o container Docker %s (Caddy, porta %s, configuração em %s/caddy/%s/Caddyfile). "
             "Depois o painel confirma que o Caddy responde em /health e que o a2-front volta a usá-lo."
             % (comp.unidade, comp.porta, CTX.dir_dados, comp.chave))
        if not any(est[a.chave].estado == "ONLINE" for a in comps_tipo("app")):
            t += "\n\nATENÇÃO: nenhum servidor está online; o %s responderá 502 até algum subir." % comp.chave
        return t
    t = ("Inicia o Caddy frontal (container a2-front, rede host, porta %s), a entrada única que "
         "distribui as requisições entre lb1 e lb2." % comp.porta)
    if not any(est[l.chave].estado == "ONLINE" for l in comps_tipo("lb")):
        t += "\n\nATENÇÃO: nenhum load balancer está online; a entrada responderá 502 até algum subir."
    return t


def mostrar_resultado(comp, est, alvo_ok, msg_ok, msg_falha):
    e = est[comp.chave]
    print()
    if e.estado in alvo_ok:
        sucesso(msg_ok)
    else:
        erro("%s (%s)" % (msg_falha, e.detalhe))
        if comp.tipo == "app":
            info("Veja os logs na opção 5 (journalctl -u %s -n 50 --no-pager)." % comp.unidade)
        else:
            info("Veja os logs na opção 5 (docker logs --tail 50 %s)." % comp.unidade)
    print()
    print(" Status atual: " + selo(e.estado) + "  " + C.CINZA + e.detalhe + C.RESET)


# ---------------------------------------------------------------- opção 1
def menu_status():
    cabecalho("STATUS DETALHADO DOS COMPONENTES")
    print()
    texto("Comandos de consulta: não alteram nada, por isso são executados automaticamente. "
          "Para cada componente o painel verifica (1) o processo e (2) se ele realmente responde por HTTP.",
          cor=C.CINZA)
    est = {}
    grupos = [("A. Servidores de aplicação (serviços systemd + HTTP direto)", ("app",)),
              ("B. Load balancers e Caddy frontal (containers Docker + HTTP)", ("lb", "front"))]
    for titulo, tipos in grupos:
        secao(titulo)
        for c in [x for x in CTX.comps if x.tipo in tipos]:
            print()
            print(" " + C.NEGRITO + c.nome_fmt() + C.RESET + C.CINZA + "  (" + c.gerenciador + ", " +
                  c.endereco + ")" + C.RESET)
            saidas = []
            for cmd in Cmd.status(c):
                texto("# " + cmd.descricao, recuo=3, cor=C.CINZA)
                cmd.exibir()
                s = cmd.executar()
                if not s.ok:
                    rodape(s, cmd)
                saidas.append(s)
            est[c.chave] = avaliar(c, *saidas)
            e = est[c.chave]
            print("   -> " + selo(e.estado) + " " + e.detalhe)
    secao("C. Rede Docker e portas em escuta")
    for cmd in (Cmd.rede(), Cmd.portas()):
        print()
        texto("# " + cmd.descricao, recuo=3, cor=C.CINZA)
        cmd.exibir()
        s = cmd.executar()
        if not s.ok:
            rodape(s, cmd)
    secao("D. Resumo")
    tabela_status(est, detalhes="todos")
    pausar()


# ---------------------------------------------------------------- opção 2
def menu_derrubar():
    comp, est = escolher_componente(
        "ENCERRAR COMPONENTE", pode=lambda e: e.rodando,
        motivo=lambda c, e: "%s já está %s: não há o que encerrar." % (c.rotulo, e.estado),
        vazio="Nenhum componente em execução: todos já estão desligados.",
        nota_excluidos="Já desligados (não aparecem na lista)")
    if not comp:
        return
    cmds = Cmd.parar([comp])
    if not apresentar_e_confirmar("Encerrar %s" % comp.rotulo, descricao_parar(comp, est), cmds):
        pausar()
        return
    cabecalho("RESULTADO")
    est = executar_plano(cmds)
    mostrar_resultado(comp, est, ESTADOS_PARADO, "%s encerrado com sucesso!" % comp.rotulo,
                      "%s não foi encerrado." % comp.rotulo)
    if est[comp.chave].estado in ESTADOS_PARADO:
        demonstrar_sem(comp, est)
    pausar()


# ---------------------------------------------------------------- opção 3
def menu_inicializar():
    comp, est = escolher_componente(
        "INICIALIZAR COMPONENTE", pode=lambda e: not e.rodando,
        motivo=lambda c, e: "%s já está em execução (%s). Para reiniciá-lo, use a opção 4."
                            % (c.rotulo, e.estado),
        vazio="Todos os componentes já estão em execução: nada a inicializar.",
        nota_excluidos="Já em execução (não aparecem na lista)")
    if not comp:
        return
    cmds, problemas = Cmd.iniciar([comp], est)
    if problemas:
        print()
        for p in problemas:
            erro(p)
        pausar()
        return
    if not apresentar_e_confirmar("Inicializar %s" % comp.rotulo, descricao_iniciar(comp, est, cmds), cmds):
        pausar()
        return
    cabecalho("RESULTADO")
    est = executar_plano(cmds)
    mostrar_resultado(comp, est, ESTADOS_RODANDO_OK, "%s inicializado e respondendo!" % comp.rotulo,
                      "%s não ficou ONLINE em %d s." % (comp.rotulo, TIMEOUT_SUBIDA))
    if est[comp.chave].estado == "ONLINE":
        verificar_reintegracao(comp, est)
    pausar()


# ---------------------------------------------------------------- opção 4
def menu_reiniciar():
    comp, est = escolher_componente(
        "REINICIAR COMPONENTE", pode=lambda e: e.estado != "AUSENTE",
        motivo=lambda c, e: "%s não está implantado (%s). Use a opção 3 para criá-lo."
                            % (c.rotulo, e.detalhe),
        vazio="Nenhum componente implantado. Use a opção 3 ou 7.",
        nota_excluidos="Não implantados (use a opção 3)")
    if not comp:
        return
    e0 = est[comp.chave]
    if e0.rodando:
        cmds_parar = Cmd.parar([comp])
        cmds_iniciar = Cmd.iniciar_simples([comp])
        equivalente = ("sudo systemctl restart %s" % comp.unidade if comp.tipo == "app"
                       else "docker restart %s" % comp.unidade)
        desc = ("Encerra e inicia novamente o %s. Equivale a '%s'; aqui os dois passos são executados "
                "separadamente para que você veja o componente cair (OFFLINE) e voltar (ONLINE). "
                "Após cada passo o painel confirma o estado real." % (comp.rotulo, equivalente))
        nota = ("Sequência: o encerramento precisa terminar antes da inicialização; a inicialização só "
                "é dada como concluída quando o componente volta a responder por HTTP.")
    else:
        cmds_parar = []
        cmds_iniciar, problemas = Cmd.iniciar([comp], est)
        if problemas:
            print()
            for p in problemas:
                erro(p)
            pausar()
            return
        desc = ("O %s não está em execução (%s), então não há o que encerrar: o reinício executará "
                "apenas a inicialização." % (comp.rotulo, e0.estado))
        nota = None
    cmds = cmds_parar + cmds_iniciar
    if not apresentar_e_confirmar("Reiniciar %s" % comp.rotulo, desc, cmds, nota):
        pausar()
        return
    cabecalho("RESULTADO")
    if cmds_parar:
        secao("Etapa 1/2: encerramento")
        est = executar_plano(cmds_parar)
        print()
        print(" Estado após o encerramento: " + selo(est[comp.chave].estado))
        secao("Etapa 2/2: inicialização")
    est = executar_plano(cmds_iniciar)
    mostrar_resultado(comp, est, ESTADOS_RODANDO_OK, "%s reiniciado e respondendo!" % comp.rotulo,
                      "%s não voltou a ficar ONLINE." % comp.rotulo)
    if est[comp.chave].estado == "ONLINE":
        verificar_reintegracao(comp, est)
    pausar()


# ---------------------------------------------------------------- opção 5
def menu_logs():
    while True:
        est = coletar()
        cabecalho("CONSULTAR LOGS")
        print()
        print(" Selecione o componente:")
        print()
        tabela_status(est, numerar=True, detalhes="nenhum")
        n_hist = len(CTX.comps) + 1
        print()
        print(" [%d] Histórico de comandos executados por este painel" % n_hist)
        print()
        print(" [0] Voltar ao menu principal")
        print()
        validas = {str(c.numero) for c in CTX.comps} | {str(n_hist), "0"}
        r = escolher(validas)
        if r == "0":
            return
        if r == str(n_hist):
            cmd = Comando("tail -n 40 " + ARQ_LOG_EXIBICAO,
                          "Últimas 40 linhas do histórico (data, tipo, código de saída, duração, comando).",
                          consulta=True)
            cabecalho("HISTÓRICO DE COMANDOS DO PAINEL")
            print()
            texto("# " + cmd.descricao, recuo=3, cor=C.CINZA)
            cmd.exibir()
            print()
            s = cmd.executar(mostrar_saida=False)
            imprimir_saida(s, recuo=1)
            pausar("Pressione ENTER para voltar.")
            continue
        comp = next(c for c in CTX.comps if str(c.numero) == r)
        submenu_logs(comp)


def submenu_logs(comp):
    while True:
        cabecalho("LOGS · %s" % comp.rotulo.upper())
        print()
        print(" [1] Exibir as últimas 50 linhas")
        print(" [2] Acompanhar em tempo real (ENTER ou Ctrl+C para parar)")
        print()
        print(" [0] Voltar")
        print()
        r = escolher({"0", "1", "2"})
        if r == "0":
            return
        cmd = Cmd.logs(comp, seguir=(r == "2"))
        cabecalho("LOGS · %s" % comp.rotulo.upper())
        print()
        texto("# " + cmd.descricao, recuo=3, cor=C.CINZA)
        cmd.exibir()
        print()
        if r == "1":
            s = cmd.executar(mostrar_saida=False)
            imprimir_saida(s, recuo=1)
            if not s.ok:
                rodape(s, cmd)
            pausar("Pressione ENTER para voltar.")
        else:
            info("Acompanhando. Pressione ENTER (ou Ctrl+C) para parar e voltar. "
                 "O componente NÃO é afetado: só a visualização é encerrada.")
            print()
            acompanhar(cmd)
            print()
            info("Visualização encerrada; o %s continua no estado em que estava." % comp.chave)
            pausar("Pressione ENTER para voltar.")


def _colorir_trafego(linha):
    m = re.match(r"^(\S+)\s+(\d{3})\s+(\S*)\s*->\s*(\S*)", linha)
    if not m:
        return linha
    hora, cod, lb, app = m.groups()
    comp = comp_por_chave(app)
    cor_app = cor_rgb(comp.cor_hex) if comp and comp.cor_hex else C.BRANCO
    cor_cod = C.VERDE if cod == "200" else C.VERMELHO
    return "%s%s%s  %s%s%s  %s%-4s%s -> %s%s%s%s" % (
        C.CINZA, hora, C.RESET, cor_cod, cod, C.RESET, C.CIANO, lb or "-", C.RESET,
        cor_app, C.NEGRITO, app or "-", C.RESET)


def acompanhar(cmd, colorir=None):
    """Executa um comando contínuo até o usuário pressionar ENTER ou Ctrl+C.
    Encerra apenas o comando de visualização (grupo de processos próprio)."""
    sys.stdout.flush()
    t0 = time.time()
    proc = subprocess.Popen(cmd._argv(), cwd=CTX.lab_dir, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if colorir else None,
                            stderr=subprocess.STDOUT if colorir else None,
                            start_new_session=True)
    parar = []
    anterior = signal.signal(signal.SIGINT, lambda s, f: parar.append(1))
    fontes = [sys.stdin]
    if colorir:
        fontes.append(proc.stdout)
    pendente = b""
    try:
        while not parar:
            if not colorir and proc.poll() is not None:
                break
            try:
                prontos, _, _ = select.select(fontes, [], [], 0.3)
            except InterruptedError:
                continue
            if sys.stdin in prontos:
                sys.stdin.readline()
                break
            if colorir and proc.stdout in prontos:
                dados = os.read(proc.stdout.fileno(), 4096)
                if not dados:
                    break
                pendente += dados
                while b"\n" in pendente:
                    linha, pendente = pendente.split(b"\n", 1)
                    print("   " + colorir(_txt(linha)))
    finally:
        signal.signal(signal.SIGINT, anterior)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        registrar("CONSULTA", cmd.linha + "   [acompanhado por %s s]" % dec(time.time() - t0), 0,
                  time.time() - t0)


# ---------------------------------------------------------------- opção 6
def menu_testes():
    while True:
        est = coletar()
        cabecalho("TESTES DE FUNCIONAMENTO")
        print()
        tabela_status(est, detalhes="nenhum")
        secao("Consultas (não alteram nenhum componente)")
        print(" [1] Bateria rápida: disponibilidade, comunicação e distribuição")
        print(" [2] Monitor de tráfego ao vivo (função 'trafego' do guia)")
        print(" [3] Verificação Ansible: make verify")
        secao("Testes de tolerância a falhas (derrubam e restauram componentes)")
        print(" [4] Todos os testes automatizados: make test (T3 a T7)")
        print(" [5] Um teste específico: make test-t3 ... make test-t7")
        print()
        print(" [0] Voltar ao menu principal")
        print()
        r = escolher({"0", "1", "2", "3", "4", "5"})
        if r == "0":
            return
        if r == "1":
            teste_bateria_rapida()
        elif r == "2":
            teste_trafego()
        elif r == "3":
            teste_make("verify", None)
        elif r == "4":
            teste_make("test", "todos")
        else:
            submenu_testes_especificos()


def teste_bateria_rapida():
    est = coletar()
    cabecalho("TESTES · BATERIA RÁPIDA DE FUNCIONAMENTO")
    print()
    texto("Somente consultas: nada é alterado, por isso os comandos são executados automaticamente.",
          cor=C.CINZA)
    resultados = []   # (descrição, True/False/None, detalhe)
    apps, lbs, fronts = comps_tipo("app"), comps_tipo("lb"), comps_tipo("front")

    secao("1. Disponibilidade dos servidores (acesso direto, sem load balancer)")
    apps_ok = []
    for a in apps:
        cmd = Cmd.status(a)[1]
        cmd.exibir()
        s = cmd.executar()
        ok = s.ok and s.stdout.strip() == "ok " + a.chave
        if ok:
            apps_ok.append(a.chave)
        resultados.append(("Servidor %s responde em /health" % a.chave, ok, "" if ok else motivo_curl(s)))

    secao("2. Disponibilidade dos load balancers (Caddy)")
    lbs_ok = []
    for l in lbs:
        cmd = Cmd.status(l)[1]
        cmd.exibir()
        s = cmd.executar()
        ok = s.ok and s.stdout.strip() == "200"
        if ok:
            lbs_ok.append(l)
        resultados.append(("Load balancer %s responde em /health" % l.chave, ok,
                           "" if ok else (motivo_curl(s) if not s.ok else "HTTP %s" % s.stdout.strip())))

    secao("3. Comunicação LB -> servidores e distribuição round robin (20 requisições por LB)")
    texto("Esperado: todas com HTTP 200, divididas igualmente entre os servidores online (%s)."
          % (", ".join(apps_ok) or "nenhum"), recuo=1, cor=C.CINZA)
    for l in lbs:
        if l not in lbs_ok:
            resultados.append(("%s encaminha requisições aos servidores" % l.chave, None, "LB fora do ar"))
            resultados.append(("%s distribui igualmente (round robin)" % l.chave, None, "LB fora do ar"))
            continue
        print()
        cmd = Cmd.contagem("http://%s/" % l.endereco, 20)
        cmd.exibir()
        s = cmd.executar()
        contagem = {}
        total200 = 0
        for linha in s.stdout.splitlines():
            m = re.match(r"^\s*(\d+)\s+(\d{3})\s*(\S*)", linha)
            if m:
                n, cod, srv = int(m.group(1)), m.group(2), m.group(3)
                if cod == "200":
                    total200 += n
                    contagem[srv] = contagem.get(srv, 0) + n
        resultados.append(("%s encaminha requisições aos servidores" % l.chave, total200 == 20,
                           "%d/20 com HTTP 200" % total200))
        vals = list(contagem.values())
        uniforme = (bool(apps_ok) and sorted(contagem) == sorted(apps_ok) and max(vals) - min(vals) <= 1)
        resultados.append(("%s distribui igualmente (round robin)" % l.chave, uniforme,
                           " ".join("%s=%d" % kv for kv in sorted(contagem.items())) or "nenhuma resposta"))

    if fronts:
        f = fronts[0]
        secao("4. Entrada única: Caddy frontal -> load balancers (8 requisições)")
        texto("Esperado: todas com HTTP 200, alternando entre os LBs online (%s)."
              % (", ".join(l.chave for l in lbs_ok) or "nenhum"), recuo=1, cor=C.CINZA)
        print()
        cmd = Cmd.requisicoes(f.url_saude, 8, "")
        cmd.exibir()
        s = cmd.executar()
        resp = [l.split() for l in s.stdout.splitlines() if l.strip()]
        ok200 = sum(1 for r in resp if r and r[0] == "200")
        lbs_vistos = sorted({r[1] for r in resp if r and r[0] == "200" and len(r) > 1})
        resultados.append(("a2-front responde pela entrada única", ok200 == 8, "%d/8 com HTTP 200" % ok200))
        esperado = sorted(l.chave for l in lbs_ok)
        resultados.append(("a2-front alterna entre os LBs online", bool(esperado) and lbs_vistos == esperado,
                           "LBs que atenderam: %s" % (", ".join(lbs_vistos) or "nenhum")))

    secao("RESULTADO DOS TESTES")
    aprov = reprov = pul = 0
    for desc, ok, det in resultados:
        if ok is True:
            tag, aprov = C.VERDE + C.NEGRITO + "[APROVADO] " + C.RESET, aprov + 1
        elif ok is False:
            tag, reprov = C.VERMELHO + C.NEGRITO + "[REPROVADO]" + C.RESET, reprov + 1
        else:
            tag, pul = C.CINZA + C.NEGRITO + "[PULADO]   " + C.RESET, pul + 1
        print(" " + tag + " " + desc + (C.CINZA + "  (" + det + ")" + C.RESET if det else ""))
    traco()
    print(" %s%d aprovado(s)%s · %s%d reprovado(s)%s · %d pulado(s)"
          % (C.VERDE, aprov, C.RESET, C.VERMELHO if reprov else "", reprov, C.RESET, pul))
    if reprov and any(est[c.chave].estado != "ONLINE" for c in CTX.comps):
        texto("Observação: há componentes fora do ar; reprovações de disponibilidade são esperadas "
              "nesse caso. As demais verificações consideram apenas os componentes online.",
              recuo=1, cor=C.CINZA)
    pausar("Pressione ENTER para voltar.")


def teste_trafego():
    alvos = [c for c in CTX.comps if c.tipo in ("front", "lb")]
    alvos.sort(key=lambda c: 0 if c.tipo == "front" else 1)
    cabecalho("MONITOR DE TRÁFEGO AO VIVO")
    print()
    print(" Para onde enviar o tráfego?")
    print()
    opcoes = {}
    for i, c in enumerate(alvos, 1):
        url = c.url_saude if c.tipo == "front" else "http://%s/" % c.endereco
        opcoes[str(i)] = (c, url)
        extra = "  (entrada única: alterna lb1/lb2)" if c.tipo == "front" else "  (direto no balanceador)"
        print(" [%d] %s%s%s" % (i, url, C.CINZA, extra + C.RESET))
    print()
    print(" [0] Voltar")
    print()
    r = escolher(set(opcoes) | {"0"})
    if r == "0":
        return
    comp, url = opcoes[r]
    cmd = Cmd.trafego(url)
    cabecalho("MONITOR DE TRÁFEGO AO VIVO")
    print()
    texto("# " + cmd.descricao, recuo=3, cor=C.CINZA)
    cmd.exibir()
    print()
    info("Cada linha: horário, código HTTP, LB -> servidor que atendeu. Pressione ENTER (ou Ctrl+C) "
         "para parar; nenhum componente é afetado.")
    texto("Dica: abra uma segunda janela do iniciar.bat e derrube/religue componentes enquanto "
          "observa o tráfego aqui.", recuo=1, cor=C.CINZA)
    print()
    acompanhar(cmd, colorir=_colorir_trafego)
    print()
    info("Tráfego interrompido.")
    pausar("Pressione ENTER para voltar.")


DESCRICOES_TESTES = {
    "t3": ("T3 — Distribuição uniforme", "Envia 20 requisições a cada LB e exige exatamente 5 por servidor. "
           "Somente consultas.", True),
    "t4": ("T4 — Queda de um servidor com tráfego contínuo", "Para o a2-app2, envia 30 requisições ao lb1 "
           "(todas devem ter 200, nenhuma do app2) e religa o a2-app2 ao final.", False),
    "t5": ("T5 — Recuperação de nó", "Para o a2-app2, confirma que saiu do rodízio, religa e aguarda o "
           "Caddy reintegrá-lo pelo health check.", False),
    "t6": ("T6 — Falha múltipla", "Para a2-app1, a2-app2 e a2-app3; exige que o app4 atenda sozinho 12 "
           "requisições; religa os três ao final.", False),
    "t7": ("T7 — Failover entre load balancers", "Para o lb1 e exige 30 respostas só do lb2 pela entrada "
           "única; religa o lb1, para o lb2 e exige 30 respostas só do lb1; religa ambos.", False),
}


def submenu_testes_especificos():
    cabecalho("TESTES AUTOMATIZADOS (ANSIBLE)")
    print()
    for k in ("t3", "t4", "t5", "t6", "t7"):
        nome, desc, _ = DESCRICOES_TESTES[k]
        print(" [%s] %s" % (k[1], nome))
        texto(desc, recuo=5, cor=C.CINZA)
    print()
    print(" [0] Voltar")
    print()
    r = escolher({"0", "3", "4", "5", "6", "7"})
    if r != "0":
        teste_make("test-t" + r, "t" + r)


def teste_make(alvo, chave):
    est = coletar()
    cabecalho("TESTES · make %s" % alvo)
    if alvo == "verify":
        cmd = Cmd.make("verify", "Ansible (playbooks/verify.yml): confere os containers Caddy, os serviços "
                                 "systemd e se os 4 servidores atendem pelos LBs. Somente consultas.",
                       consulta=True)
        print()
        texto("# " + cmd.descricao, recuo=3, cor=C.CINZA)
        print()
        cmd.exibir()
        print()
        info("Comando de consulta: executado automaticamente.")
        print()
        s = cmd.executar()
        _resumo_make(s, cmd)
        return
    if chave == "todos":
        titulo = "Executar todos os testes automatizados (T3 a T7)"
        desc = ("Executa playbooks/testes.yml com o Ansible: " + " ".join(
            "%s: %s" % (DESCRICOES_TESTES[k][0].split(" — ")[0], DESCRICOES_TESTES[k][1])
            for k in ("t3", "t4", "t5", "t6", "t7")))
        so_consulta = False
    else:
        nome, d, so_consulta = DESCRICOES_TESTES[chave]
        titulo, desc = "Executar %s" % nome, d
    cmd = Cmd.make(alvo, "Testes de tolerância a falhas do projeto (playbooks/testes.yml).", consulta=so_consulta)
    fora = [c for c in CTX.comps if est[c.chave].estado != "ONLINE"]
    nota = ("Os testes religam ao final tudo o que derrubam (blocos 'always' do playbook). Linhas "
            "'FAILED - RETRYING' são normais: é o Ansible aguardando o health check.")
    if fora:
        nota += ("\nATENÇÃO: componentes fora do ar agora: %s. Os testes esperam o ambiente completo "
                 "(podem reprovar) e religam o que eles mesmos derrubarem." % nomes(fora))
    if so_consulta:
        print()
        texto("# " + desc, recuo=3, cor=C.CINZA)
        print()
        cmd.exibir()
        print()
        info("Somente consultas: executado automaticamente.")
        print()
        s = cmd.executar()
    else:
        if not apresentar_e_confirmar(titulo, desc, [cmd], nota):
            pausar("Pressione ENTER para voltar.")
            return
        cabecalho("RESULTADO · make %s" % alvo)
        print()
        info("Executando comando:")
        print()
        cmd.exibir()
        print()
        s = cmd.executar()
    _resumo_make(s, cmd)


def _resumo_make(s, cmd):
    print()
    rodape(s, cmd)
    print()
    if s.ok:
        sucesso("make %s concluído: APROVADO (código de saída 0)." % cmd.linha.split()[-1])
    else:
        erro("make %s REPROVADO ou interrompido (código de saída %d). Veja as mensagens do Ansible acima."
             % (cmd.linha.split()[-1], s.rc))
    est = coletar()
    secao("Status após o teste")
    tabela_status(est)
    pausar("Pressione ENTER para voltar.")


# ---------------------------------------------------------------- opção 7
def menu_reiniciar_ambiente():
    est = coletar()
    cabecalho("REINICIAR TODO O AMBIENTE")
    print()
    tabela_status(est)
    rodando = [c for c in CTX.comps if est[c.chave].rodando]
    cmds_parar = Cmd.parar(rodando)
    est_pos = {c.chave: (Estado("OFFLINE", False, "", "inactive") if est[c.chave].rodando else est[c.chave])
               for c in CTX.comps}
    cmds_iniciar, problemas = Cmd.iniciar(CTX.comps, est_pos)
    if problemas:
        print()
        for p in problemas:
            aviso(p)
    desc = ("Encerra os componentes em execução e inicializa todos novamente, respeitando as "
            "dependências. Após cada comando o painel confirma o estado real (OFFLINE / ONLINE) "
            "antes de seguir para o próximo.")
    nota = ("Ordem de encerramento: entrada (a2-front) -> load balancers -> servidores, para que "
            "nenhuma camada receba tráfego sem ter para onde encaminhá-lo. Ordem de inicialização: "
            "servidores -> load balancers -> entrada, para que cada camada só suba quando a de baixo "
            "já puder atender.")
    if not rodando:
        nota += "\nNenhum componente está em execução: apenas a inicialização será feita."
    if not apresentar_e_confirmar("Reiniciar todo o ambiente", desc, cmds_parar + cmds_iniciar, nota):
        pausar()
        return
    cabecalho("RESULTADO")
    if cmds_parar:
        secao("Etapa 1/2: encerramento")
        executar_plano(cmds_parar)
        secao("Etapa 2/2: inicialização")
    est = executar_plano(cmds_iniciar)
    secao("STATUS FINAL")
    tabela_status(est, detalhes="todos")
    print()
    fora = [c for c in CTX.comps if est[c.chave].estado != "ONLINE"]
    if not fora:
        sucesso("Ambiente reiniciado: todos os componentes estão ONLINE.")
    else:
        erro("Componentes que não ficaram ONLINE: %s." % nomes(fora))
    pausar()


# ---------------------------------------------------------------- opção 8
def verificar_residuos():
    """Confirma que nenhum processo do laboratório ficou para trás."""
    ok_geral = True
    for cmd in Cmd.residuos():
        print()
        texto("# " + cmd.descricao, recuo=3, cor=C.CINZA)
        cmd.exibir()
        s = cmd.executar()
        if cmd.linha.startswith("systemctl is-active"):
            ok = not any(l.strip() in ("active", "activating", "deactivating", "reloading")
                         for l in s.stdout.splitlines())
        else:
            ok = s.stdout.strip() == ""
        print("     " + (C.VERDE + "[OK] nada em execução" if ok else C.VERMELHO + "[FALHA] ainda há itens em execução")
              + C.RESET)
        ok_geral = ok_geral and ok
    return ok_geral


def menu_encerrar_ambiente():
    """Retorna 'SAIR' quando o ambiente foi encerrado e o painel deve fechar."""
    est = coletar()
    cabecalho("ENCERRAR TODO O AMBIENTE")
    print()
    tabela_status(est)
    print()
    print(" [1] Parar todos os componentes (recomendado)")
    texto("Mantém a implantação (/opt/a2, serviços systemd e containers). O iniciar.bat volta a "
          "subir tudo em segundos.", recuo=5, cor=C.CINZA)
    print(" [2] Parar e REMOVER a implantação (make down)")
    texto("Remove os containers lb1/lb2, a rede %s, os serviços systemd e %s. A próxima "
          "inicialização executará make up (mais lenta)." % (CTX.rede, CTX.dir_dados), recuo=5, cor=C.CINZA)
    print()
    print(" [0] Voltar ao menu principal")
    print()
    r = escolher({"0", "1", "2"})
    if r == "0":
        return None
    rodando = [c for c in CTX.comps if est[c.chave].rodando]
    if r == "1":
        if not rodando:
            print()
            aviso("Nenhum componente está em execução: não há o que encerrar.")
            secao("Verificação de processos remanescentes")
            verificar_residuos()
            print()
            print(" [1] Fechar o painel")
            print(" [2] Voltar ao menu principal")
            print()
            return "SAIR" if escolher({"1", "2"}) == "1" else None
        cmds = Cmd.parar(rodando)
        titulo = "Encerrar todo o ambiente"
        desc = ("Encerra todos os componentes em execução na ordem inversa das dependências e confirma "
                "que nenhum processo ou porta do laboratório ficou ativo. Os serviços continuam "
                "instalados e os containers continuam existindo; o iniciar.bat os religa.")
        nota = "Ordem: entrada (a2-front) -> load balancers -> servidores."
    else:
        cmds = [Cmd.make("down", "Ansible (playbooks/teardown.yml): remove containers lb1/lb2, rede Docker, "
                                 "serviços systemd, regras do UFW e o diretório %s." % CTX.dir_dados)]
        cmds[0].alvos, cmds[0].efeito = comps_tipo("app") + comps_tipo("lb"), "parar"
        front = [c for c in rodando if c.tipo == "front"]
        if front:
            cmds = Cmd.parar(front) + cmds
        titulo = "Encerrar e remover a implantação"
        desc = ("Para o Caddy frontal (se estiver rodando) e remove a implantação com make down. O "
                "repositório, o .env e o container a2-front (parado) são preservados.")
        nota = ("ATENÇÃO: operação destrutiva para a implantação. Para voltar a usar, o iniciar.bat "
                "executará make up (requer o Ansible e, possivelmente, acesso à internet para o apt).")
    if not apresentar_e_confirmar(titulo, desc, cmds, nota):
        pausar()
        return None
    cabecalho("RESULTADO")
    with OperacaoCritica():
        est = executar_plano(cmds)
        secao("Verificação de processos remanescentes")
        limpo = verificar_residuos()
    secao("STATUS FINAL")
    tabela_status(est, detalhes="todos")
    print()
    if limpo:
        sucesso("Ambiente encerrado: nenhum processo, container ou porta do laboratório permanece ativo.")
    else:
        erro("Ainda há itens do laboratório em execução (veja acima). O painel permanece aberto.")
        pausar()
        return None
    info("Para iniciar novamente, execute o iniciar.bat.")
    pausar("Pressione ENTER para fechar o painel.")
    return "SAIR"


# ---------------------------------------------------------------- opção 0
def menu_sair():
    """Retorna True se o painel deve fechar."""
    est = coletar()
    rodando = [c for c in CTX.comps if est[c.chave].rodando]
    cabecalho("SAIR DO PAINEL")
    print()
    if not rodando:
        info("Nenhum componente do laboratório está em execução.")
        pausar("Pressione ENTER para fechar esta janela.")
        return True
    tabela_status(est)
    print()
    texto("%d componente(s) estão em execução no WSL. Eles são gerenciados pelo systemd e pelo "
          "Docker, não por este painel: sair NÃO os encerra." % len(rodando))
    print()
    texto("Atenção: o WSL desliga a distribuição cerca de 1 minuto depois que a última janela do WSL "
          "é fechada, e os componentes param junto (o Docker e o systemd os religam no próximo boot). "
          "Para mantê-los no ar, mantenha este painel (ou outro terminal do Ubuntu) aberto. Ao abrir "
          "o iniciar.bat de novo, o painel religa o que estiver desligado.", cor=C.AMARELO)
    print()
    print(" [1] Sair sem encerrar os componentes")
    print(" [2] Encerrar todo o ambiente antes de sair")
    print()
    print(" [0] Voltar ao menu principal")
    print()
    try:
        r = escolher({"0", "1", "2"})
    except KeyboardInterrupt:
        print()
        r = "1"
    if r == "0":
        return False
    if r == "2":
        return menu_encerrar_ambiente() == "SAIR"
    print()
    sucesso("Painel encerrado sem alterar os componentes.")
    info("Para administrá-los novamente, execute o iniciar.bat.")
    pausar("Pressione ENTER para fechar esta janela.")
    return True


# ---------------------------------------------------------------- menu principal
ITENS_MENU = [
    ("1", "Consultar status detalhado (com os comandos de verificação)", menu_status),
    ("2", "Derrubar um servidor ou load balancer", menu_derrubar),
    ("3", "Inicializar um servidor ou load balancer", menu_inicializar),
    ("4", "Reiniciar um componente", menu_reiniciar),
    ("5", "Consultar logs dos componentes", menu_logs),
    ("6", "Executar testes de funcionamento", menu_testes),
    ("7", "Reiniciar todo o ambiente", menu_reiniciar_ambiente),
    ("8", "Encerrar todo o ambiente", menu_encerrar_ambiente),
]


def tela_menu():
    est = coletar()
    cabecalho("PAINEL DE CONTROLE · DUAL LOAD BALANCER LAB",
              "Projeto A2 · Sistemas Distribuídos · Caddy + Python + Docker + systemd")
    w = largura()
    esq = " %s · %s" % (CTX.ambiente, CTX.lab_curto)
    dir_ = "atualizado às %s " % datetime.datetime.now().strftime("%H:%M:%S")
    print(C.CINZA + esq + " " * max(1, w - len(esq) - len(dir_)) + dir_ + C.RESET)
    secao("STATUS GERAL")
    tabela_status(est)
    print(" " + C.CINZA + "-" * (w - 1) + C.RESET)
    texto("Fluxo: cliente -> a2-front -> lb1 / lb2 -> app1..app4 (round robin + health check /health). "
          "Status via systemctl show / docker inspect + requisição HTTP real (detalhes na opção 1).",
          cor=C.CINZA)
    if CTX.mensagem:
        print()
        print(CTX.mensagem)
        CTX.mensagem = None
    secao("MENU PRINCIPAL")
    for k, t, _ in ITENS_MENU:
        print(" [%s] %s" % (k, t))
    print()
    print(" [0] Sair do painel")
    print(C.AZUL + "=" * w + C.RESET)
    try:
        return ler("Selecione uma opção (ENTER atualiza o status):")
    except KeyboardInterrupt:
        print()
        return "0"


def loop_principal():
    acoes = {k: f for k, _, f in ITENS_MENU}
    while True:
        op = tela_menu()
        if op == "":
            continue
        if op == "0":
            if menu_sair():
                return
            continue
        func = acoes.get(op)
        if func is None:
            CTX.mensagem = (" " + C.VERMELHO + C.NEGRITO + "[ERRO]" + C.RESET +
                            " Opção inválida: '%s'. Digite um número de 0 a 8." % op)
            continue
        try:
            if func() == "SAIR":
                return
        except KeyboardInterrupt:
            CTX.mensagem = (" " + C.AMARELO + C.NEGRITO + "[AVISO]" + C.RESET +
                            " Operação interrompida com Ctrl+C. Nenhuma ação pendente foi executada.")


# ======================================================================
# INICIALIZAÇÃO AUTOMÁTICA (executada ao abrir o painel)
# ======================================================================

def _executar_visivel(cmd):
    texto("# " + cmd.descricao, recuo=3, cor=C.CINZA)
    cmd.exibir()
    s = cmd.executar()
    if not s.ok:
        rodape(s, cmd)
    return s


def inicializar_ambiente():
    """Verifica dependências e inicializa os componentes desligados.
    Retorna False em caso de erro fatal."""
    cabecalho("INICIALIZAÇÃO DO AMBIENTE", "Dual Load Balancer Lab · Projeto A2")
    print()
    print(" Projeto (pasta do iniciar.bat): " + CTX.repo_dir)
    print(" Diretório de execução:          " + CTX.lab_dir)
    print(" Ambiente:                       " + CTX.ambiente)
    print(" " + C.CINZA + "Os comandos abaixo são executados automaticamente nesta etapa." + C.RESET)

    secao("[1/4] Dependências")
    programas = ["docker", "make", "curl", "ss", "systemctl", "journalctl"]
    s = _executar_visivel(Comando("command -v " + " ".join(programas),
                                  "Os programas usados pelo painel estão instalados?", consulta=True))
    achados = {os.path.basename(l.strip()) for l in s.stdout.splitlines()}
    faltando = [p for p in programas if p not in achados]
    if faltando:
        erro("Programas ausentes no WSL: %s. Instale-os e abra o painel novamente." % ", ".join(faltando))
        return False
    print()
    s = _executar_visivel(Comando("ls -1 Makefile .env .venv/bin/ansible-playbook",
                                  "Arquivos do laboratório: Makefile, configuração local (.env) e Ansible (.venv).",
                                  consulta=True))
    if not os.path.isfile(os.path.join(CTX.lab_dir, ".env")):
        aviso("Arquivo .env ausente: o make usará o modo VPS (SSH). Veja a seção 9 do guia.")
    if not os.access(os.path.join(CTX.lab_dir, ".venv/bin/ansible-playbook"), os.X_OK):
        aviso("Ansible (.venv) não encontrado: make up / verify / test podem falhar.")
    print()
    s = _executar_visivel(Comando("sudo -n true", "O sudo funciona sem senha? (usado por systemctl start/stop)",
                                  consulta=True))
    CTX.sudo_sem_senha = s.ok
    if not s.ok:
        aviso("O sudo pedirá senha ao executar comandos 'sudo ...' (eles rodarão em primeiro plano).")
    if CTX.repo_dir != CTX.lab_dir:
        print()
        s1 = Comando("git log -1 --format='%h %s'", "Commit do clone de execução.", consulta=True)
        s2 = Comando("git -C \"%s\" log -1 --format='%%h %%s'" % CTX.repo_dir,
                     "Commit da pasta do projeto no Windows.", consulta=True)
        r1, r2 = _executar_visivel(s1), _executar_visivel(s2)
        if r1.ok and r2.ok and r1.stdout.split()[:1] != r2.stdout.split()[:1]:
            aviso("O clone de execução está em um commit diferente da pasta do Windows. Se você alterou "
                  "playbooks ou roles, sincronize: cd %s && git pull" % CTX.lab_curto)

    secao("[2/4] Serviço Docker")
    docker_ativo = Comando("systemctl is-active docker", "O serviço Docker está ativo?", consulta=True)
    s = _executar_visivel(docker_ativo)
    if s.stdout.strip() != "active":
        if s.stdout.strip() in ("activating", "reloading"):
            prog, t0 = Progresso(), time.time()
            while time.time() - t0 < 30 and docker_ativo.capturar().stdout.strip() != "active":
                prog.mostrar("   aguardando o Docker iniciar... %ds" % int(time.time() - t0))
                time.sleep(1)
            prog.fim()
        else:
            print()
            info("Docker parado: iniciando automaticamente.")
            _executar_visivel(Comando("sudo systemctl start docker", "Inicia o serviço Docker."))
    print()
    s = _executar_visivel(Comando("docker info --format '{{.ServerVersion}}'",
                                  "O usuário tem acesso ao Docker? (mostra a versão do servidor)", consulta=True))
    if not s.ok:
        erro("Sem acesso ao Docker. Verifique se o serviço está ativo e se o usuário está no grupo docker.")
        return False

    secao("[3/4] Estado atual dos componentes")
    texto("Consultas: systemctl show / docker inspect + curl em cada componente (as mesmas da opção 1).",
          recuo=1, cor=C.CINZA)
    est = coletar()
    transitorios = ("INICIALIZANDO", "ENCERRANDO")
    if any(e.estado in transitorios for e in est.values()):
        prog, t0 = Progresso(), time.time()
        while time.time() - t0 < 20 and any(e.estado in transitorios for e in est.values()):
            pend = [k for k, e in est.items() if e.estado in transitorios]
            prog.mostrar("   [%2ds] componentes em transição (boot do WSL?): %s" % (int(time.time() - t0),
                                                                                   ", ".join(pend)))
            time.sleep(1)
            est = coletar()
        prog.fim()
    print()
    tabela_status(est)

    secao("[4/4] Inicialização dos componentes desligados")
    parados = [c for c in CTX.comps if not est[c.chave].rodando]
    if not parados:
        sucesso("Todos os componentes já estão em execução: nada a inicializar (sem inicialização duplicada).")
    else:
        cmds, problemas = Cmd.iniciar(parados, est)
        for p in problemas:
            aviso(p)
        if cmds:
            info("Desligados: %s. Inicializando na ordem das dependências "
                 "(servidores -> load balancers -> entrada):" % nomes(parados))
            est = executar_plano(cmds)
    defeituosos = [c for c in CTX.comps if est[c.chave].estado == "ERRO" and est[c.chave].rodando]
    if defeituosos:
        aviso("Em execução, mas sem responder: %s. Use a opção 4 (reiniciar) ou 5 (logs)." % nomes(defeituosos))

    secao("RESUMO")
    tabela_status(est)
    print()
    fora = [c for c in CTX.comps if est[c.chave].estado != "ONLINE"]
    if not fora:
        sucesso("Ambiente pronto: todos os componentes estão ONLINE.")
        front = comps_tipo("front")
        if front:
            info("Entrada única no navegador do Windows: %s" % front[0].url_saude)
    else:
        aviso("Componentes fora do ar: %s. O painel abre mesmo assim." % nomes(fora))
    pausar("Pressione ENTER para abrir o painel de controle.")
    return True


# ======================================================================
# CONFIGURAÇÃO E PONTO DE ENTRADA
# ======================================================================

def _valido(d):
    return os.path.isfile(os.path.join(d, "Makefile")) and os.path.isdir(os.path.join(d, "playbooks"))


def localizar_lab_dir(arg):
    explicito = arg or os.environ.get("A2_LAB_DIR")
    if explicito:
        d = os.path.abspath(os.path.expanduser(explicito))
        return d if _valido(d) else None
    # Fora de /mnt (pasta já dentro do Linux) o próprio repositório serve.
    # Em /mnt/c o Ansible ignora o ansible.cfg (diretório gravável por
    # todos) e os arquivos têm quebra de linha do Windows: usa-se o clone.
    if not CTX.repo_dir.startswith("/mnt/") and _valido(CTX.repo_dir):
        return CTX.repo_dir
    d = os.path.expanduser("~/" + NOME_CLONE)
    return d if _valido(d) else None


def ler_env(caminho):
    env = {}
    try:
        with open(caminho, encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if not linha or linha.startswith("#") or "=" not in linha:
                    continue
                k, v = linha.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def adquirir_trava():
    if fcntl is None:
        return True
    caminho = os.path.join(tempfile.gettempdir(), "a2-painel-%d.lock" % os.getuid())
    try:
        CTX.trava = open(caminho, "w")
        fcntl.flock(CTX.trava, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def executar_painel(args):
    painel_dir = os.path.dirname(os.path.abspath(__file__))
    CTX.repo_dir = os.path.dirname(painel_dir)
    CTX.lab_dir = localizar_lab_dir(args.lab_dir)
    if not CTX.lab_dir:
        cabecalho("PAINEL DE CONTROLE · ERRO DE CONFIGURAÇÃO")
        print()
        erro("Diretório de execução do laboratório não encontrado.")
        texto("Esperado: um clone do projeto em ~/%s dentro do WSL (com Makefile e playbooks/). "
              "Veja a seção 9 do GUIA_EXECUCAO_E_TESTES.txt, ou informe outro caminho com "
              "--lab-dir / variável A2_LAB_DIR." % NOME_CLONE)
        return 1   # o iniciar.bat mantém a janela aberta em caso de erro
    home = os.path.expanduser("~")
    CTX.lab_curto = "~" + CTX.lab_dir[len(home):] if CTX.lab_dir.startswith(home + "/") else CTX.lab_dir
    CTX.env = ler_env(os.path.join(CTX.lab_dir, ".env"))
    CTX.rede = CTX.env.get("A2_DOCKER_NET_NAME") or "a2_net"
    CTX.dir_dados = CTX.env.get("A2_DATA_DIR") or "/opt/a2"
    CTX.distro = os.environ.get("WSL_DISTRO_NAME", "")
    CTX.ambiente = ("WSL " + CTX.distro) if CTX.distro else os.uname().nodename
    CTX.comps = montar_componentes(CTX.env)
    CTX.sudo_sem_senha = subprocess.run(["sudo", "-n", "true"], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    if not adquirir_trava():
        cabecalho("PAINEL DE CONTROLE")
        print()
        aviso("Já existe outro painel aberto. Esta janela não executará a inicialização automática.")
        texto("Você pode usá-la normalmente (por exemplo, para o monitor de tráfego ao vivo), mas evite "
              "executar operações nas duas janelas ao mesmo tempo.", cor=C.CINZA)
        pausar("Pressione ENTER para abrir o menu.")
    elif not args.sem_inicializar:
        if not inicializar_ambiente():
            return 1   # o iniciar.bat mantém a janela aberta em caso de erro
    loop_principal()
    return 0


def main():
    parser = argparse.ArgumentParser(description="Painel de controle do Dual Load Balancer Lab")
    parser.add_argument("--sem-inicializar", action="store_true",
                        help="abre o menu sem a inicialização automática dos componentes")
    parser.add_argument("--lab-dir", help="diretório do clone de execução (padrão: ~/%s)" % NOME_CLONE)
    args = parser.parse_args()

    if os.name != "posix":
        print("Este painel roda dentro do WSL. Execute o iniciar.bat.")
        return 1
    configurar_terminal()
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, _sinal_terminacao)
    try:
        return executar_painel(args)
    except FimDaEntrada:
        return 0
    except KeyboardInterrupt:
        print()
        return 0
    except OSError as e:
        if e.errno == 5:   # EIO: terminal fechado
            return 0
        raise


if __name__ == "__main__":
    sys.exit(main())
