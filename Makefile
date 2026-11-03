ifneq (,$(wildcard ./.env))
    include .env
    export
endif

ifeq ($(IN_NIX_SHELL),)
  ifneq ($(shell command -v nix 2>/dev/null),)
    NIX_RUN = nix develop --command
  endif
endif

ANSIBLE = $(if $(NIX_RUN),$(NIX_RUN) ansible,$(if $(wildcard .venv/bin/ansible),.venv/bin/ansible,ansible))
ANSIBLE_PLAYBOOK = $(if $(NIX_RUN),$(NIX_RUN) ansible-playbook,$(if $(wildcard .venv/bin/ansible-playbook),.venv/bin/ansible-playbook,ansible-playbook))

BECOME_FLAG = $(if $(filter 1,$(A2_SEM_SENHA)),,-K)
SSH_PASS_FLAG = $(if $(filter 0,$(A2_SENHA_SSH)),,-k)
FLAGS = $(SSH_PASS_FLAG) $(BECOME_FLAG) $(ARGS)

VPS_USER ?= deploy
VPS_HOST ?= 127.0.0.1
VPS_PORT ?= 22

.PHONY: help ssh ping up verify test test-t3 test-t4 test-t5 test-t6 test-t7 status down

help:
	@echo "Comandos disponíveis:"
	@echo "  make ssh         -> Conecta via SSH ao host ($(VPS_USER)@$(VPS_HOST):$(VPS_PORT))"
	@echo "  make ping        -> Testa conectividade com o host"
	@echo "  make up          -> Deploy completo da infraestrutura"
	@echo "  make verify      -> Verificação de saúde e rodízio"
	@echo "  make test        -> Executa todos os testes (T3 a T7)"
	@echo "  make test-t3     -> Teste T3: Distribuição uniforme"
	@echo "  make test-t4     -> Teste T4: Queda com tráfego contínuo"
	@echo "  make test-t5     -> Teste T5: Recuperação de nó"
	@echo "  make test-t6     -> Teste T6: Falha múltipla"
	@echo "  make test-t7     -> Teste T7: Failover via domínio"
	@echo "  make status      -> Diagnóstico de containers e serviços"
	@echo "  make down        -> Remoção completa do ambiente"

ssh:
	ssh -p $(VPS_PORT) $(VPS_USER)@$(VPS_HOST)

ping:
	$(ANSIBLE) vps -m ping $(FLAGS)

up:
	$(ANSIBLE_PLAYBOOK) playbooks/site.yml $(FLAGS)

verify:
	$(ANSIBLE_PLAYBOOK) playbooks/verify.yml $(FLAGS)

test:
	$(ANSIBLE_PLAYBOOK) playbooks/testes.yml $(FLAGS)

test-t3:
	$(ANSIBLE_PLAYBOOK) playbooks/testes.yml --tags t3 $(FLAGS)

test-t4:
	$(ANSIBLE_PLAYBOOK) playbooks/testes.yml --tags t4 $(FLAGS)

test-t5:
	$(ANSIBLE_PLAYBOOK) playbooks/testes.yml --tags t5 $(FLAGS)

test-t6:
	$(ANSIBLE_PLAYBOOK) playbooks/testes.yml --tags t6 $(FLAGS)

test-t7:
	$(ANSIBLE_PLAYBOOK) playbooks/testes.yml --tags t7 $(FLAGS)

status:
	$(ANSIBLE_PLAYBOOK) playbooks/status.yml $(FLAGS)

down:
	$(ANSIBLE_PLAYBOOK) playbooks/teardown.yml $(FLAGS)
