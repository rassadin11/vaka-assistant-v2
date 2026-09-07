# Ansible provisioning

This directory contains the production provisioning code. All three roles are
implemented and used in production (31.76.15.130): `base` (server hardening +
`deploy` user), `docker` (Docker Engine + compose plugin), `app` (app dirs,
`compose.prod.yml`, Caddy/Prometheus/Grafana configs, systemd unit, webapp).
Day-to-day operations (deploy, secrets, rollback) live in
[docs/runbook-deploy.md](../../docs/runbook-deploy.md); infra changes go through
the `app` role only — never edit files on the server by hand.

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
ansible-playbook -i inventory/prod.yml site.yml -e prod_server_ip=<IP> --ask-vault-pass
```

The inventory connects as `deploy` by default. On a **fresh server** the `deploy`
user does not exist yet, so bootstrap the first run as `root` — override the
inventory host var via extra-vars (a plain `-u root` is ignored because the
host-level `ansible_user` wins over `--user`):

```shell
ansible-playbook -i inventory/prod.yml site.yml -e prod_server_ip=<IP> -e ansible_user=root
```

After the base role creates `deploy` (key + passwordless sudo) and hardens sshd,
every subsequent run uses the default `deploy` user. Root key login stays enabled
as an emergency fallback (`PermitRootLogin prohibit-password`).

Create `group_vars/prod/vault.yml` from `vault.yml.example`, replace its placeholders,
and encrypt it with `ansible-vault`. The vault password belongs outside this repository:
use a local password manager or an approved CI secret.
