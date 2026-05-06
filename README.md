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
    ├── worker_join/           # Join worker nodes to the cluster
    ├── cluster_addons/        # MetalLB, cert-manager, Traefik, Longhorn UI
    ├── node_maintenance/      # Drain, update, reboot, uncordon
    └── cluster_cleanup/       # Remove hosts from Foreman and FreeIPA
```

## Idempotency and Scale-Out

The playbook is safe to re-run against a partially or fully provisioned cluster:

- **Prep phase** — each node checks for `/etc/kubernetes/kubelet.conf` before running `common`, `controlplane_infra`, and `worker_storage`. Nodes already in the cluster skip those roles entirely.
- **Bootstrap phase** — `join_secondary` checks for `kubelet.conf` and skips the join if the node is already a cluster member. When `controlplane_node_count=0`, the entire bootstrap phase is skipped.
- **Worker join** — workers are skipped if they already have `kubelet.conf`. The join token is generated fresh via `delegate_to` directly against a control plane node, so running with `--limit` scoped to only new workers works without needing the control plane in scope.
- **Cluster add-ons** — skipped when `controlplane_node_count=0` (already installed on the running cluster).

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

| Variable | Type | Default | Description |
|---|---|---|---|
| `controlplane_node_count` | Integer | 1 | Number of control plane nodes to provision this run. Set to `0` when only adding worker nodes — this skips the bootstrap and cluster add-ons phases entirely. Max 3. |
| `worker_node_count` | Integer | 1 | Number of worker nodes to add this run. The provisioning role queries Foreman for existing workers with the configured prefix and starts numbering from the next available index. Running with `worker_node_count=3` twice produces 6 workers total. |

Neither control plane nor worker nodes have hardcoded names. Both are generated at runtime from a prefix and a counter. Name prefixes and Foreman hostgroup strings are configured in `vars/vms.yml`.

## Usage

### Full Cluster Provisioning

Run the entire pipeline end to end:

```bash
ansible-playbook main.yml --ask-vault-pass
```

Add 3 control plane nodes and 5 worker nodes:

```bash
ansible-playbook main.yml --ask-vault-pass -e "controlplane_node_count=3 worker_node_count=5"
```

### Adding Worker Nodes to an Existing Cluster

Set `controlplane_node_count=0` to skip bootstrap and cluster add-ons. The playbook provisions the new VMs, preps them, joins them to the cluster, and stops — existing nodes that already have `kubelet.conf` are skipped during prep:

```bash
ansible-playbook main.yml --ask-vault-pass -e "controlplane_node_count=0 worker_node_count=2"
```

### Run a Specific Phase

Each phase is tagged. You can run or skip individual phases:

```bash
# Phase 1: Create VMs in Proxmox via Foreman
ansible-playbook main.yml --tags provision

# Phase 2: Install K8s prerequisites on all nodes (skips already-provisioned nodes)
ansible-playbook main.yml --tags prep

# Phase 3: Bootstrap control plane (kubeadm init, join secondaries, tooling)
ansible-playbook main.yml --tags bootstrap

# Phase 4: Join workers to the cluster
ansible-playbook main.yml --tags workers

# Phase 5: Install add-ons (MetalLB, metrics-server)
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

Queries Foreman for all hosts matching the configured CP and worker prefixes and removes them from Foreman and FreeIPA. Finds everything regardless of how many scale-out runs were done. Does not touch the Proxmox VMs themselves.

```bash
ansible-playbook wipe.yml --ask-vault-pass
```

## VM Definitions

Edit `vars/vms.yml` to configure nodes. There are no hardcoded hostnames — both control plane and worker names are generated at runtime from a prefix and a counter (e.g. `naxxramas-cp-01`, `naxxramas-worker-03`).

Control plane configs are ordered: index 0 is always the primary. Only the first `controlplane_node_count` entries are provisioned.

```yaml
# Naming — adjust prefixes and hostgroups to match your Foreman setup
controlplane_hostgroup: "AlmaLinux 10/Kubernetes Controlplane Node"
controlplane_name_prefix: "my-cluster-cp"     # → my-cluster-cp-01, my-cluster-cp-02, …

worker_hostgroup: "AlmaLinux 10/Kubernetes Worker Node"
worker_name_prefix: "my-cluster-worker"       # → my-cluster-worker-01, my-cluster-worker-02, …

controlplane_configs:
  # Index 0 — primary, always provisioned
  - host_parameters:
      - { name: k8s_role,            value: primary }
      - { name: keepalived_state,    value: MASTER }
      - { name: keepalived_priority, value: 101 }
      - { name: cluster_name,        value: my-cluster }
      - { name: k8s_api_endpoint,    value: k8s-api.example.com }
      - { name: k8s_api_endpoint_ip, value: 172.16.0.29 }
      - { name: metallb_pool,        value: 172.16.0.50-172.16.0.60 }

  # Index 1 — secondary, provisioned when controlplane_node_count >= 2
  - host_parameters:
      - { name: k8s_role,            value: secondary }
      - { name: keepalived_state,    value: BACKUP }
      - { name: keepalived_priority, value: 100 }

  # Index 2 — secondary, provisioned when controlplane_node_count >= 3
  - host_parameters:
      - { name: k8s_role,            value: secondary }
      - { name: keepalived_state,    value: BACKUP }
      - { name: keepalived_priority, value: 99 }
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
| Traefik dashboard | `https://traefik.k8s.<domain>` |
| Longhorn dashboard | `https://longhorn.k8s.<domain>` |
| Kubernetes API | `https://<k8s_api_endpoint>:6443` |

Point `*.k8s.<domain>` at the Traefik MetalLB LoadBalancer IP in your DNS provider. The wildcard cert covers `*.k8s.<domain>` — configured via `ingress_domain` in `roles/cluster_addons/defaults/main.yml`.
