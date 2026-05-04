# Kubernetes Cluster Provisioning

Ansible automation for provisioning and managing a highly-available Kubernetes cluster on Proxmox via Foreman. Targets AlmaLinux 10, uses Flannel CNI, MetalLB, Traefik, cert-manager (Cloudflare DNS-01), and Longhorn storage.

## Cluster Architecture

| Component | Details |
|---|---|
| Control plane | 1–3 nodes (primary always included, up to 2 secondaries), HA via HAProxy + keepalived |
| Workers | 1–13 nodes, Longhorn LVM on dedicated data disk |
| Networking | Flannel VXLAN, MetalLB L2 LoadBalancer |
| Ingress | Traefik v2 with TLS redirect |
| TLS | cert-manager + Let's Encrypt + Cloudflare DNS-01 |
| Storage | Longhorn with XFS LVM partition |
| Tooling | kubectl, Helm, k9s on all control plane nodes |

Node counts are defined in `vars/vms.yml` and filtered at runtime via `controlplane_node_count` and `worker_node_count` (see [AWX Survey Variables](#awx-survey-variables)).

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
    ├── vm_provisioning/       # Create VMs in Foreman/Proxmox, filters by node counts
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

## AWX Survey Variables

When running via AWX/Tower, these survey variables control how many nodes are provisioned. Both default to `1` if not provided by a survey.

| Variable | Type | Min | Max | Default | Description |
|---|---|---|---|---|---|
| `controlplane_node_count` | Integer | 1 | 3 | 1 | Number of control plane nodes to provision. The primary node (`k8s_role: primary`) is always included; the value determines how many secondaries are added (count − 1). |
| `worker_node_count` | Integer | 1 | 13 | 1 | Number of worker nodes to provision, selected in the order they appear in `vars/vms.yml`. |

All node definitions live in `vars/vms.yml`. The `vm_provisioning` role filters that list at runtime before any Foreman API calls are made, so only the selected nodes are created.

## Usage

### Full Cluster Provisioning

Run the entire pipeline end to end:

```bash
ansible-playbook main.yml --ask-vault-pass
```

Override node counts at the command line:

```bash
ansible-playbook main.yml --ask-vault-pass -e "controlplane_node_count=3 worker_node_count=5"
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

All available nodes are defined in `vars/vms.yml`. The `vm_provisioning` role selects a subset at runtime based on `controlplane_node_count` and `worker_node_count`.

Control plane nodes must include a `host_parameters` list. The node with `k8s_role: primary` is the kubeadm bootstrap node and is always provisioned regardless of `controlplane_node_count`. Secondary nodes are selected in definition order up to `controlplane_node_count - 1`.

```yaml
vms:
  # Primary control plane — always provisioned
  - name: my-cp-01
    hostgroup: "AlmaLinux 10/Kubernetes Controlplane Node"
    host_parameters:
      - { name: k8s_role,            value: primary }
      - { name: keepalived_state,    value: MASTER }
      - { name: keepalived_priority, value: 101 }
      - { name: cluster_name,        value: my-cluster }
      - { name: k8s_api_endpoint,    value: k8s-api.example.com }
      - { name: k8s_api_endpoint_ip, value: 172.16.0.29 }
      - { name: metallb_pool,        value: 172.16.0.50-172.16.0.60 }

  # Secondary control planes — included when controlplane_node_count > 1
  - name: my-cp-02
    hostgroup: "AlmaLinux 10/Kubernetes Controlplane Node"
    host_parameters:
      - { name: k8s_role,            value: secondary }
      - { name: keepalived_state,    value: BACKUP }
      - { name: keepalived_priority, value: 100 }

  # Workers — included up to worker_node_count, in definition order
  - { name: my-worker-01, hostgroup: "AlmaLinux 10/Kubernetes Worker Node" }
  - { name: my-worker-02, hostgroup: "AlmaLinux 10/Kubernetes Worker Node" }
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
