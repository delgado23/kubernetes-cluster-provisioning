# Kubernetes Cluster Provisioning

Ansible automation for provisioning and managing a highly-available Kubernetes cluster on Proxmox via Foreman. Targets AlmaLinux 10, uses Flannel CNI, MetalLB, Traefik, cert-manager (Cloudflare DNS-01), and Longhorn storage.

## Cluster Architecture

| Component | Details |
|---|---|
| Control plane | 3 nodes (1 primary + 2 secondary), HA via HAProxy + keepalived |
| Workers | 4 nodes, Longhorn LVM on dedicated data disk |
| Networking | Flannel VXLAN, MetalLB L2 LoadBalancer |
| Ingress | Traefik v2 with TLS redirect |
| TLS | cert-manager + Let's Encrypt + Cloudflare DNS-01 |
| Storage | Longhorn with XFS LVM partition |
| Tooling | kubectl, Helm, k9s on all control plane nodes |

## Prerequisites

- Ansible control node with collections:
  - `ansible.posix`
  - `community.general`
  - `theforeman.foreman`
  - `freeipa.ansible_freeipa`
- Foreman with a Proxmox compute resource configured
- Ansible Vault password for `vars/vault.yml`
- Cloudflare API token with `Zone/DNS/Edit` permission on your domain

## Repository Structure

```
.
├── main.yml              # Full cluster provisioning pipeline
├── maintenance.yml       # Rolling OS maintenance (drain/update/reboot/uncordon)
├── wipe.yml              # Remove all nodes from Foreman and FreeIPA
├── vars/
│   ├── foreman.yml       # Foreman connection settings and compute profile IDs
│   ├── freeipa.yml       # FreeIPA connection settings
│   ├── vault.yml         # Ansible Vault: API tokens and passwords
│   ├── vms.yml           # VM definitions (names, hostgroups, Foreman parameters)
│   └── vm_defaults.yml   # Default VM hardware specs
└── roles/
    ├── vm_provisioning/       # Create VMs in Foreman/Proxmox
    ├── proxmox_postconfig/    # EFI disk, rename, tags via Proxmox API
    ├── common/                # K8s prerequisites: containerd, kubelet, firewall
    ├── controlplane_infra/    # HAProxy, keepalived, controlplane firewall rules
    ├── worker_storage/        # LVM setup for Longhorn on worker data disk
    ├── cluster_bootstrap/     # kubeadm init/join, Flannel, tooling (Helm, k9s)
    ├── cluster_addons/        # Workers join, MetalLB, cert-manager, Traefik, Longhorn UI
    ├── node_maintenance/      # Drain, update, reboot, uncordon
    └── cluster_cleanup/       # Remove hosts from Foreman and FreeIPA
```

## Vault Variables

Encrypt `vars/vault.yml` with `ansible-vault encrypt vars/vault.yml`. Required keys:

| Variable | Description |
|---|---|
| `vault_proxmox_api_token` | Proxmox API token secret |
| `vault_cloudflare_api_token` | Cloudflare API token for DNS-01 |
| `vault_traefik_dashboard_password` | bcrypt htpasswd string for Traefik dashboard |
| `vault_longhorn_dashboard_password` | bcrypt htpasswd string for Longhorn dashboard |
| `foreman_user_vault` | Foreman username |
| `foreman_password_vault` | Foreman password |
| `ipa_password` | FreeIPA admin password |
| `keepalived_password` | Keepalived VRRP authentication password |

Generate dashboard passwords with:
```bash
htpasswd -nbB admin 'yourpassword'
```

## Usage

### Full Cluster Provisioning

Run the entire pipeline end to end:

```bash
ansible-playbook main.yml --ask-vault-pass
```

### Run a Specific Phase

Each phase is tagged. You can run or skip individual phases:

```bash
# Phase 1: Create VMs in Proxmox via Foreman
ansible-playbook main.yml --tags provision

# Phase 2: Install K8s prerequisites on all nodes
ansible-playbook main.yml --tags prep

# Phase 3: Bootstrap control plane (kubeadm init, join secondaries, tooling)
ansible-playbook main.yml --tags bootstrap

# Phase 4: Join workers
ansible-playbook main.yml --tags workers

# Phase 5: Install add-ons (MetalLB, cert-manager, Traefik, Longhorn UI)
ansible-playbook main.yml --tags addons

# Skip VM provisioning for re-runs against existing nodes
ansible-playbook main.yml --skip-tags provision
```

### Node Maintenance (Rolling OS Updates)

Drains, updates packages, reboots, and uncordons nodes. Control planes are rolled one at a time; workers two at a time.

```bash
ansible-playbook maintenance.yml --ask-vault-pass

# Workers only
ansible-playbook maintenance.yml --limit foreman_almalinux10_kubernetesworkernode

# Override drain timeout
ansible-playbook maintenance.yml -e "drain_timeout=600"
```

### Wipe Cluster

Removes all nodes from Foreman and FreeIPA. Does not touch the Proxmox VMs themselves.

```bash
ansible-playbook wipe.yml --ask-vault-pass
```

## VM Definitions

Edit `vars/vms.yml` to add or remove nodes. Each VM entry supports:

```yaml
vms:
  - name: my-node
    hostgroup: "AlmaLinux 10/Kubernetes Controlplane Node"
    host_parameters:
      - name: k8s_role
        value: primary
      - name: keepalived_state
        value: MASTER
      - name: keepalived_priority
        value: 101
      - name: keepalived_vip
        value: "172.16.0.29"
      - name: cluster_name
        value: my-cluster
      - name: k8s_api_endpoint
        value: k8s-api.example.com
      - name: k8s_pod_cidr
        value: "10.244.0.0/16"
      - name: k8s_service_cidr
        value: "10.96.0.0/12"
      - name: metallb_pool
        value: "172.16.0.50-172.16.0.60"
      - name: haproxy_port
        value: "7443"
```

## Proxmox API Token Setup

Create the token once in Proxmox before running the playbooks:

1. Datacenter → Permissions → API Tokens → Add
2. User: `root@pam`, Token ID: `ansible`, Privilege Separation: **No**
3. Copy the displayed secret (shown only once) into vault as `vault_proxmox_api_token`

## Exposed Services

After a successful run:

| Service | URL |
|---|---|
| Traefik dashboard | `https://traefik.<domain>` |
| Longhorn dashboard | `https://longhorn.<domain>` |
| Kubernetes API | `https://<k8s_api_endpoint>:6443` |

Point `*.<domain>` at the Traefik MetalLB LoadBalancer IP in your DNS provider.
