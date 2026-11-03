{
  description = "Ambiente de desenvolvimento para automacao do Projeto A2";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
          python = pkgs.python312;
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              python
              python.pkgs.pip
              python.pkgs.setuptools
              python.pkgs.wheel
              curl
              openssh
              sshpass
              jq
            ];

            shellHook = ''
              export ANSIBLE_CONFIG="$PWD/ansible.cfg"
              export PATH="$PWD/.venv/bin:$PATH"

              if [ ! -x .venv/bin/python ]; then
                echo "Criando .venv local enxuto do projeto..."
                python -m venv .venv
              fi

              requirements_hash="$(cat requirements.txt requirements.yml | sha256sum | cut -d ' ' -f 1)"
              requirements_stamp=".venv/.requirements.sha256"

              if [ ! -x .venv/bin/ansible-playbook ] \
                || [ ! -f "$requirements_stamp" ] \
                || [ "$(cat "$requirements_stamp" 2>/dev/null)" != "$requirements_hash" ]; then
                echo "Instalando ansible-core e dependencias no .venv..."
                .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
                echo "Instalando colecoes Ansible necessarias (community.docker, community.general)..."
                .venv/bin/ansible-galaxy collection install -r requirements.yml -p .venv/collections --force
                printf '%s\n' "$requirements_hash" > "$requirements_stamp"
              fi

              echo "Ambiente pronto. Ansible: $(.venv/bin/ansible-playbook --version | head -n 1)"
            '';
          };
        });
    };
}
