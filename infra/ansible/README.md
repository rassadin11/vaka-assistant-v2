# Ansible provisioning skeleton

This directory contains the production provisioning scaffold. The `base`, `docker`,
and `app` roles are intentionally no-op stubs until tasks 1.10.2 through 1.10.4.

## Setup

Install the required Ansible collections:

```shell
ansible-galaxy collection install -r requirements.yml
```

Run the Molecule scenario, including idempotence verification:

```shell
molecule test
```

Run the production playbook after setting `prod_server_ip` and creating the encrypted
vault file:

```shell
ansible-playbook -i inventory/prod.yml site.yml --ask-vault-pass
```

Create `group_vars/prod/vault.yml` from `vault.yml.example`, replace its placeholders,
and encrypt it with `ansible-vault`. The vault password belongs outside this repository:
use a local password manager or an approved CI secret.
