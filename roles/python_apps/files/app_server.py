#!/usr/bin/env python3
"""
Projeto A2 - Sistemas Distribuídos
Servidor HTTP simples em Python (sem dependências externas)
Substitui os containers nginx por processos nativos gerenciados via systemd.
"""

import argparse
import datetime
import html
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name_upper} · Projeto A2 · Sistemas Distribuídos</title>
<link rel="icon" href="data:,">
<style>
  :root {{
    --tema: {color};
    --tema-escuro: {color_dark};
    --tema-claro: {color_light};
    --texto: #1F2937;
    --texto-suave: #4B5563;
    --linha: #E5E7EB;
    --fundo: #F8FAFC;
    --branco: #FFFFFF;
    --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, Ubuntu, sans-serif;
    color: var(--texto);
    background: var(--fundo);
    line-height: 1.6;
  }}
  .faixa-topo {{ height: 6px; background: var(--tema); }}
  .container {{ max-width: 1000px; margin: 0 auto; padding: 0 20px; }}
  header.barra {{ background: var(--branco); border-bottom: 1px solid var(--linha); padding: 14px 0; }}
  header.barra .container {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }}
  .marca {{ font-weight: 700; }}
  .marca span {{ color: var(--tema); }}
  .pilula {{
    display: inline-flex; align-items: center; gap: 8px;
    background: var(--tema-claro); color: var(--tema-escuro);
    border-radius: 999px; padding: 4px 14px; font-size: 13px; font-weight: 600;
  }}
  .ponto {{ width: 10px; height: 10px; border-radius: 50%; background: var(--tema); }}
  .destaque {{ background: linear-gradient(135deg, var(--tema-escuro), var(--tema)); color: #fff; padding: 48px 0 56px; }}
  .destaque .container {{ display: grid; grid-template-columns: 1.2fr .8fr; gap: 32px; align-items: center; }}
  @media (max-width: 768px) {{
    .destaque .container {{ grid-template-columns: 1fr; }}
  }}
  .destaque h1 {{ font-size: 36px; line-height: 1.15; margin: 8px 0 12px; }}
  .destaque p.lead {{ font-size: 17px; opacity: .95; margin: 0; }}
  .cartao-servidor {{ background: var(--branco); color: var(--texto); border-radius: 16px; padding: 24px; box-shadow: 0 20px 40px rgba(0,0,0,.2); }}
  .rotulo {{ font-size: 12px; color: var(--texto-suave); text-transform: uppercase; letter-spacing: 1.2px; font-weight: 700; }}
  .nome {{ font-size: 54px; font-weight: 800; line-height: 1; color: var(--tema); margin: 6px 0 14px; }}
  dl {{ margin: 0; display: grid; grid-template-columns: auto 1fr; gap: 8px 12px; font-size: 14px; }}
  dt {{ color: var(--texto-suave); }}
  dd {{ margin: 0; font-family: var(--mono); font-weight: 600; text-align: right; }}
  .servidores {{ margin-top: -24px; }}
  .grade {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }}
  @media (max-width: 640px) {{ .grade {{ grid-template-columns: repeat(2, 1fr); }} }}
  .srv {{
    background: var(--branco); border: 1px solid var(--linha); border-radius: 12px;
    padding: 12px 14px; display: flex; gap: 10px; align-items: center;
    box-shadow: 0 2px 8px rgba(0,0,0,.04);
  }}
  .srv.atual {{ border: 2px solid var(--tema); }}
  .srv-cor {{ width: 28px; height: 28px; border-radius: 8px; flex: none; }}
  .srv-info b {{ display: block; font-size: 14px; }}
  .srv-info small {{ color: var(--texto-suave); font-size: 12px; }}
  .conteudo {{ padding: 36px 0; }}
  .card {{ background: var(--branco); border: 1px solid var(--linha); border-radius: 14px; padding: 20px; margin-bottom: 20px; }}
  .card h3 {{ margin-top: 0; font-size: 18px; }}
</style>
</head>
<body>
<div class="faixa-topo"></div>
<header class="barra">
  <div class="container">
    <div class="marca">Projeto A2 · <span>Sistemas Distribuídos</span></div>
    <div class="pilula"><span class="ponto"></span> Python HTTP Server ({name})</div>
  </div>
</header>

<div class="destaque">
  <div class="container">
    <div>
      <div style="text-transform: uppercase; letter-spacing: 1.5px; font-size: 12px; font-weight: 700; opacity: .85;">Balanceamento Ativo</div>
      <h1>Servidor {name_upper} respondeu</h1>
      <p class="lead">Esta requisição passou pelo balanceador Caddy (em container) e foi atendida pela aplicação Python nativa na VPS.</p>
    </div>
    <div>
      <div class="cartao-servidor">
        <div class="rotulo">Servidor Atual</div>
        <div class="nome">{name}</div>
        <dl>
          <dt>Cor tema:</dt><dd>{color_name} ({color})</dd>
          <dt>Porta local:</dt><dd>{port}</dd>
          <dt>Balanceador (Caddy):</dt><dd>{lb_name}</dd>
          <dt>IP Cliente:</dt><dd>{client_ip}</dd>
          <dt>Horário:</dt><dd>{timestamp}</dd>
        </dl>
      </div>
    </div>
  </div>
</div>

<div class="container servidores">
  <div class="grade">
    <div class="srv {srv_active_app1}">
      <div class="srv-cor" style="background: #2563EB;"></div>
      <div class="srv-info"><b>app1</b><small>Azul · :{p_app1}</small></div>
    </div>
    <div class="srv {srv_active_app2}">
      <div class="srv-cor" style="background: #059669;"></div>
      <div class="srv-info"><b>app2</b><small>Verde · :{p_app2}</small></div>
    </div>
    <div class="srv {srv_active_app3}">
      <div class="srv-cor" style="background: #EA580C;"></div>
      <div class="srv-info"><b>app3</b><small>Laranja · :{p_app3}</small></div>
    </div>
    <div class="srv {srv_active_app4}">
      <div class="srv-cor" style="background: #7C3AED;"></div>
      <div class="srv-info"><b>app4</b><small>Roxo · :{p_app4}</small></div>
    </div>
  </div>
</div>

<div class="container conteudo">
  <div class="card">
    <h3>Topologia da Solução</h3>
    <p>
      <b>Ponto de Entrada Público:</b> Caddy nativo no host Debian (terminação HTTPS / portas 80 e 443).<br>
      <b>Balanceadores de Carga:</b> 2 containers Caddy (<code>lb1</code> na porta {p_lb1} e <code>lb2</code> na porta {p_lb2}) em loopback.<br>
      <b>Servidores de Aplicação:</b> 4 processos Python HTTP nativos no Debian, gerenciados via <code>systemd</code> (portas {p_app1} a {p_app4}).<br>
      <b>Resiliência:</b> Verificações ativas via <code>/health</code> e tentativa com duração/intervalo configurados.
    </p>
  </div>
</div>
</body>
</html>
"""


class AppRequestHandler(BaseHTTPRequestHandler):
    server_version = "A2-PythonHTTP/1.0"
    sys_version = ""

    def log_message(self, format, *args):
        # Log simplificado para journald / stdout
        sys.stdout.write(f"[{self.log_date_time_string()}] {self.address_string()} - {format % args}\n")
        sys.stdout.flush()

    def do_GET(self):
        # Health check
        if self.path == "/health":
            body = f"ok {self.server.app_name}\n".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Servidor", self.server.app_name)
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return

        # Cabeçalhos recebidos do proxy com sanitização HTML
        lb_name = html.escape(self.headers.get("X-Load-Balancer", "-"))
        raw_client_ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        client_ip = html.escape(raw_client_ip)
        timestamp = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

        name = self.server.app_name
        rendered = HTML_TEMPLATE.format(
            name=name,
            name_upper=name.upper(),
            port=self.server.app_port,
            color=self.server.app_color,
            color_dark=self.server.app_color_dark,
            color_light=self.server.app_color_light,
            color_name=self.server.app_color_name,
            p_app1=self.server.ports.get("app1", "31001"),
            p_app2=self.server.ports.get("app2", "31002"),
            p_app3=self.server.ports.get("app3", "31003"),
            p_app4=self.server.ports.get("app4", "31004"),
            p_lb1=self.server.ports.get("lb1", "31081"),
            p_lb2=self.server.ports.get("lb2", "31082"),
            lb_name=lb_name,
            client_ip=client_ip,
            timestamp=timestamp,
            srv_active_app1="atual" if name == "app1" else "",
            srv_active_app2="atual" if name == "app2" else "",
            srv_active_app3="atual" if name == "app3" else "",
            srv_active_app4="atual" if name == "app4" else "",
        )

        content = rendered.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Servidor", name)
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    do_HEAD = do_GET


def main():
    parser = argparse.ArgumentParser(description="Projeto A2 - Servidor HTTP da Aplicação")
    parser.add_argument("--name", required=True, help="Nome do servidor (ex: app1)")
    parser.add_argument("--port", type=int, required=True, help="Porta para escutar")
    parser.add_argument("--bind", default="0.0.0.0", help="Endereço de bind (padrão: 0.0.0.0)")
    parser.add_argument("--color", default="#2563EB", help="Cor hexadecimal do tema")
    parser.add_argument("--color-dark", default="#1E3A8A", help="Cor escura do tema")
    parser.add_argument("--color-light", default="#DBEAFE", help="Cor clara do tema")
    parser.add_argument("--color-name", default="Azul", help="Nome da cor do tema")
    parser.add_argument("--app1-port", default="31001", help="Porta app1")
    parser.add_argument("--app2-port", default="31002", help="Porta app2")
    parser.add_argument("--app3-port", default="31003", help="Porta app3")
    parser.add_argument("--app4-port", default="31004", help="Porta app4")
    parser.add_argument("--lb1-port", default="31081", help="Porta lb1")
    parser.add_argument("--lb2-port", default="31082", help="Porta lb2")
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.bind, args.port), AppRequestHandler)
    httpd.app_name = args.name
    httpd.app_port = args.port
    httpd.app_color = args.color
    httpd.app_color_dark = args.color_dark
    httpd.app_color_light = args.color_light
    httpd.app_color_name = args.color_name
    httpd.ports = {
        "app1": args.app1_port,
        "app2": args.app2_port,
        "app3": args.app3_port,
        "app4": args.app4_port,
        "lb1": args.lb1_port,
        "lb2": args.lb2_port,
    }

    def handle_sigterm(signum, frame):
        sys.stdout.write(f"Encerrando {args.name}...\n")
        sys.stdout.flush()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    sys.stdout.write(f"Servidor {args.name} ouvindo em http://{args.bind}:{args.port}/ (cor: {args.color_name})\n")
    sys.stdout.flush()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
